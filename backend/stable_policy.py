"""Persistent, conservative budget shared by all workbench engine tasks.

Only one worker may own a data directory (enforced by WorkerManager). The state
file is replaced atomically and contains timing/control data, never credentials.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from pathlib import Path
from email.utils import parsedate_to_datetime

from backend.crawler import PageFailure, ControlledStop
from backend.request_policy import RequestPolicy


class ProtectionPause(ControlledStop):
    pass


class StableRequestPolicy(RequestPolicy):
    RANGES = ((2, 4), (4, 8), (8, 16), (16, 32))
    NORMAL_RANGES = ((1.5, 2.5), (1, 1.5), (.8, 1.2))
    ACCELERATION_SUCCESSES = 10
    RECOVERY_SUCCESSES = 5
    PROTECTED_RECOVERY_SUCCESSES = 20

    def __init__(self, state_path, *, wall_clock=time.time, uniform=random.uniform, **kwargs):
        super().__init__(base_interval=self.NORMAL_RANGES[0][0], max_concurrency=1, wall_clock=wall_clock, **kwargs)
        self.path, self.uniform = Path(state_path), uniform
        self.state = self.read_state(self.path)
        self.state.setdefault('level', 0)
        self.state.setdefault('successes', 0)
        self.paused = bool(self.state.get('manual_required'))
        self._level = self.state['level']
        self._successes = self.state['successes']
        # Acceleration belongs to this worker; persisted failure recovery does not.
        self._normal_step = self._normal_successes = 0
        # Starting another worker is an explicit user continue, only after the
        # breaker deadline. It does not remove the guard or pending challenge.
        if self.state.get('halted') and self.wall_clock() >= self.state.get('cooldown_until', 0):
            self.state['halted'] = False
            self.save()

    @staticmethod
    def read_state(path):
        try:
            state = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(state, dict) or state.get('version') != 1:
                raise ValueError()
            for key in ('cooldown_until', 'guard_until', 'last_start', 'level', 'successes'):
                value = state.get(key, 0)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError()
            if type(state.get('level', 0)) is not int or state.get('level', 0) > 3 or not isinstance(state.get('events', []), list):
                raise ValueError()
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in state.get('events', [])):
                raise ValueError()
            return state
        except FileNotFoundError:
            return {'version': 1}
        except (ValueError, OSError, TypeError):
            raise PageFailure('采集控制状态无法读取，已停止请求；请检查本地控制文件后重试。') from None

    def save(self):
        self.state.update(level=self._level, successes=self._successes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        # A simultaneous /session read can briefly hold the destination on Windows.
        for attempt in range(5):
            try:
                temporary.replace(self.path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 4:
                    raise
                time.sleep(.01)

    @property
    def interval_range(self):
        low, high = self.RANGES[self._level] if self._level else self.NORMAL_RANGES[self._normal_step]
        if self.state.get('guard_until', 0) > self.wall_clock():
            low, high = max(low, 8), max(high, 12)
        return low, high

    @property
    def interval(self):
        return self.interval_range[0] if hasattr(self, 'state') else 2

    @property
    def recovery_successes(self):
        # A healthy response streak may recover ordinary backoff sooner. Time
        # passing or restarting alone never resets it, or the separate risk guard.
        now = self.wall_clock()
        protected = (self.paused or self.state.get('halted') or
                     self.state.get('cooldown_until', 0) > now or
                     self.state.get('guard_until', 0) > now)
        return self.PROTECTED_RECOVERY_SUCCESSES if protected else self.RECOVERY_SUCCESSES

    def check_control(self):
        self.check_stop()
        if self.state.get('halted'):
            raise ProtectionPause('短时间内再次触发限流或验证，任务已暂停；冷却 15 分钟后请手动继续。', kind='protection')

    async def wait_ready(self):
        while self.paused:
            self.check_control()
            await self.sleep(.2)
        self.check_control()

    async def prepare_start(self, prepare=None):
        began, prepare_seconds = self.clock(), 0.0
        await self._acquire(self._starts)
        try:
            while True:
                await self.wait_ready()
                revision = self.generation
                delay = self.uniform(*self.interval_range)
                due = max(self.state.get('cooldown_until', 0), self.state.get('last_start', 0) + delay)
                while self.wall_clock() < due:
                    self.check_control()
                    if revision != self.generation or self.paused:
                        break
                    await self.sleep(min(.2, due - self.wall_clock()))
                if revision != self.generation or self.paused:
                    continue
                self.check_control()
                started = self.clock()
                try:
                    result = await prepare() if prepare else None
                finally:
                    prepare_seconds += self.clock() - started
                self.check_control()
                if revision != self.generation or self.paused:
                    continue
                self.state['last_start'] = self.wall_clock()
                self.save()
                self.metrics['request_starts'] += 1
                return result
        finally:
            self.metrics['schedule_wait_seconds'] += max(0, self.clock() - began - prepare_seconds)
            self._starts.release()

    def risk_event(self, reason, delay=60):
        self._normal_step = self._normal_successes = 0
        now = self.wall_clock()
        events = [stamp for stamp in self.state.get('events', []) if now - stamp < 900]
        repeated = bool(events)
        events.append(now)
        self.state.update(events=events[-20:], reason=reason,
                          guard_until=max(self.state.get('guard_until', 0), now + 1800),
                          cooldown_until=max(self.state.get('cooldown_until', 0), now + max(60, delay, 900 if repeated else 0)),
                          halted=repeated or self.state.get('halted', False))
        self.state.setdefault('control_events', []).append({'at': now, 'reason': reason,
            'cooldown_until': self.state['cooldown_until'], 'guard_until': self.state['guard_until'], 'halted': self.state['halted']})
        self.state['control_events'] = self.state['control_events'][-32:]
        self.save()
        self.check_control()

    def failure(self, *, retry_after=None, rate_limited=False, temporary=True):
        previous = self.interval
        self._revision += 1
        self._successes = 0
        self._normal_step = self._normal_successes = 0
        if temporary or rate_limited:
            self._level = min(3, self._level + 1)
            self.metrics['temporary_failures'] += 1
        else:
            self.metrics['permanent_failures'] += 1
        if rate_limited:
            self.metrics['rate_limits'] += 1
            delay = 60
            try:
                parsed = float(retry_after)
            except (ValueError, TypeError):
                try:
                    parsed = parsedate_to_datetime(retry_after).timestamp() - self.wall_clock()
                except (ValueError, TypeError, OverflowError, AttributeError):
                    parsed = 0
            if math.isfinite(parsed):
                delay = max(60, parsed)
            self.risk_event('平台限流', delay)
        self._record_speed_change(previous, 'rate_limit' if rate_limited else 'request_failure')
        self.save()

    def success(self, generation=None):
        if self.paused or (generation is not None and generation != self.generation):
            self.metrics['stale_successes'] += 1
            return
        previous = self.interval
        recovering = bool(self._level)
        if self._level:
            self._successes += 1
            if self._successes >= self.recovery_successes:
                self._successes = 0
                self._level -= 1
        elif self.state.get('guard_until', 0) <= self.wall_clock():
            self._normal_successes += 1
            if self._normal_successes >= self.ACCELERATION_SUCCESSES:
                self._normal_successes = 0
                self._normal_step = min(self._normal_step + 1, len(self.NORMAL_RANGES) - 1)
        self._record_speed_change(previous, 'success_recovery' if previous > 2 else 'success_acceleration')
        if recovering:
            self.save()

    def pause(self, *, challenge=True, reason='人工验证'):
        if self.paused:
            if challenge and not self.state.get('challenge_pending', True):
                self._revision += 1
                self.state['challenge_pending'] = True
                self.risk_event(reason)
            return
        self._revision += 1
        self._successes = 0
        self._normal_step = self._normal_successes = 0
        self.paused = True
        self.state['manual_required'] = True
        self.state['challenge_pending'] = challenge
        if challenge:
            self.risk_event(reason)
        self.save()

    def resume(self):
        if not self.paused:
            return
        self.check_control()
        self._revision += 1
        self._successes = 0
        self._normal_step = self._normal_successes = 0
        self.paused = False
        self.state['manual_required'] = False
        if self.state.pop('challenge_pending', False):
            self.state['cooldown_until'] = max(self.state.get('cooldown_until', 0), self.wall_clock() + 60)
        self.save()

    def summary(self):
        return {**super().summary(), 'speed_mode': 'moderate',
                'backoff_level': self._level,
                'recovery_successes_remaining': max(0, self.recovery_successes - self._successes) if self._level else 0,
                'acceleration_allowed': not self.paused and not self._level and self.state.get('guard_until', 0) <= self.wall_clock(),
                'interval_range_seconds': self.interval_range, 'control_events': self.state.get('control_events', []),
                **self.public_state(self.path, self.wall_clock())}

    @classmethod
    def public_state(cls, path, now=None):
        now = time.time() if now is None else now
        try:
            state = cls.read_state(path)
        except PageFailure as exc:
            return {'control_status': 'error', 'control_reason': str(exc)}
        status = ('paused' if state.get('halted') else 'verification' if state.get('manual_required') else
                  'cooldown' if state.get('cooldown_until', 0) > now else
                  'protected' if state.get('guard_until', 0) > now else 'ready')
        return {'control_status': status, 'cooldown_until': state.get('cooldown_until', 0),
                'guard_until': state.get('guard_until', 0), 'control_reason': state.get('reason', '')}
