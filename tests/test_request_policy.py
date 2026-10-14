import asyncio
from email.utils import formatdate

import pytest

from backend.crawler import StopRequested
from backend.request_policy import RequestPolicy


class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_first_immediate_spacing_backoff_and_success_recovery():
    clock = Clock()
    # Explicit intervals retain the previous policy for comparable benchmarks.
    policy = RequestPolicy(base_interval=1, clock=clock, sleep=clock.sleep)
    await policy.wait_turn()
    assert clock() == 0
    await policy.wait_turn()
    assert clock() == pytest.approx(1)
    for expected in (2, 4, 8, 8):
        previous = clock()
        policy.failure()
        await policy.wait_turn()
        assert clock() - previous == pytest.approx(expected)
    for expected in (4, 2, 1, 1):
        for _ in range(10):
            policy.success()
        assert policy.interval == expected


@pytest.mark.asyncio
async def test_progressive_first_immediate_and_each_twenty_successes_accelerate():
    clock = Clock()
    policy = RequestPolicy(clock=clock, sleep=clock.sleep)
    await policy.wait_turn()
    assert clock() == 0
    await policy.wait_turn()
    assert clock() == pytest.approx(.5)
    for before, after in ((.5, .4), (.4, .3), (.3, .25), (.25, .25)):
        for _ in range(19):
            policy.success(generation=policy.generation)
        assert policy.interval == before
        policy.success(generation=policy.generation)
        assert policy.interval == after
        previous = clock()
        await policy.wait_turn()
        assert clock() - previous == pytest.approx(after)
    report = policy.summary()
    assert report['speed_mode'] == 'progressive'
    assert report['speed_change_count'] == 3
    assert [event['to_interval_seconds'] for event in report['speed_changes']] == [.4, .3, .25]


def test_progressive_backoff_recovery_and_fresh_acceleration_streak():
    policy = RequestPolicy()
    for _ in range(79):
        policy.success()
    assert policy.interval == .25
    for expected in (2, 4, 8, 8):
        policy.failure()
        assert policy.interval == expected
        # Nine intervening successes must be discarded on the next failure.
        for _ in range(9):
            policy.success()
        assert policy.interval == expected
    policy.success()
    assert policy.interval == 4
    for before, after in ((4, 2), (2, .5)):
        for _ in range(9):
            policy.success()
        assert policy.interval == before
        policy.success()
        assert policy.interval == after
    for _ in range(19):
        policy.success()
    assert policy.interval == .5
    policy.success()
    assert policy.interval == .4


def test_late_successes_do_not_recover_after_any_failure():
    policy = RequestPolicy()
    old_generation = policy.generation
    policy.failure()
    failed_generation = policy.generation
    for _ in range(50):
        policy.success(generation=old_generation)
    assert policy.interval == 2
    for _ in range(9):
        policy.success(generation=failed_generation)
    assert policy.interval == 2
    policy.failure()
    for _ in range(50):
        policy.success(generation=failed_generation)
    assert policy.interval == 4
    assert policy.summary()['stale_successes'] == 100
    for _ in range(10):
        policy.success(generation=policy.generation)
    assert policy.interval == 2


def test_permanent_failure_resets_streak_and_generation_without_slowing_down():
    policy = RequestPolicy()
    for _ in range(39):
        policy.success()
    assert policy.interval == .4
    before_failure = policy.generation
    policy.failure(temporary=False)
    assert policy.generation == before_failure + 1
    assert policy.interval == .4
    for _ in range(20):
        policy.success(generation=before_failure)
    for _ in range(19):
        policy.success(generation=policy.generation)
    assert policy.interval == .4
    policy.success(generation=policy.generation)
    assert policy.interval == .3
    assert policy.summary()['permanent_failures'] == 1
    assert policy.summary()['temporary_failures'] == 0


def test_rate_limit_always_triggers_temporary_backoff():
    policy = RequestPolicy()
    policy.failure(temporary=False, rate_limited=True, retry_after='0')
    assert policy.interval == 2
    assert policy.summary()['temporary_failures'] == 1
    assert policy.summary()['permanent_failures'] == 0


def test_rate_limit_locks_minimum_interval_for_rest_of_task():
    policy = RequestPolicy()
    for _ in range(60):
        policy.success()
    assert policy.interval == .25
    policy.failure(rate_limited=True, retry_after='0')
    assert policy.interval == 2
    for _ in range(200):
        policy.success(generation=policy.generation)
    assert policy.interval == .5
    assert not policy.summary()['acceleration_allowed']
    policy.failure()
    for _ in range(200):
        policy.success(generation=policy.generation)
    assert policy.interval == .5


def test_manual_pause_is_idempotent_and_resume_resets_to_two_seconds():
    policy = RequestPolicy()
    for _ in range(59):
        policy.success()
    old_generation = policy.generation
    policy.pause()
    paused_generation = policy.generation
    policy.pause()
    assert policy.generation == paused_generation == old_generation + 1
    for _ in range(20):
        policy.success(generation=paused_generation)
    policy.resume()
    assert not policy.paused
    assert policy.interval == 2
    resumed_generation = policy.generation
    policy.resume()
    assert policy.generation == resumed_generation
    for generation in (old_generation, paused_generation):
        for _ in range(20):
            policy.success(generation=generation)
    assert policy.interval == 2
    for _ in range(9):
        policy.success(generation=resumed_generation)
    assert policy.interval == 2
    policy.success(generation=resumed_generation)
    assert policy.interval == .5
    for _ in range(110):
        policy.success(generation=resumed_generation)
    assert policy.interval == .5
    assert not policy.summary()['acceleration_allowed']
    assert policy.summary()['speed_changes'][-1]['reason'] == 'success_recovery'


@pytest.mark.asyncio
async def test_time_scale_scales_tiers_but_never_retry_after():
    clock = Clock()
    policy = RequestPolicy(time_scale=.05, clock=clock, sleep=clock.sleep)
    assert policy.interval == pytest.approx(.025)
    for _ in range(20):
        policy.success()
    assert policy.interval == pytest.approx(.02)
    policy.failure(rate_limited=True, retry_after='12')
    assert policy.interval == pytest.approx(.1)
    await policy.wait_turn()
    assert clock() == pytest.approx(12)


@pytest.mark.parametrize('scale', [0, -1, float('nan'), float('inf')])
def test_time_scale_must_be_positive_and_finite(scale):
    with pytest.raises(ValueError, match='time_scale'):
        RequestPolicy(time_scale=scale)


@pytest.mark.asyncio
@pytest.mark.parametrize('header,expected', [('12', 12), (None, 30), ('invalid', 30),
    (formatdate(1020, usegmt=True), 20)])
async def test_rate_limit_cooldown_covers_all_requests(header, expected):
    clock = Clock()
    policy = RequestPolicy(clock=clock, sleep=clock.sleep, wall_clock=lambda: 1000)
    policy.failure(rate_limited=True, retry_after=header)
    await policy.wait_turn()
    assert clock() == pytest.approx(expected)


@pytest.mark.asyncio
async def test_bounded_slots_cancel_waiter_without_leaking_permits():
    policy = RequestPolicy(base_interval=0)
    running = peak = 0
    release = asyncio.Event()
    entered = asyncio.Event()

    async def work():
        nonlocal running, peak
        async with policy.slot():
            running += 1
            peak = max(peak, running)
            if running == 3:
                entered.set()
            try:
                await release.wait()
            finally:
                running -= 1

    jobs = [asyncio.create_task(work()) for _ in range(5)]
    await entered.wait()
    jobs[-1].cancel()
    await asyncio.gather(jobs[-1], return_exceptions=True)
    release.set()
    await asyncio.gather(*jobs[:-1])
    assert peak == 3
    assert policy._slots._value == 3


@pytest.mark.asyncio
async def test_stop_interrupts_manual_pause_and_spacing():
    stopped = False
    def check():
        if stopped:
            raise StopRequested()
    policy = RequestPolicy(check_stop=check)
    policy.pause()
    waiter = asyncio.create_task(policy.wait_turn())
    await asyncio.sleep(0)
    stopped = True
    with pytest.raises(StopRequested):
        await asyncio.wait_for(waiter, .5)
    assert not policy._starts.locked()


@pytest.mark.asyncio
async def test_stop_during_spacing_releases_start_lock_without_another_request():
    clock = Clock()
    def check():
        if clock() > 0:
            raise StopRequested()
    policy = RequestPolicy(clock=clock, sleep=clock.sleep, check_stop=check)
    await policy.wait_turn()
    with pytest.raises(StopRequested):
        await policy.wait_turn()
    assert not policy._starts.locked()
    assert policy.summary()['request_starts'] == 1


@pytest.mark.asyncio
async def test_prepare_happens_after_cooldown_and_actual_sends_stay_spaced():
    clock = Clock()
    policy = RequestPolicy(clock=clock, sleep=clock.sleep)
    policy.failure(rate_limited=True, retry_after='30')
    preparing, sends = [], []
    async def slow_sign():
        preparing.append(clock())
        await clock.sleep(3)
        return 'slow'
    async def fast_sign():
        preparing.append(clock())
        await clock.sleep(.1)
        return 'fast'
    assert await policy.prepare_start(slow_sign) == 'slow'
    sends.append(clock())
    assert await policy.prepare_start(fast_sign) == 'fast'
    sends.append(clock())
    assert preparing[0] == pytest.approx(30)
    assert sends[1] - sends[0] >= policy.interval


@pytest.mark.asyncio
async def test_cooldown_during_preparation_discards_old_signature():
    clock = Clock()
    policy = RequestPolicy(clock=clock, sleep=clock.sleep)
    stamps = []
    async def prepare():
        stamps.append(clock())
        if len(stamps) == 1:
            policy.failure(rate_limited=True, retry_after='30')
        return len(stamps)
    assert await policy.prepare_start(prepare) == 2
    assert stamps == pytest.approx([0, 30])
    assert policy.summary()['request_starts'] == 1


@pytest.mark.asyncio
async def test_manual_verification_during_preparation_discards_old_snapshot():
    clock = Clock()
    policy = RequestPolicy(clock=clock, sleep=clock.sleep)
    await policy.wait_turn()
    generations = []
    async def prepare():
        generations.append(policy.generation)
        if len(generations) == 1:
            policy.pause()
            policy.resume()
        return len(generations)
    assert await policy.prepare_start(prepare) == 2
    assert len(set(generations)) == 2
    assert clock() == pytest.approx(2)
    assert policy.summary()['request_starts'] == 2


def test_speed_change_history_is_bounded_and_snapshot_is_detached():
    policy = RequestPolicy()
    for _ in range(40):
        policy.failure()
        for _ in range(20):
            policy.success()
    report = policy.summary()
    assert report['speed_change_count'] == 80
    assert len(report['speed_changes']) == 64
    report['speed_changes'][-1]['reason'] = 'changed by caller'
    assert policy.summary()['speed_changes'][-1]['reason'] == 'success_recovery'
