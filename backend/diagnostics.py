"""Bounded, allowlisted collection diagnostics. Never accept upstream text."""
import json
import logging
import math
import time
import threading
from functools import wraps
import uuid
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from backend.upstream import ENGINE_VERSION

PHASES = {'startup', 'engine_import', 'transport', 'browser_driver', 'browser_connect',
          'navigation', 'session', 'main_comments', 'replies', 'list', 'authors', 'shutdown'}
COUNTERS = {'requests', 'network_seconds', 'signature_seconds', 'local_seconds',
            'normal_wait_seconds', 'backoff_wait_seconds', 'protection_wait_seconds',
            'manual_wait_seconds', 'new_records', 'duplicate_records', 'authors_resolved'}
OUTCOMES = {'success', 'private', 'login', 'verification', 'rate_limit', 'temporary',
            'permanent', 'malformed', 'cancelled', 'error'}


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


class Diagnostics:
    def __init__(self, directory, *, clock=time.monotonic, max_bytes=1024*1024, backups=4):
        self.lock = threading.RLock()
        self.termination = "running"
        self.run_id = uuid.uuid4().hex
        self.clock, self.started = clock, clock()
        self.phase = 'startup'
        self.phase_started = self.started
        self.stages = {}
        self.dropped = 0
        self.handler = RotatingFileHandler(directory / 'collection.jsonl', maxBytes=max_bytes,
                                           backupCount=backups, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        # Handler.handleError normally prints exception details. Keep disk errors local.
        self.handler.handleError = lambda record: self._drop()

    def _drop(self):
        self.dropped += 1

    @synchronized
    def add(self, key, value=1):
        if key not in COUNTERS or not isinstance(value, (int, float)) or not math.isfinite(value):
            return
        stage = self.stages.setdefault(self.phase, {})
        stage[key] = stage.get(key, 0) + max(0, value)

    @synchronized
    def outcome(self, kind):
        kind = kind if kind in OUTCOMES else 'error'
        stage = self.stages.setdefault(self.phase, {})
        failures = stage.setdefault('outcomes', {})
        failures[kind] = failures.get(kind, 0) + 1

    @synchronized
    def set_phase(self, phase):
        if phase not in PHASES:
            phase = 'startup'
        self._elapsed()
        self.phase = phase
        self.emit('phase')

    @contextmanager
    def span(self, phase):
        previous = self.phase
        self.set_phase(phase)
        try:
            yield
        finally:
            self._elapsed()
            self.emit('phase_end')
            self.set_phase(previous)

    @synchronized
    def _elapsed(self):
        now = self.clock()
        stage = self.stages.setdefault(self.phase, {})
        stage['elapsed_seconds'] = stage.get('elapsed_seconds', 0) + max(0, now-self.phase_started)
        self.phase_started = now

    @synchronized
    def emit(self, event):
        if event not in {'start', 'phase', 'phase_end', 'periodic', 'finish'}:
            return
        self._elapsed()
        record = {'run_id': self.run_id, 'engine_version': ENGINE_VERSION, 'event': event, 'phase': self.phase,
                  'elapsed_seconds': round(self.clock()-self.started, 3),
                  'termination': self.termination, 'at': round(time.time(), 3), 'dropped_records': self.dropped, 'stages': self.stages}
        self.handler.handle(logging.LogRecord('collection', logging.INFO, '', 0,
                            json.dumps(record, ensure_ascii=True, allow_nan=False), (), None))

    @synchronized
    def close(self):
        self.handler.close()


def local_work(method):
    """Time synchronous page processing independently of network waits."""
    from functools import wraps
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        diagnostic = getattr(getattr(self.task, 'client', None), 'diagnostics', None)
        began = time.monotonic()
        try:
            return method(self, *args, **kwargs)
        finally:
            if diagnostic:
                diagnostic.add('local_seconds', time.monotonic() - began)
    return wrapped
