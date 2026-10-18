import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.crawler import ManualActionRequired, PageFailure, normalize_comment
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.engine_tasks import CommentTask
from backend.likes import normalize_liked_video
from backend.responses import (
    Kind, LoginRequired, NetworkPause, RequestFailure, RequestRejected,
    classify_http,
)
from backend.stable_policy import ProtectionPause
from test_crawler import comment
from test_engine_integration import Fixture, SimulatedTransport, args_for, create, fixture_signing
from test_likes_collector import video
from test_stable_policy import Clock, policy
from test_transport_speed import adapter


@pytest.mark.parametrize('status,data,category,expected', [
    (401, None, None, Kind.LOGIN),
    (200, {'status_code': 1001}, None, Kind.LOGIN),
    (200, {'status_code': 0, 'message': '请重新登录'}, None, Kind.LOGIN),
    (403, {'status_code': 1001, 'status_msg': '请登录'}, None, Kind.LOGIN),
    (200, {'search_nil_info': {'search_nil_type': 'login'}}, None, Kind.LOGIN),
    (200, {'search_nil_info': {'search_nil_type': 'verify_check'}}, None, Kind.VERIFICATION),
    (200, {'verify_check': True, 'status_msg': '请登录'}, None, Kind.VERIFICATION),
    (403, {'verify_info': {'type': 'captcha'}}, None, Kind.VERIFICATION),
    (403, {'status_code': 20, 'status_msg': '该账号未公开推荐列表'}, 'recommendations', Kind.PRIVATE),
    (200, {'status_code': 20, 'status_msg': '该账号未公开喜欢列表'}, 'likes', Kind.PRIVATE),
    (200, {'status_code': 20, 'status_msg': '该账号未公开喜欢列表'}, 'recommendations', Kind.PERMANENT),
    (200, {'status_code': 20, 'status_msg': '该账号未公开推荐列表'}, 'author_profile', Kind.PERMANENT),
    (429, {'status_msg': '该账号未公开推荐列表'}, 'recommendations', Kind.RATE_LIMIT),
    (200, {'status_code': 0, 'message': '请求过于频繁'}, None, Kind.RATE_LIMIT),
    (403, {'status_code': 9, 'message': '请求过于频繁'}, None, Kind.RATE_LIMIT),
    (503, {'message': '请登录'}, None, Kind.TEMPORARY),
    (200, {'status_code': 2, 'status_msg': '系统繁忙'}, None, Kind.TEMPORARY),
    (403, None, None, Kind.PERMANENT),
    (404, {'message': '请登录'}, None, Kind.PERMANENT),
    (200, {'status_code': 99999}, None, Kind.PERMANENT),
    (200, None, None, Kind.MALFORMED),
    (200, [], None, Kind.MALFORMED),
    (200, {'status_code': 0}, 'author_profile', Kind.MALFORMED),
    (200, {'status_code': 0, 'aweme_detail': []}, 'video_detail', Kind.MALFORMED),
    (200, {'status_code': 0, 'user_list': [], 'has_more': None}, 'user_search', Kind.MALFORMED),
    (200, {'status_code': 0, 'user': {}}, 'author_profile', Kind.SUCCESS),
])
def test_response_classification_uses_explicit_evidence(status, data, category, expected):
    assert classify_http(status, data, category=category).kind == expected


@asynccontextmanager
async def harness(tmp_path, handler):
    instance = adapter(tmp_path / 'profile', handler)
    clock = Clock()
    instance.policy = policy(tmp_path / 'control.json', clock, check_stop=instance.check_stop)
    db = Database(tmp_path / 'test.sqlite3')
    task = CommentTask(db, instance, db.create_task('fixture')['id'])
    try:
        yield instance, task, clock
    finally:
        await instance.__aexit__()


@pytest.mark.parametrize('status', [401, 404, 429, 503])
async def test_http_error_precedence_does_not_parse_body(tmp_path, status):
    response = httpx.Response(status)
    def forbidden_json():
        raise AssertionError('HTTP status must be handled before JSON')
    response.json = forbidden_json
    async with harness(tmp_path, lambda _: response) as (instance, _, __):
        with pytest.raises((LoginRequired, RequestFailure)):
            await instance.http_request('GET', 'https://www.douyin.com/test')
        assert sum(instance.metrics['request_outcomes'].values()) == 1


@pytest.mark.parametrize('initial', [
    httpx.Response(401, text='not json'),
    httpx.Response(200, json={'status_code': 1001, 'status_msg': '请登录'}),
    httpx.Response(200, json={'search_nil_info': {'search_nil_type': 'login'}}),
])
async def test_login_recovery_does_not_record_captcha_or_cooldown(tmp_path, initial):
    responses = iter([initial, httpx.Response(200, json={'status_code': 0})])
    async with harness(tmp_path, lambda _: next(responses)) as (instance, task, clock):
        instance.wait_for_login = AsyncMock()
        instance._manual_prompt = AsyncMock(side_effect=AssertionError('Login is not a captcha'))
        instance._session_checked = instance._cookies_checked = clock.now
        result = await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        assert result == {'status_code': 0}
        assert instance.metrics['http_requests'] == 2
        assert instance.metrics['request_outcomes'] == {'login': 1, 'success': 1}
        instance.wait_for_login.assert_awaited_once()
        instance._manual_prompt.assert_not_awaited()
        assert instance._session_checked is None and instance._cookies_checked is None
        assert not instance.policy.paused
        assert not instance.policy.state.get('events')
        assert not instance.policy.state.get('guard_until')
        assert not instance.policy.state.get('cooldown_until')
        assert instance.policy._level == 0
        assert clock.now == pytest.approx(10001.5)


async def test_repeated_login_requires_no_risk_protection_and_stops_after_three_attempts(tmp_path):
    async with harness(tmp_path, lambda _: httpx.Response(401)) as (instance, task, _):
        instance.wait_for_login = AsyncMock()
        with pytest.raises(ManualActionRequired):
            await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        assert instance.metrics['http_requests'] == instance.wait_for_login.await_count == 3
        assert not instance.policy.state.get('events')


@pytest.mark.parametrize('header,delay', [(None, 60), ('120', 120)])
@pytest.mark.parametrize('status', [429, 403, 200])
async def test_rate_limit_has_one_control_result_and_obeys_retry_after(tmp_path, header, delay, status):
    responses = iter([httpx.Response(status, json={'status_code': 9, 'message': '请求过于频繁'},
                                    headers={'Retry-After': header} if header else {}),
                      httpx.Response(200, json={'status_code': 0})])
    async with harness(tmp_path, lambda _: next(responses)) as (instance, task, clock):
        await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        assert instance.metrics['request_outcomes'] == {'rate_limit': 1, 'success': 1}
        assert instance.policy.metrics['rate_limits'] == 1
        assert instance.policy.metrics['temporary_failures'] == 1
        assert len(instance.policy.state['events']) == 1
        assert clock.now == pytest.approx(10000 + delay)
        assert instance.policy.interval_range == (8, 12)


async def test_second_rate_limit_pauses_task_without_extra_requests(tmp_path):
    async with harness(tmp_path, lambda _: httpx.Response(429)) as (instance, task, clock):
        with pytest.raises(ProtectionPause):
            await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        assert instance.metrics['http_requests'] == 2
        assert len(instance.policy.state['events']) == 2
        assert instance.policy.state['cooldown_until'] >= clock.now + 900
        assert instance.policy.state['halted']


async def test_manual_verification_counts_once_and_cools_down_after_resume(tmp_path):
    responses = iter([httpx.Response(403, json={'verify_check': True}),
                      httpx.Response(200, json={'status_code': 0})])
    async with harness(tmp_path, lambda _: next(responses)) as (instance, task, clock):
        async def manual():
            assert instance.policy.paused
            assert len(instance.policy.state['events']) == 1
            instance.policy.resume()
        instance._manual_prompt = AsyncMock(side_effect=manual)
        await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        instance._manual_prompt.assert_awaited_once()
        assert instance.metrics['request_outcomes'] == {'verification': 1, 'success': 1}
        assert len(instance.policy.state['events']) == 1
        assert clock.now == pytest.approx(10060)
        assert instance.policy.interval_range == (8, 12)


async def test_real_challenge_can_upgrade_a_login_pause_without_duplicate_events(tmp_path):
    async with harness(tmp_path, lambda _: httpx.Response(200)) as (instance, _, __):
        instance.require_manual(challenge=False)
        instance.require_manual(challenge=True)
        instance.require_manual(challenge=True)
        assert instance.policy.state['challenge_pending']
        assert len(instance.policy.state['events']) == 1


async def test_login_recovery_cannot_resume_a_new_captcha_without_manual_action(tmp_path):
    async with harness(tmp_path, lambda _: httpx.Response(200, json={})) as (instance, _, __):
        instance.require_manual(challenge=False)
        entered, release = asyncio.Event(), asyncio.Event()
        async def login():
            entered.set()
            await release.wait()
        async def manual():
            assert instance.policy.paused
            assert instance.policy.state['challenge_pending']
            instance.policy.resume()
        instance.wait_for_login = AsyncMock(side_effect=login)
        instance._manual_prompt = AsyncMock(side_effect=manual)
        pending = asyncio.create_task(instance.wait_for_manual())
        await entered.wait()
        instance.require_manual(challenge=True)
        release.set()
        await pending
        instance._manual_prompt.assert_awaited_once()
        assert len(instance.policy.state['events']) == 1
        assert not instance.policy.paused


async def test_worker_restart_preserves_login_pause_without_creating_captcha(tmp_path):
    handler = lambda _: httpx.Response(200, json={})
    async with harness(tmp_path, handler) as (first, _, __):
        first.require_manual(challenge=False)
    async with harness(tmp_path, handler) as (restarted, task, _):
        restarted.wait_for_login = AsyncMock()
        restarted._manual_prompt = AsyncMock(side_effect=AssertionError('Login is not a captcha'))
        assert restarted.policy.paused
        await restarted.wait_for_manual()
        await task.request(lambda: restarted.http_request('GET', 'https://www.douyin.com/test'))
        restarted.wait_for_login.assert_awaited_once()
        restarted._manual_prompt.assert_not_awaited()
        assert not restarted.policy.state.get('events')
        assert not restarted.policy.state.get('guard_until')
        assert not restarted.policy.state.get('cooldown_until')


@pytest.mark.parametrize('status', [400, 403, 404])
async def test_permanent_error_is_not_retried_or_counted_as_temporary(tmp_path, status):
    async with harness(tmp_path, lambda _: httpx.Response(status, text='private raw payload')) as (instance, task, _):
        with pytest.raises(RequestRejected) as error:
            await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        assert 'private raw payload' not in str(error.value)
        assert instance.metrics['http_requests'] == 1
        assert instance.metrics['request_outcomes'] == {'permanent': 1}
        assert instance.policy.metrics['permanent_failures'] == 1
        assert instance.policy.metrics['temporary_failures'] == 0
        assert instance.policy._level == 0


async def test_local_protocol_error_is_permanent(tmp_path):
    def handler(request):
        raise httpx.LocalProtocolError('private request details')
    async with harness(tmp_path, handler) as (instance, task, _):
        with pytest.raises(RequestRejected):
            await task.request(lambda: instance.http_request('GET', 'https://www.douyin.com/test'))
        assert instance.metrics['http_requests'] == 1
        assert instance._temporary_streak == 0


@pytest.mark.parametrize('network', [False, True])
async def test_five_temporary_failures_span_operations_then_block_all_requests(tmp_path, network):
    def handler(request):
        if network:
            raise httpx.ConnectError('fixture', request=request)
        return httpx.Response(503)
    async with harness(tmp_path, handler) as (instance, task, _):
        call = lambda: instance.http_request('GET', 'https://www.douyin.com/test')
        with pytest.raises(RequestRejected):
            await task.request(call)
        assert instance.metrics['http_requests'] == 3
        with pytest.raises(NetworkPause):
            await task.request(call)
        assert instance.metrics['http_requests'] == 5
        with pytest.raises(NetworkPause):
            await task.request(call)
        assert instance.metrics['http_requests'] == 5
        assert instance.metrics['request_outcomes'] == {'temporary': 5}
        assert not instance.policy.state.get('events')
        assert not instance.policy.state.get('cooldown_until')
        assert not instance.policy.state.get('guard_until')


async def test_valid_response_resets_network_failure_streak(tmp_path):
    responses = iter([httpx.Response(503) for _ in range(4)] +
                     [httpx.Response(200, json={'status_code': 0})] +
                     [httpx.Response(503) for _ in range(3)])
    async with harness(tmp_path, lambda _: next(responses)) as (instance, task, _):
        call = lambda: instance.http_request('GET', 'https://www.douyin.com/test')
        with pytest.raises(RequestRejected):
            await task.request(call)
        await task.request(call)
        assert instance._temporary_streak == 0
        with pytest.raises(RequestRejected):
            await task.request(call)
        assert instance._temporary_streak == 3 and not instance._network_paused


async def test_late_success_cannot_clear_a_new_failure_streak(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={})
    async with harness(tmp_path, handler) as (instance, _, __):
        pending = asyncio.create_task(instance.http_request('GET', 'https://www.douyin.com/test'))
        await entered.wait()
        from backend.responses import TEMPORARY
        with pytest.raises(RequestFailure):
            instance._complete_outcome(TEMPORARY, instance.policy.generation)
        release.set()
        await pending
        assert instance._temporary_streak == 1
        assert instance.policy.metrics['stale_successes'] == 1


async def test_bad_page_cannot_accelerate_before_backoff_or_double_count(tmp_path):
    async with harness(tmp_path, lambda _: httpx.Response(200, json={'status_code': 0})) as (instance, task, _):
        for _ in range(9):
            instance.policy.success(instance.policy.generation)
        async def invalid_page():
            await instance.http_request('GET', 'https://www.douyin.com/test')
            raise PageFailure('missing pagination fields')
        with pytest.raises(RequestRejected):
            await task.request(invalid_page)
        assert instance.metrics['request_outcomes'] == {'malformed': 3}
        assert instance.policy.metrics['temporary_failures'] == 3
        assert instance.policy._level == 3
        assert instance._temporary_streak == 0
        assert not any(change['reason'] == 'success_acceleration' for change in instance.policy.summary()['speed_changes'])


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
@pytest.mark.parametrize('status', [200, 403])
async def test_private_list_does_not_slow_next_worker(tmp_path, kind, status):
    fixture = Fixture(kind)
    label = '喜欢' if kind == 'likes' else '推荐'
    fixture.pages = {0: httpx.Response(status, json={'status_code': 20, 'status_msg': f'该账号未公开{label}列表', 'aweme_list': []})}
    db = Database(tmp_path / 'test.sqlite3')
    task = create(db, kind, verify_authors=False)
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    assert db.get_task(task['id'])['status'] == 'private'
    assert db.get_task(task['id'])['error'] == f'该账号未公开{label}列表'
    assert runtime.transport.metrics['request_outcomes']['private'] == 1
    assert runtime.transport.policy.metrics['temporary_failures'] == 0
    assert runtime.transport.policy._level == 0
    next_policy = policy(runtime.transport.policy.path, runtime.transport.clock)
    assert next_policy.interval_range == (1.5, 2.5)
    assert not next_policy.state.get('events')


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('status,attempts', [(404, 1), (503, 3), (200, 3)])
async def test_transport_failure_does_not_repeat_operation_via_cursor_replay(tmp_path, kind, status, attempts):
    db = Database(tmp_path / 'test.sqlite3')
    task = create(db, kind, verify_authors=False)
    fixture = Fixture(kind)
    checkpoint = '' if kind == 'comments' else kind
    if kind == 'comments':
        db.update_task(task['id'], title='fixture')
        db.save_page(task['id'], '', [], 20, False)
        fixture.main = {20: httpx.Response(status, json={})}
    else:
        db.update_task(task['id'], target_sec_uid='target_sec')
        db.save_likes_page(task['id'], [], 100, False, checkpoint_key=kind)
        fixture.pages = {100: httpx.Response(status, json={})}
    before = db.get_checkpoint(task['id'], checkpoint)
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    assert db.get_checkpoint(task['id'], checkpoint) == before
    assert db.get_task(task['id'])['status'] == 'partial'
    category = 'comments' if kind == 'comments' else kind
    assert runtime.transport.metrics['http_request_categories'][category] == attempts


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
async def test_author_network_pause_preserves_saved_results_and_checkpoint(tmp_path, kind):
    db = Database(tmp_path / 'test.sqlite3')
    task = create(db, kind)
    tid = task['id']
    authors = [{'uid': f'uid{i}', 'sec_uid': f'sec{i}', 'nickname': '模拟作者'} for i in range(3)]
    if kind == 'comments':
        db.save_page(tid, '', [normalize_comment(comment(str(i), user=author), '123') for i, author in enumerate(authors)], 40, False)
        count_key, checkpoint = 'main_count', ''
    else:
        db.save_likes_page(tid, [normalize_liked_video(video(str(i + 1), author=author)) for i, author in enumerate(authors)], 40, False, checkpoint_key=kind)
        count_key, checkpoint = 'video_count', kind
    before = db.get_checkpoint(tid, checkpoint)
    args = args_for(tmp_path, task)
    args.verify_only = True
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=lambda request: httpx.Response(503)))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    result = db.get_task(tid)
    assert result['status'] == 'paused' and '连续 5 次' in result['error']
    assert result[count_key] == 3
    assert db.get_checkpoint(tid, checkpoint) == before
    assert runtime.transport.metrics['http_requests'] == 5
    assert runtime.transport.metrics['request_outcomes'] == {'temporary': 5}
    assert not runtime.transport.policy.state.get('events')


async def test_cancellation_during_response_scope_keeps_outcomes_uncommitted(tmp_path):
    entered = asyncio.Event()
    async with harness(tmp_path, lambda _: httpx.Response(200, json={})) as (instance, task, _):
        async def normalize():
            await instance.http_request('GET', 'https://www.douyin.com/test')
            entered.set()
            await asyncio.Event().wait()
        pending = asyncio.create_task(task.request(normalize))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert instance.metrics['request_outcomes'] == {}
        assert instance._response_scope.get() is None
        assert instance._temporary_streak == 0
