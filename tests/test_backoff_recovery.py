"""Recovery after an isolated error, including persisted pre-fix state."""
import json

import httpx
import pytest

from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.stable_policy import StableRequestPolicy
from test_crawler import comment, page
from test_engine_integration import Fixture, SimulatedTransport, args_for, create, exported, fixture_signing
from test_likes_collector import video, likes
from test_stable_policy import Clock, policy


def succeed(p, count):
    for _ in range(count):
        p.success(p.generation)


def test_restart_preserves_backoff_and_success_progress(tmp_path):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    p.failure()
    succeed(p, 4)
    restored = policy(p.path, clock)
    assert restored.interval_range == (4, 8)
    assert restored.summary()['recovery_successes_remaining'] == 1
    restored.success(restored.generation)
    assert restored.interval_range == (1.5, 2.5)
    succeed(restored, 9)
    assert restored.interval_range == (1.5, 2.5)
    succeed(restored, 1)
    assert restored.interval_range == (1, 1.5)


def test_legacy_fifteen_successes_need_a_new_valid_response(tmp_path):
    path = tmp_path / 'control.json'
    path.write_text(json.dumps({'version': 1, 'level': 1, 'successes': 15}), encoding='utf-8')
    p = policy(path, Clock())
    assert p.interval_range == (4, 8)
    p.success(p.generation)
    assert p.interval_range == (1.5, 2.5)
    assert p._normal_step == p._normal_successes == 0
    assert policy(path, Clock()).interval_range == (1.5, 2.5)


def test_another_failure_resets_streak_and_late_responses_do_not_recover(tmp_path):
    p = policy(tmp_path / 'control.json', Clock())
    p.failure()
    succeed(p, 4)
    old = p.generation
    p.failure()
    for _ in range(10):
        p.success(old)
    assert p._successes == 0 and p.interval_range == (8, 16)
    succeed(p, 4)
    assert p.interval_range == (8, 16)
    succeed(p, 1)
    assert p.interval_range == (4, 8)
    succeed(p, 5)
    assert p.interval_range == (1.5, 2.5)


@pytest.mark.parametrize('control', ['guard_until', 'cooldown_until'])
def test_protection_keeps_twenty_successes_and_deadlines(tmp_path, control):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    p.failure()
    p.state[control] = clock.now + 1800
    p.save()
    p = policy(p.path, clock)
    succeed(p, 5)
    assert p._level == 1
    succeed(p, 14)
    assert p._level == 1
    succeed(p, 1)
    assert p._level == 0
    assert p.state[control] == 11800
    if control == 'guard_until':
        assert p.interval_range == (8, 12)
        succeed(p, 40)
        assert p._normal_step == p._normal_successes == 0


def test_restart_and_idle_time_alone_cannot_clear_backoff(tmp_path):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    p.failure()
    clock.now += 86400
    assert policy(p.path, clock).interval_range == (4, 8)


def test_pause_and_stale_success_cannot_recover(tmp_path):
    p = policy(tmp_path / 'control.json', Clock())
    p.failure()
    succeed(p, 4)
    old = p.generation
    p.pause()
    succeed(p, 100)
    assert p._successes == 0 and p._level == 1
    p.resume()
    p.success(old)
    assert p._successes == 0 and p._level == 1
    assert p.state['guard_until'] == 11800
    assert p.state['cooldown_until'] == 10060


class PreviousRecovery(StableRequestPolicy):
    RECOVERY_SUCCESSES = 20


class RecoveryTransport(SimulatedTransport):
    def __init__(self, *args, fixture, previous):
        super().__init__(*args, fixture=fixture)
        cls = PreviousRecovery if previous else StableRequestPolicy
        self.policy = cls(self.profile.parent / 'control.json', clock=self.clock,
                          wall_clock=self.clock, sleep=self.clock.sleep,
                          uniform=lambda low, high: (low + high) / 2,
                          check_stop=self.check_stop)

    async def http_request(self, *args, **kwargs):
        try:
            return await super().http_request(*args, **kwargs)
        finally:
            await self.clock.sleep(.3)


class RecoveryFixture(Fixture):
    def __init__(self, kind, fault):
        super().__init__(kind)
        self.fault, self.failed = fault, False
        self.main = {i * 20: page([comment(str(i))], (i + 1) * 20, int(i < 39)) for i in range(40)}
        self.pages = {i * 20: likes([video(str(i + 1))], (i + 1) * 20, int(i < 39)) for i in range(40)}

    def __call__(self, request):
        if not self.failed and request.url.path.endswith(('/comment/list/', '/favorite/', '/recommend/feed/')):
            self.failed = True
            self.requests.append(request)
            return httpx.Response(503 if self.fault == 'temporary' else 200, json={})
        return super().__call__(request)


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('fault', ['temporary', 'malformed'])
async def test_recovery_keeps_pages_and_txt_identical_with_less_waiting(tmp_path, kind, fault):
    samples = []
    for previous in (True, False):
        directory = tmp_path / ('before' if previous else 'after')
        db = Database(directory / 'test.db')
        task = create(db, kind, verify_authors=False, list_order='publication')
        fixture = RecoveryFixture(kind, fault)
        runtime = WorkbenchRuntime(db, args_for(directory, task), lambda: False, lambda *args: None,
            transport_factory=lambda *args: RecoveryTransport(*args, fixture=fixture, previous=previous))
        await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
        result = db.get_task(task['id'])
        assert result['status'] == 'completed' and result['collection_complete']
        assert result['main_count'] + result['video_count'] == 40
        assert runtime.transport.metrics['http_peak_active'] == 1
        assert runtime.transport.metrics['request_outcomes'][fault] == 1
        assert not runtime.transport.policy.state.get('events')
        samples.append((exported(db, task['id'], kind), {
            'seconds': round(runtime.transport.clock.now - 10000, 3),
            'requests': runtime.transport.metrics['http_requests'],
        }))
    assert samples[0][0] == samples[1][0]
    before, after = (item[1] for item in samples)
    assert before['requests'] == after['requests']
    assert after['seconds'] < before['seconds'] * .75
    report = {'kind': kind, 'fault': fault, 'before': before, 'after': after, 'txt_identical': True,
              'time_basis': 'virtual network and scheduler clock'}
    (tmp_path / 'recovery-benchmark.json').write_text(json.dumps(report), encoding='utf-8')
    print('RECOVERY_BENCHMARK ' + json.dumps(report))
