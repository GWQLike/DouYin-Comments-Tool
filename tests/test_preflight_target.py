"""Collection preflight and discovery hints through the production engine."""
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import WorkerManager, create_app
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.likes import normalize_liked_video
from backend.preflight import PreflightRejected
from backend.responses import Kind, LoginRequired, classify_http
from backend.upstream import module
from test_engine_integration import Fixture, SimulatedTransport, args_for, create
from test_likes_collector import likes, profile, users, video
from test_search_api import FakeManager


@pytest.mark.parametrize('bounds', [
    {'start': '2026-10-01'}, {'end': '2026-10-21'},
    {'start': '2026-10-01', 'end': '2026-10-21'},
])
def test_new_likes_time_bounds_rejected_without_task_or_worker(tmp_path, bounds):
    app = create_app(tmp_path, manager_factory=FakeManager)
    with TestClient(app) as client:
        app.state.manager.start = Mock(side_effect=AssertionError('must not start'))
        response = client.post('/api/likes/tasks', json={'account': 'target.user', **bounds})
        assert response.status_code == 400
        assert '点赞时间' in response.json()['detail']
        assert client.get('/api/tasks').json() == []
        app.state.manager.start.assert_not_called()


@pytest.mark.parametrize('action', ['start', 'refresh'])
@pytest.mark.parametrize('bound', ['start_time', 'end_time'])
def test_historical_likes_preflight_preserves_results_and_checkpoint(tmp_path, action, bound):
    app = create_app(tmp_path, manager_factory=FakeManager)
    with TestClient(app) as client:
        db = app.state.db
        task = create(db, 'likes', **{bound: 0})
        db.save_likes_page(task['id'], [normalize_liked_video(video())], 100, False)
        db.update_task(task['id'], status='partial', error='原采集错误')
        before, rows, checkpoint = db.get_task(task['id']), db.read_liked_videos(task['id']), db.get_checkpoint(task['id'], 'likes')
        app.state.manager.start = Mock(side_effect=AssertionError('must not start'))
        assert client.post(f"/api/tasks/{task['id']}/{action}").status_code == 400
        assert db.get_task(task['id']) == before
        assert db.read_liked_videos(task['id']) == rows
        assert db.get_checkpoint(task['id'], 'likes') == checkpoint
        assert len(db.list_tasks()) == 1
        app.state.manager.start.assert_not_called()


def test_direct_worker_manager_checks_before_browser_or_state_changes(tmp_path):
    db = Database(tmp_path / 'test.db')
    task = create(db, 'likes', start_time=0)
    manager = WorkerManager(db, tmp_path)
    manager.browser.ensure = Mock(side_effect=AssertionError('must not open browser'))
    with pytest.raises(HTTPException) as caught:
        manager.start(task['id'])
    assert caught.value.status_code == 400
    manager.browser.ensure.assert_not_called()
    assert db.get_task(task['id']) == task


async def test_engine_preflight_before_transport_and_upstream_lifecycle(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.db')
    task = create(db, 'likes', end_time=0)
    forbidden = Mock(side_effect=AssertionError('must not load engine or transport'))
    monkeypatch.setattr('backend.engine.module', forbidden)
    monkeypatch.setattr('backend.engine.WorkbenchRuntime', forbidden)
    with pytest.raises(PreflightRejected, match='点赞时间'):
        await run_engine(db, args_for(tmp_path, task), lambda: False, lambda *args: None)
    forbidden.assert_not_called()
    assert db.get_task(task['id']) == task


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    monkeypatch.setattr(module('media_platform.douyin.help'), 'get_a_bogus_from_js', lambda *args: 'fixture-signature')


class DiscoveryFixture(Fixture):
    def __init__(self, kind, old_response):
        super().__init__(kind)
        self.old_response = old_response
        self.search_response = users([profile()['user']])
        self.pages = {0: likes([video('2')])}

    def __call__(self, request):
        path, params = request.url.path, request.url.params
        if path.endswith('/profile/other/') and params['sec_user_id'] == 'old_sec':
            self.requests.append(request)
            if isinstance(self.old_response, Exception):
                raise self.old_response
            return self.old_response if isinstance(self.old_response, httpx.Response) else httpx.Response(200, json=self.old_response)
        if path.endswith('/discover/search/'):
            self.requests.append(request)
            return httpx.Response(200, json=self.search_response)
        return super().__call__(request)


def seed_history(db):
    old = create(db, 'comments')
    from backend.crawler import normalize_comment
    from test_crawler import comment
    db.save_page(old['id'], '', [normalize_comment(comment('hint', user={
        'uid': 'old_uid', 'sec_uid': 'old_sec', 'unique_id': 'target.user',
    }), '123456789')], 1, True)
    # A confirmed historical identity is only a discovery hint for this run.
    assert db.cached_account_profiles('target.user')


async def collect(db, task, fixture, tmp_path, *, verify=False):
    args = args_for(tmp_path, task)
    args.verify_only = verify
    args.stop_on_challenge = True
    args.max_requests = 30
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    return runtime.transport


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
@pytest.mark.parametrize('old_response', [
    httpx.Response(404), httpx.Response(410),
    profile('renamed.account', 'old_sec', 'old_uid'),
    profile('target.user', 'different_sec', 'different_uid'),
])
async def test_invalid_history_falls_back_to_exact_search_and_new_profile(tmp_path, kind, old_response):
    db = Database(tmp_path / 'test.db')
    seed_history(db)
    task = create(db, kind, verify_authors=False)
    fixture = DiscoveryFixture(kind, old_response)
    transport = await collect(db, task, fixture, tmp_path)
    actual = db.get_task(task['id'])
    assert actual['status'] == 'completed' and actual['target_sec_uid'] == 'target_sec'
    assert [row['video_id'] for row in db.read_liked_videos(task['id'])] == ['2']
    assert transport.metrics['http_request_categories'] == {'author_profile': 2, 'user_search': 1, kind: 1}
    assert not transport.policy.state.get('events')
    assert transport.policy.state['level'] == 0


@pytest.mark.parametrize('failure, attempts, status, protected', [
    (httpx.Response(503), 3, 'partial', False),
    (httpx.ConnectError('network unavailable'), 3, 'partial', False),
    (httpx.Response(403), 1, 'partial', False),
    (httpx.Response(200, json={'status_code': 0, 'user': None}), 3, 'partial', False),
    (httpx.Response(200, json={'verify_check': True}), 1, 'paused', True),
    (httpx.Response(429), 2, 'paused', True),
])
async def test_history_fault_never_triggers_search_or_collection(tmp_path, failure, attempts, status, protected):
    db = Database(tmp_path / 'test.db')
    seed_history(db)
    task = create(db, 'recommendations', verify_authors=False)
    fixture = DiscoveryFixture('recommendations', failure)
    transport = await collect(db, task, fixture, tmp_path)
    actual = db.get_task(task['id'])
    assert actual['status'] == status and actual['error']
    assert not actual['target_sec_uid'] and actual['video_count'] == 0
    assert transport.metrics['http_request_categories'] == {'author_profile': attempts}
    assert bool(transport.policy.state.get('events')) == protected


@pytest.mark.parametrize('failure', [httpx.Response(401), httpx.Response(200, json={'status_msg': '请登录'})])
async def test_history_login_failure_waits_without_becoming_a_challenge(tmp_path, monkeypatch, failure):
    async def not_logged_in(self):
        raise LoginRequired('请重新登录')
    monkeypatch.setattr(SimulatedTransport, 'wait_for_manual', not_logged_in)
    db = Database(tmp_path / 'test.db')
    seed_history(db)
    task = create(db, 'likes', verify_authors=False)
    fixture = DiscoveryFixture('likes', failure)
    transport = await collect(db, task, fixture, tmp_path)
    assert db.get_task(task['id'])['status'] == 'waiting_login'
    assert transport.metrics['http_request_categories'] == {'author_profile': 1}
    assert not transport.policy.state.get('events')
    assert not db.get_task(task['id'])['target_sec_uid']


async def test_valid_history_is_reverified_before_using_it(tmp_path):
    db = Database(tmp_path / 'test.db')
    seed_history(db)
    task = create(db, 'likes', verify_authors=False)
    fixture = DiscoveryFixture('likes', profile('target.user', 'old_sec', 'old_uid', show_favorite_list=True))
    transport = await collect(db, task, fixture, tmp_path)
    assert db.get_task(task['id'])['target_sec_uid'] == 'old_sec'
    assert db.get_task(task['id'])['status'] == 'completed'
    assert transport.metrics['http_request_categories'] == {'author_profile': 1, 'likes': 1}


async def test_exact_search_candidate_must_match_current_profile(tmp_path):
    db = Database(tmp_path / 'test.db')
    task = create(db, 'likes', verify_authors=False)
    fixture = DiscoveryFixture('likes', profile('renamed.account', 'old_sec', 'old_uid'))
    fixture.search_response = users([profile('target.user', 'old_sec', 'old_uid')['user']])
    transport = await collect(db, task, fixture, tmp_path)
    assert '未能核验到完全匹配' in db.get_task(task['id'])['error']
    assert transport.metrics['http_request_categories'] == {'user_search': 1, 'author_profile': 1}


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
@pytest.mark.parametrize('old_response', [httpx.Response(404), profile('renamed.account', 'old_sec', 'old_uid')])
async def test_resume_missing_or_reassigned_target_keeps_original_results(tmp_path, kind, old_response):
    db = Database(tmp_path / 'test.db')
    task = create(db, kind, verify_authors=False)
    db.update_task(task['id'], target_sec_uid='old_sec')
    db.save_likes_page(task['id'], [normalize_liked_video(video('1'))], 100, False, checkpoint_key=kind)
    rows, checkpoint = db.read_liked_videos(task['id']), db.get_checkpoint(task['id'], kind)
    fixture = DiscoveryFixture(kind, old_response)
    transport = await collect(db, task, fixture, tmp_path)
    actual = db.get_task(task['id'])
    assert actual['status'] == 'partial' and '新建任务' in actual['error']
    assert actual['target_sec_uid'] == 'old_sec'
    assert db.read_liked_videos(task['id']) == rows and db.get_checkpoint(task['id'], kind) == checkpoint
    assert transport.metrics['http_request_categories'] == {'author_profile': 1}


async def test_search_never_uses_nickname_or_internal_id_as_account(tmp_path):
    db = Database(tmp_path / 'test.db')
    task = create(db, 'likes', verify_authors=False)
    fixture = DiscoveryFixture('likes', httpx.Response(404))
    fixture.search_response = users([
        profile('other', nickname='target.user')['user'],
        profile('', uid='target.user')['user'],
    ])
    transport = await collect(db, task, fixture, tmp_path)
    assert '未能核验到完全匹配' in db.get_task(task['id'])['error']
    assert transport.metrics['http_request_categories'] == {'user_search': 1}


async def test_historical_time_bounds_still_allow_saved_author_verification(tmp_path):
    db = Database(tmp_path / 'test.db')
    task = create(db, 'likes', start_time=1)
    db.save_likes_page(task['id'], [normalize_liked_video(video(author={
        'uid': 'author_uid', 'sec_uid': 'author_sec', 'nickname': '原作者',
    }))], 100, False)
    db.update_task(task['id'], status='partial', error='原采集错误')
    checkpoint = db.get_checkpoint(task['id'], 'likes')
    transport = await collect(db, task, Fixture('likes'), tmp_path, verify=True)
    assert db.get_task(task['id'])['verification_complete']
    assert db.get_task(task['id'])['error'] == '原采集错误'
    assert db.read_liked_videos(task['id'])[0]['author_douyin_id'] == 'author.one'
    assert db.get_checkpoint(task['id'], 'likes') == checkpoint
    assert transport.metrics['http_request_categories'] == {'author_profile': 1}


@pytest.mark.parametrize('status, category, expected', [
    (404, 'author_profile', True), (410, 'author_profile', True),
    (404, 'recommendations', False), (403, 'author_profile', False),
    (503, 'author_profile', False), (401, 'author_profile', False),
])
def test_only_missing_profile_http_status_invalidates_discovery_hint(status, category, expected):
    outcome = classify_http(status, None, category=category)
    assert outcome.resource_missing is expected
    if expected:
        assert outcome.kind == Kind.PERMANENT
