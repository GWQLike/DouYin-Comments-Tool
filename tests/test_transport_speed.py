import asyncio
import threading
import time
from urllib.parse import parse_qs
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.crawler import ManualActionRequired, PageFailure, StopRequested
from backend.douyin_adapter import DouyinAdapter, load_upstream_client, pooled_client_type
from backend.request_policy import RequestPolicy


def adapter(tmp_path, handler):
    instance = DouyinAdapter(tmp_path, lambda: False, lambda *args: None)
    instance.policy = RequestPolicy(base_interval=0, check_stop=instance.check_stop)
    instance.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    instance.client = SimpleNamespace(timeout=25)
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error,paused', [(401, ManualActionRequired, True),
    (403, PageFailure, False), (429, PageFailure, False), (503, PageFailure, False)])
async def test_http_status_checked_before_json(tmp_path, status, error, paused):
    instance = adapter(tmp_path, lambda _: httpx.Response(status, text='not json', headers={'Retry-After':'7'}))
    with pytest.raises(error):
        await instance.http_request('GET', 'https://www.douyin.com/test')
    assert instance.policy.paused is paused
    assert instance.metrics['http_requests'] == 1
    if status == 429:
        assert instance.policy.summary()['rate_limits'] == 1
    await instance.__aexit__()
    assert instance.http_client.is_closed


@pytest.mark.asyncio
async def test_business_challenge_pauses_before_collector_sees_response(tmp_path):
    instance = adapter(tmp_path, lambda _: httpx.Response(200, json={'status_code':0, 'verify_check':True}))
    with pytest.raises(ManualActionRequired):
        await instance.http_request('GET', 'https://www.douyin.com/test')
    assert instance.policy.paused
    await instance.__aexit__()


@pytest.mark.asyncio
async def test_private_list_response_preserved_for_specific_collector(tmp_path):
    payload = {'status_code':20, 'status_msg':'该账号未公开推荐列表', 'aweme_list':[]}
    instance = adapter(tmp_path, lambda _: httpx.Response(200, json=payload))
    assert await instance.http_request('GET', 'https://www.douyin.com/test') == payload
    assert not instance.policy.paused
    await instance.__aexit__()


@pytest.mark.asyncio
async def test_manual_prompt_is_shared_and_blocks_queued_requests(tmp_path):
    calls = []
    instance = adapter(tmp_path, lambda request: calls.append(request) or httpx.Response(200, json={}))
    entered, resume = asyncio.Event(), asyncio.Event()
    prompts = 0
    async def prompt():
        nonlocal prompts
        prompts += 1
        entered.set()
        await resume.wait()
        instance.policy.resume()
    instance._manual_prompt = prompt
    instance.require_manual()
    manual = [asyncio.create_task(instance.wait_for_manual()) for _ in range(3)]
    queued = asyncio.create_task(instance.http_request('GET', 'https://www.douyin.com/test'))
    await entered.wait()
    assert not calls
    resume.set()
    await asyncio.gather(*manual, queued)
    await instance.wait_for_manual()  # A concurrent caller arriving late reuses it.
    assert prompts == 1 and len(calls) == 1
    await instance.__aexit__()


@pytest.mark.asyncio
async def test_session_and_cookie_cache_deduplicates_concurrent_checks(tmp_path):
    instance = adapter(tmp_path, lambda _: httpx.Response(200, json={}))
    instance.client = SimpleNamespace(pong=AsyncMock(return_value=True), update_cookies=AsyncMock(),
        cookie_dict={'UIFID':'test'}, headers={})
    await asyncio.gather(*(instance.ensure_session() for _ in range(12)))
    assert instance.client.pong.await_count == instance.client.update_cookies.await_count == 1
    await instance.wait_for_login()
    assert instance.client.pong.await_count == instance.client.update_cookies.await_count == 2
    assert instance.client.headers['uifid'] == 'test'
    await instance.__aexit__()


@pytest.mark.asyncio
async def test_signed_requests_copy_inputs_and_keep_headers_isolated(tmp_path, monkeypatch):
    upstream = load_upstream_client()
    import importlib
    helpers = importlib.import_module('media_platform.douyin.help')
    signed = []
    loop_thread = threading.get_ident()
    def sign(uri, query, agent):
        signed.append((query, agent, threading.get_ident()))
        return 'test-signature'
    monkeypatch.setattr(helpers, 'get_a_bogus_from_js', sign)
    requests = []
    async def handler(request):
        requests.append(request)
        await asyncio.sleep(.005)
        return httpx.Response(200, json={'status_code':0})
    instance = adapter(tmp_path, handler)
    instance.sign_executor = ThreadPoolExecutor(max_workers=1)
    instance.ensure_session = AsyncMock()
    page = SimpleNamespace(evaluate=AsyncMock(return_value={'xmst':'token'}))
    client_type = pooled_client_type(upstream, instance)
    instance.client = client_type(headers={'User-Agent':'agent', 'Cookie':'cookie', 'Origin':'origin'},
        playwright_page=page, cookie_dict={})
    values = {'count':10}
    await asyncio.gather(instance.invoke_with_referer('https://www.douyin.com/user/A', 'get', '/first', values),
                         instance.invoke_with_referer('https://www.douyin.com/user/B', 'get', '/second', values))
    assert values == {'count':10}
    assert [item.headers['Referer'] for item in requests] == [
        'https://www.douyin.com/user/A', 'https://www.douyin.com/user/B']
    assert all(item.url.params['a_bogus'] == 'test-signature' for item in requests)
    assert all(item[1] == 'agent' and item[2] != loop_thread for item in signed)
    assert len({item[2] for item in signed}) == 1
    assert all('a_bogus=' not in item[0] for item in signed)
    await instance.__aexit__()


@pytest.mark.asyncio
@pytest.mark.parametrize('cookies', [
    {'UIFID': 'fresh-user', 's_v_web_id': 'fresh-fingerprint'},
    {'UIFID_TEMP': 'fresh-temp', 's_v_web_id': 'fresh-fingerprint'},
    {},
])
async def test_queued_video_detail_uses_one_fresh_cookie_snapshot(tmp_path, monkeypatch, cookies):
    upstream = load_upstream_client()
    import importlib
    helpers = importlib.import_module('media_platform.douyin.help')
    signed = []
    def sign(uri, query, agent):
        signed.append(parse_qs(query, keep_blank_values=True))
        return 'fresh-signature'
    monkeypatch.setattr(helpers, 'get_a_bogus_from_js', sign)
    requests = []
    instance = adapter(tmp_path, lambda request: requests.append(request) or
        httpx.Response(200, json={'status_code': 0, 'aweme_detail': {'aweme_id': '123'}}))
    instance.sign_executor = ThreadPoolExecutor(max_workers=1)
    page = SimpleNamespace(evaluate=AsyncMock(return_value={'xmst': 'token'}))
    client_type = pooled_client_type(upstream, instance)
    instance.client = client_type(
        headers={'User-Agent': 'agent', 'Cookie': 'old-cookie', 'Origin': 'origin'},
        playwright_page=page, cookie_dict={'UIFID': 'old-user', 's_v_web_id': 'old-fingerprint'})
    originals = []
    original_get = instance.client.get
    async def capture_get(uri, params=None, headers=None):
        originals.append((params, headers))
        return await original_get(uri, params, headers)
    instance.client.get = capture_get
    fresh_cookie = '; '.join(f'{key}={value}' for key, value in cookies.items())
    fresh_user = cookies.get('UIFID') or cookies.get('UIFID_TEMP', '')
    async def refresh_session():
        # The upstream detail method has already copied the old session fields.
        assert originals[0][0]['uifid'] == 'old-user'
        instance.client.cookie_dict = dict(cookies)
        instance.client.headers['Cookie'] = fresh_cookie
        if fresh_user:
            instance.client.headers['uifid'] = fresh_user
        else:
            instance.client.headers.pop('uifid', None)
    instance.ensure_session = refresh_session
    try:
        assert await instance.client.get_video_by_id('123') == {'aweme_id': '123'}
        assert len(requests) == len(signed) == 1
        request = requests[0]
        assert request.headers['Cookie'] == fresh_cookie
        assert request.headers.get('uifid', '') == fresh_user
        assert 'Origin' not in request.headers
        for key, expected in {'uifid': fresh_user,
                              'verifyFp': cookies.get('s_v_web_id', ''),
                              'fp': cookies.get('s_v_web_id', '')}.items():
            assert request.url.params[key] == expected
            assert signed[0][key] == [expected]
        assert request.url.params['a_bogus'] == 'fresh-signature'
        assert originals[0][0] == {'aweme_id': '123', 'uifid': 'old-user',
                                   'verifyFp': 'old-fingerprint', 'fp': 'old-fingerprint'}
        assert originals[0][1]['Cookie'] == 'old-cookie'
    finally:
        await instance.__aexit__()


@pytest.mark.asyncio
async def test_stop_cancels_active_http_and_closes_pool(tmp_path):
    active, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(request):
        active.set()
        try:
            await asyncio.sleep(20)
        finally:
            cancelled.set()
    instance = adapter(tmp_path, handler)
    task = asyncio.create_task(instance.http_request('GET', 'https://www.douyin.com/test'))
    await active.wait()
    instance.stop = lambda: True
    with pytest.raises(StopRequested):
        await asyncio.wait_for(task, .5)
    assert cancelled.is_set()
    await instance.__aexit__()
    assert instance.http_client.is_closed


@pytest.mark.asyncio
async def test_permanent_http_error_breaks_success_streak_without_backoff(tmp_path):
    instance = adapter(tmp_path, lambda request: httpx.Response(404, text='not json'))
    instance.policy = RequestPolicy(time_scale=.01)
    old_generation = instance.policy.generation
    for _ in range(19):
        instance.policy.success(generation=old_generation)
    try:
        with pytest.raises(PageFailure, match='404'):
            await instance.http_request('GET', 'https://www.douyin.com/test')
        assert instance.policy.generation != old_generation
        for _ in range(20):
            instance.policy.success(generation=old_generation)
        instance.policy.success(generation=instance.policy.generation)
        assert instance.policy.interval == pytest.approx(.005)
        assert instance.policy.summary()['permanent_failures'] == 1
        assert instance.policy.summary()['temporary_failures'] == 0
        for _ in range(19):
            instance.policy.success(generation=instance.policy.generation)
        assert instance.policy.interval == pytest.approx(.004)
    finally:
        await instance.__aexit__()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['failure', 'verification'])
async def test_old_http_success_cannot_undo_new_slowdown(tmp_path, reason):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={'status_code': 0})
    instance = adapter(tmp_path, handler)
    instance.policy = RequestPolicy(time_scale=.01)
    pending = asyncio.create_task(instance.http_request('GET', 'https://www.douyin.com/test'))
    try:
        await entered.wait()
        if reason == 'failure':
            instance.policy.failure()
        else:
            instance.require_manual()
            instance.policy.resume()
        for _ in range(9):
            instance.policy.success(generation=instance.policy.generation)
        assert instance.policy.interval == pytest.approx(.02)
        release.set()
        assert await pending == {'status_code': 0}
        assert instance.policy.interval == pytest.approx(.02)
        instance.policy.success(generation=instance.policy.generation)
        assert instance.policy.interval == pytest.approx(.005)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await instance.__aexit__()


@pytest.mark.asyncio
@pytest.mark.parametrize('destination', ['video', 'loop', 'foreign'])
async def test_short_url_uses_shared_gate_without_extra_sleep(tmp_path, destination):
    starts = []
    async def handler(request):
        starts.append(time.monotonic())
        if destination == 'foreign':
            target = 'https://example.com/private-path'
        elif destination == 'video' and len(starts) == 2:
            target = 'https://www.douyin.com/video/123456789'
        else:
            target = f'https://v.douyin.com/next{len(starts)}'
        return httpx.Response(302, headers={'Location': target})
    instance = adapter(tmp_path, handler)
    instance.policy = RequestPolicy(base_interval=.01)
    instance.ensure_session = AsyncMock()
    instance.sleep = AsyncMock(side_effect=AssertionError('Unexpected fixed redirect delay'))
    client_type = pooled_client_type(load_upstream_client(), instance)
    instance.client = client_type(headers={}, playwright_page=None, cookie_dict={})
    try:
        if destination == 'video':
            assert await instance.resolve_video_id('https://v.douyin.com/start') == '123456789'
            assert len(starts) == 2
        else:
            with pytest.raises(PageFailure):
                await instance.resolve_video_id('https://v.douyin.com/start')
            assert len(starts) == (1 if destination == 'foreign' else 5)
        assert all(b - a >= .009 for a, b in zip(starts, starts[1:]))
        instance.sleep.assert_not_awaited()
        assert instance.metrics['http_request_categories'] == {'short_url': len(starts)}
        assert instance.metrics['http_peak_active'] == 1
        assert instance._http_active == 0
        assert 'private-path' not in str(instance.performance_summary())
    finally:
        await instance.__aexit__()


@pytest.mark.asyncio
async def test_http_metrics_count_actual_sends_without_sensitive_urls(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    running = 0
    async def handler(request):
        nonlocal running
        running += 1
        if running == 3:
            entered.set()
        await release.wait()
        return httpx.Response(200, json={'status_code': 0})
    instance = adapter(tmp_path, handler)
    async def request(path):
        async with instance.policy.slot():
            return await instance.http_request('GET', f'https://www.douyin.com{path}?secret=private-token')
    jobs = [asyncio.create_task(request(path)) for path in [
        '/aweme/v1/web/comment/list/', '/aweme/v1/web/comment/list/reply/',
        '/aweme/v1/web/familiar/recommend/feed/']]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert instance.metrics['http_peak_active'] == 3
        release.set()
        await asyncio.gather(*jobs)
        assert instance._http_active == 0
        summary = instance.performance_summary()
        assert summary['http_request_categories'] == {'comments': 1, 'replies': 1, 'recommendations': 1}
        assert 'private-token' not in str(summary)
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await instance.__aexit__()
