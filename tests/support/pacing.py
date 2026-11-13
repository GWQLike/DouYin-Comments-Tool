"""Virtual time for request pacing tests."""
import asyncio

from backend.stable_policy import StableRequestPolicy


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
