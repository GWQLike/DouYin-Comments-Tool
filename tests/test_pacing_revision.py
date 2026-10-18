"""Compare the revised pacing with the immediately preceding 2–4 second warmup."""
import json
import random

import httpx
import pytest

from backend.crawler import ControlledStop
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.stable_policy import StableRequestPolicy
from test_backoff_recovery import RecoveryFixture
from test_crawler import comment, page
from test_engine_integration import SimulatedTransport, args_for, create, exported, fixture_signing
from test_likes_collector import video, likes
from test_stable_policy import Clock, policy
from test_transport_speed import adapter


class PreviousPacing(StableRequestPolicy):
    NORMAL_RANGES = ((2, 4), (1.5, 2.5), (1, 2))
    ACCELERATION_SUCCESSES = 20


class PacingTransport(SimulatedTransport):
    def __init__(self, *args, fixture, baseline, latency):
        super().__init__(*args, fixture=fixture)
        self.latency = latency
        cls = PreviousPacing if baseline else StableRequestPolicy
        self.policy = cls(self.profile.parent / 'control.json', clock=self.clock, wall_clock=self.clock,
                          sleep=self.clock.sleep, uniform=random.Random(731).uniform,
                          check_stop=self.check_stop)

    async def http_request(self, *args, **kwargs):
        result = await super().http_request(*args, **kwargs)
        await self.clock.sleep(self.latency)
        return result


@pytest.mark.parametrize('kind,pages,latency', [
    ('comments', 10, .3), ('comments', 54, .3), ('comments', 83, .3),
    ('likes', 54, .3), ('recommendations', 54, .3), ('comments', 30, 3),
])
async def test_same_results_fewer_waits_and_one_request_at_a_time(tmp_path, kind, pages, latency):
    samples = []
    for baseline in (True, False):
        directory = tmp_path / ('before' if baseline else 'after')
        db = Database(directory / 'test.db')
        task = create(db, kind, verify_authors=True)
        db.update_task(task['id'], title='测试')
        fixture = RecoveryFixture(kind, 'unused')
        fixture.failed = True
        fixture.main = {i * 20: page([comment(str(i))], (i + 1) * 20, int(i + 1 < pages)) for i in range(pages)}
        fixture.pages = {i * 20: likes([video(str(i + 1))], (i + 1) * 20, int(i + 1 < pages)) for i in range(pages)}
        runtime = WorkbenchRuntime(db, args_for(directory, task), lambda: False, lambda *args: None,
            transport_factory=lambda *args: PacingTransport(*args, fixture=fixture, baseline=baseline, latency=latency))
        await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
        state = db.get_task(task['id'])
        assert state['status'] == 'completed' and state['collection_complete']
        assert state['verification_complete']
        assert state['main_count'] + state['video_count'] == pages
        assert runtime.transport.metrics['http_peak_active'] == 1
        samples.append((exported(db, task['id'], kind), {
            'seconds': round(runtime.transport.clock.now - 10000, 3),
            'requests': runtime.transport.metrics['http_requests'],
        }))
    assert samples[0][0] == samples[1][0]
    before, after = [item[1] for item in samples]
    assert before['requests'] == after['requests']
    assert after['seconds'] <= before['seconds']
    if latency == .3:
        assert after['seconds'] <= before['seconds'] * .72
    report = {'kind': kind, 'pages': pages, 'latency': latency, 'before': before, 'after': after,
              'txt_identical': True, 'time_basis': 'virtual network and scheduler clock'}
    (tmp_path / 'pacing-benchmark.json').write_text(json.dumps(report), encoding='utf-8')
    print('PACING_BENCHMARK ' + json.dumps(report))


@pytest.mark.parametrize('signed', [False, True])
async def test_exhausted_budget_skips_wait_and_signature_but_preserves_last_response(tmp_path, signed):
    calls = []
    instance = adapter(tmp_path / 'profile', lambda _: calls.append(True) or httpx.Response(200, json={}))
    clock = Clock()
    instance.policy = policy(tmp_path / 'control.json', clock, check_stop=instance.check_stop)
    instance.max_requests = 1
    try:
        assert await instance.http_request('GET', 'https://www.douyin.com/test') == {}
        assert instance.metrics['request_outcomes'] == {'success': 1}
        before = clock.now
        if signed:
            from backend.douyin_adapter import load_upstream_client, pooled_client_type
            client = pooled_client_type(load_upstream_client(), instance)(headers={}, playwright_page=None, cookie_dict={})
            call = lambda: client.get('/test', {})
        else:
            call = lambda: instance.http_request('GET', 'https://www.douyin.com/test')
        with pytest.raises(ControlledStop):
            await call()
        assert clock.now == before
        assert instance.policy.metrics['request_starts'] == 1
        assert instance.metrics['signature_seconds'] == 0 and len(calls) == 1
    finally:
        await instance.__aexit__()


async def test_stop_during_faster_wait_sends_no_next_request(tmp_path):
    import asyncio
    from backend.crawler import StopRequested
    clock = Clock()
    stopped = False
    def stop():
        if stopped:
            raise StopRequested()
    p = policy(tmp_path / 'control.json', clock, check_stop=stop)
    for _ in range(20):
        p.success(p.generation)
    assert p.interval_range == (.8, 1.2)
    await p.wait_turn()
    pending = asyncio.create_task(p.wait_turn())
    await asyncio.sleep(0)
    stopped = True
    with pytest.raises(StopRequested):
        await pending
    assert p.metrics['request_starts'] == 1
