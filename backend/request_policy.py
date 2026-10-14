"""One cooperative request budget per worker; no retry loop lives here."""
from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable


class RequestPolicy:
    _FAST_INTERVALS = (0.5, 0.4, 0.3, 0.25)
    _RECOVERY_INTERVALS = (0.5, 2.0, 4.0, 8.0)

    def __init__(self, *, base_interval: float | None = None, max_concurrency: int = 3,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 check_stop: Callable[[], None] = lambda: None,
                 wall_clock: Callable[[], float] = time.time,
                 rate_limit_cooldown: float = 30.0, time_scale: float = 1.0):
        if not math.isfinite(time_scale) or time_scale <= 0:
            raise ValueError("time_scale must be finite and greater than zero")
        self._progressive = base_interval is None
        self.base_interval = 0.5 if self._progressive else max(0.0, base_interval)
        self.time_scale = time_scale
        self.clock, self.sleep, self.check_stop = clock, sleep, check_stop
        self.wall_clock, self.rate_limit_cooldown = wall_clock, rate_limit_cooldown
        self._slots = asyncio.Semaphore(max(1, min(3, max_concurrency)))
        self._starts = asyncio.Lock()
        self._last_start: float | None = None
        self._cooldown_until = 0.0
        self._level = self._successes = 0
        self._fast_level = 0
        self._acceleration_allowed = True
        self._revision = 0
        self._created_at = self.clock()
        self._speed_changes: list[dict] = []
        self.paused = False
        self.metrics = {"request_starts": 0, "schedule_wait_seconds": 0.0,
                        "temporary_failures": 0, "rate_limits": 0,
                        "permanent_failures": 0,
                        "stale_successes": 0, "speed_change_count": 0}

    @property
    def interval(self) -> float:
        if self._progressive:
            seconds = (self._RECOVERY_INTERVALS[self._level] if self._level
                       else self._FAST_INTERVALS[self._fast_level])
            return seconds * self.time_scale
        return self.base_interval * (2 ** self._level)

    @property
    def generation(self) -> int:
        """Capture at the actual send; old responses cannot recover a new epoch."""
        return self._revision

    def _record_speed_change(self, previous: float, reason: str):
        if previous == self.interval:
            return
        self.metrics["speed_change_count"] += 1
        self._speed_changes.append({
            "elapsed_seconds": max(0.0, self.clock() - self._created_at),
            "from_interval_seconds": previous, "to_interval_seconds": self.interval,
            "reason": reason,
        })
        # Keep long-running worker reports bounded, with a separate total count.
        del self._speed_changes[:-64]

    async def _acquire(self, lock):
        pending = asyncio.create_task(lock.acquire())
        acquired = False
        try:
            while not pending.done():
                self.check_stop()
                await asyncio.wait({pending}, timeout=0.2)
            acquired = pending.result()
            self.check_stop()
        except BaseException:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if acquired or (not pending.cancelled() and pending.exception() is None and pending.result()):
                lock.release()
            raise

    @asynccontextmanager
    async def slot(self):
        began = self.clock()
        await self._acquire(self._slots)
        self.metrics["schedule_wait_seconds"] += max(0, self.clock() - began)
        try:
            yield
        finally:
            self._slots.release()

    async def wait_ready(self):
        while self.paused:
            self.check_stop()
            await self.sleep(0.2)
        self.check_stop()

    async def wait_turn(self):
        await self.prepare_start()

    async def prepare_start(self, prepare=None):
        """Wait before signing, serialize preparation, commit the actual send.

        A failure or challenge reported by an in-flight request invalidates any
        preparation in progress. Its snapshot is discarded rather than sent
        after a long cooldown with an old signature or session.
        """
        began = self.clock()
        prepare_seconds = 0.0
        await self._acquire(self._starts)
        try:
            while True:
                await self.wait_ready()
                now = self.clock()
                due = max(self._cooldown_until,
                          now if self._last_start is None else self._last_start + self.interval)
                if due > now:
                    await self.sleep(min(0.2, due - now))
                    continue
                revision = self._revision
                prepare_began = self.clock()
                try:
                    result = await prepare() if prepare is not None else None
                finally:
                    prepare_seconds += max(0, self.clock() - prepare_began)
                self.check_stop()
                if revision != self._revision or self.paused:
                    continue
                self._last_start = self.clock()
                self.metrics["request_starts"] += 1
                return result
        finally:
            self.metrics["schedule_wait_seconds"] += max(0, self.clock() - began - prepare_seconds)
            self._starts.release()

    def success(self, generation: int | None = None):
        if self.paused or (generation is not None and generation != self.generation):
            self.metrics["stale_successes"] += 1
            return
        previous = self.interval
        self._successes += 1
        if not self._progressive:
            if self._successes >= 10:
                self._level = max(0, self._level - 1)
                self._successes = 0
            self._record_speed_change(previous, "success_recovery")
            return
        if self._level and self._successes >= 10:
            self._level = max(0, self._level - 1)
            self._successes = 0
            self._record_speed_change(previous, "success_recovery")
        elif not self._level and self._successes >= 20:
            if self._acceleration_allowed:
                self._fast_level = min(len(self._FAST_INTERVALS) - 1, self._fast_level + 1)
            self._successes = 0
            self._record_speed_change(previous, "success_acceleration")

    def failure(self, *, retry_after: str | None = None, rate_limited: bool = False,
                temporary: bool = True):
        previous = self.interval
        self._revision += 1
        self._successes = 0
        if not temporary and not rate_limited:
            self.metrics["permanent_failures"] += 1
            return
        self._level = min(3, self._level + 1)
        self._fast_level = 0
        self.metrics["temporary_failures"] += 1
        if rate_limited:
            self._acceleration_allowed = False
            self.metrics["rate_limits"] += 1
            delay = self.rate_limit_cooldown
            if retry_after is not None:
                try:
                    parsed = float(retry_after)
                except (ValueError, TypeError):
                    try:
                        parsed = parsedate_to_datetime(retry_after).timestamp() - self.wall_clock()
                    except (ValueError, TypeError, OverflowError):
                        parsed = float("nan")
                if math.isfinite(parsed):
                    delay = max(0.0, parsed)
            self._cooldown_until = max(self._cooldown_until, self.clock() + delay)
        self._record_speed_change(previous, "rate_limit" if rate_limited else "temporary_failure")

    def pause(self):
        if self.paused:
            return
        self._revision += 1
        self._successes = 0
        self._acceleration_allowed = False
        self.paused = True

    def resume(self):
        if not self.paused:
            return
        previous = self.interval
        self._revision += 1
        self._successes = 0
        if self._progressive:
            self._level = 1
            self._fast_level = 0
        self.paused = False
        self._record_speed_change(previous, "manual_resume")

    def summary(self) -> dict:
        return {**self.metrics, "request_interval_seconds": self.interval,
                "speed_mode": "progressive" if self._progressive else "fixed_baseline",
                "acceleration_allowed": self._progressive and self._acceleration_allowed,
                "speed_changes": [dict(event) for event in self._speed_changes]}
