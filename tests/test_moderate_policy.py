import pytest
from pathlib import Path

from test_stable_policy import Clock, policy


def successes(p, count):
    for _ in range(count):
        p.success(p.generation)


def test_ten_success_steps_and_restart_warmup(tmp_path):
    p = policy(tmp_path / 'control.json', Clock())
    for expected in [(1, 1.5), (.8, 1.2), (.8, 1.2)]:
        before = p.interval_range
        successes(p, 9)
        assert p.interval_range == before
        successes(p, 1)
        assert p.interval_range == expected
    assert policy(p.path, Clock()).interval_range == (1.5, 2.5)


def test_failure_resets_progress_and_late_success_then_full_recovery(tmp_path):
    p = policy(tmp_path / 'control.json', Clock())
    successes(p, 19)
    generation = p.generation
    p.failure()
    assert p.interval_range == (4, 8)
    successes(p, 4)
    p.success(generation)
    assert p.interval_range == (4, 8)
    successes(p, 1)
    assert p.interval_range == (1.5, 2.5)
    successes(p, 9)
    assert p.interval_range == (1.5, 2.5)
    successes(p, 1)
    assert p.interval_range == (1, 1.5)
    p.failure(temporary=False)
    assert p.interval_range == (1.5, 2.5) and p._normal_successes == 0


@pytest.mark.parametrize('challenge', [False, True])
async def test_risk_cooldown_guard_and_no_acceleration_until_expiry(tmp_path, challenge):
    clock = Clock()
    p = policy(tmp_path / 'control.json', clock)
    successes(p, 40)
    old = p.generation
    if challenge:
        p.pause()
        p.success(old)
        p.resume()
    else:
        p.failure(rate_limited=True, retry_after='120')
    await p.wait_turn()
    assert clock.now >= 10000 + (60 if challenge else 120)
    successes(p, 100)
    assert p.interval_range == (8, 12)
    assert p._normal_successes == 0
    clock.now = 11801
    assert p.interval_range == (1.5, 2.5)
    successes(p, 9)
    assert p.interval_range == (1.5, 2.5)
    successes(p, 1)
    assert p.interval_range == (1, 1.5)


def test_control_write_retries_brief_windows_read_lock_without_losing_cooldown(tmp_path, monkeypatch):
    p = policy(tmp_path / 'control.json', Clock())
    original = Path.replace
    attempts = []
    def replace(source, destination):
        attempts.append(True)
        if len(attempts) == 1:
            error = PermissionError('sharing violation')
            error.winerror = 32
            raise error
        return original(source, destination)
    monkeypatch.setattr(Path, 'replace', replace)
    p.failure(rate_limited=True, retry_after='120')
    restored = policy(p.path, Clock())
    assert len(attempts) >= 2
    assert restored.state['cooldown_until'] == 10120
    assert restored.state['guard_until'] == 11800
