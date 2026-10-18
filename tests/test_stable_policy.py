import asyncio
import json
from email.utils import formatdate

import pytest

from backend.crawler import PageFailure, StopRequested
from backend.stable_policy import StableRequestPolicy, ProtectionPause


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now

    async def sleep(self, value):
        self.now += value
        await asyncio.sleep(0)


def policy(path, clock, **kwargs):
    return StableRequestPolicy(path, wall_clock=clock, clock=clock, sleep=clock.sleep,
                               uniform=lambda low, high: low, **kwargs)


async def test_base_pacing_and_cross_task_last_start(tmp_path):
    clock = Clock()
    first = policy(tmp_path / 'control.json', clock)
    await first.wait_turn()
    assert clock.now == 10000
    await first.wait_turn()
    assert clock.now == pytest.approx(10001.5)
    next_task = policy(first.path, clock)
    await next_task.wait_turn()
    assert clock.now == pytest.approx(10003)
    assert first.interval_range == (1.5, 2.5)


def test_failures_and_five_successes_restore_one_step(tmp_path):
    p = policy(tmp_path / 'control.json', Clock())
    for expected in [(4, 8), (8, 16), (16, 32), (16, 32)]:
        p.failure()
        assert p.interval_range == expected
    old = p.generation
    p.failure()
    p.success(old)
    assert p._successes == 0
    for _ in range(4):
        p.success(p.generation)
    assert p.interval_range == (16, 32)
    p.success(p.generation)
    assert p.interval_range == (8, 16)
    assert policy(p.path, Clock()).interval_range == (8, 16)


@pytest.mark.parametrize('header,delay', [(None, 60), ('3', 60), ('120', 120), ('bad', 60), ('inf', 60)])
async def test_429_minimum_cooldown_and_guard_survive_restart(tmp_path, header, delay):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    await p.wait_turn()
    p.failure(rate_limited=True, retry_after=header)
    p = policy(p.path, clock)
    await p.wait_turn()
    assert clock.now == pytest.approx(10000 + delay)
    assert p.interval_range == (8, 12)
    clock.now += 1801
    assert p.interval_range == (4, 8)


def test_http_date_retry_after(tmp_path):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    p.failure(rate_limited=True, retry_after=formatdate(clock.now + 300, usegmt=True))
    assert p.state['cooldown_until'] == clock.now + 300


async def test_repeated_risk_pauses_until_explicit_new_worker(tmp_path):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    p.failure(rate_limited=True)
    clock.now += 100
    with pytest.raises(ProtectionPause):
        p.pause()
    assert p.state['cooldown_until'] == clock.now + 900
    with pytest.raises(ProtectionPause):
        await policy(p.path, clock).wait_turn()
    clock.now += 901
    with pytest.raises(ProtectionPause):
        p.check_control()  # no automatic resume of the old task
    resumed = policy(p.path, clock)
    assert resumed.paused  # pending challenge still needs human action
    resumed.resume()
    await resumed.wait_turn()
    assert clock.now >= 10000 + 100 + 901 + 60


def test_first_login_does_not_record_risk(tmp_path):
    p = policy(tmp_path / 'control.json', Clock())
    p.pause(challenge=False)
    p.resume()
    assert not p.state.get('events') and not p.state.get('guard_until')


async def test_challenge_pauses_globally_and_refresh_precedes_resume(tmp_path):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    old = p.generation
    p.pause()
    sent = []
    pending = asyncio.create_task(p.prepare_start(lambda: mark(sent)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not sent
    p.success(old)
    assert p._successes == 0
    p.resume()
    resumed_at = clock.now
    await pending
    assert len(sent) == 1 and clock.now >= resumed_at + 60


async def mark(sent):
    sent.append(True)


async def test_single_inflight_request_and_cancel_cooldown(tmp_path):
    clock = Clock()
    stopped = False
    def stop():
        if stopped:
            raise StopRequested()
    p = policy(tmp_path / 'control.json', clock, check_stop=stop)
    active = peak = 0
    async def operation():
        nonlocal active, peak
        async with p.slot():
            await p.wait_turn()
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.001)
            active -= 1
    await asyncio.gather(*(operation() for _ in range(8)))
    assert peak == 1
    p.failure(rate_limited=True)
    pending = asyncio.create_task(p.wait_turn())
    await asyncio.sleep(0)
    stopped = True
    with pytest.raises(StopRequested):
        await pending
    assert p._starts.locked() is False


def test_corrupt_control_fails_closed(tmp_path):
    path = tmp_path / 'control.json'
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(PageFailure):
        policy(path, Clock())
    assert StableRequestPolicy.public_state(path)['control_status'] == 'error'
