"""Bounded, service-local keyword matches; never retain comment bodies."""
from collections import OrderedDict
from dataclasses import dataclass, field
import sys
import threading


def match_key(task_id, version, filters):
    return (task_id, version, filters.account, filters.keyword, filters.mode,
            filters.threshold, filters.start_ts, filters.end_ts)


@dataclass
class MatchBuild:
    key: tuple
    size: int
    matches: list = field(default_factory=list)


class KeywordMatchCache:
    def __init__(self, *, max_entries=64, max_bytes=32 * 1024 * 1024,
                 max_entry_bytes=16 * 1024 * 1024):
        if min(max_entries, max_bytes, max_entry_bytes) < 0:
            raise ValueError('Cache limits must be nonnegative')
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = min(max_entry_bytes, max_bytes)
        self._entries = OrderedDict()
        self._bytes = 0
        self._building = None
        self._lock = threading.RLock()

    def _remove(self, key):
        _, size = self._entries.pop(key)
        self._bytes -= size

    def _observe(self, key):
        # An older read snapshot must neither evict nor replace newer matches.
        for saved in list(self._entries):
            if saved[0] == key[0] and saved[1] < key[1]:
                self._remove(saved)
        build = self._building
        if build is not None and build.key[0] == key[0] and build.key[1] < key[1]:
            self.abort(build)
        return not any(saved[0] == key[0] and saved[1] > key[1] for saved in self._entries)

    def get(self, key):
        with self._lock:
            self._observe(key)
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def begin(self, key):
        with self._lock:
            if not self._observe(key) or self._building is not None or not self.max_entries:
                return None
            # Include key objects and container/bookkeeping overhead. Per-row
            # accounting below also reserves both list and final tuple pointers.
            size = 512 + sys.getsizeof(key) + sum(sys.getsizeof(value) for value in key)
            if size > self.max_entry_bytes:
                return None
            while self._entries and self._bytes + size > self.max_bytes:
                self._remove(next(iter(self._entries)))
            self._building = MatchBuild(key, size)
            return self._building

    def append(self, build, comment_id, kind, score):
        if build is None:
            return
        match = (comment_id, kind, score)
        size = sys.getsizeof(match) + sum(sys.getsizeof(value) for value in match) + 32
        with self._lock:
            if self._building is not build:
                return
            if build.size + size > self.max_entry_bytes:
                self.abort(build)
                return
            while self._entries and self._bytes + build.size + size > self.max_bytes:
                self._remove(next(iter(self._entries)))
            build.matches.append(match)
            build.size += size

    def finish(self, build, current_version):
        if build is None:
            return
        with self._lock:
            if self._building is not build:
                return
            try:
                # Check the latest committed state, not the reader's old SQLite
                # snapshot. The same lock serializes publication with deletion.
                if current_version() != build.key[1]:
                    return
                if build.key in self._entries:
                    self._remove(build.key)
                while len(self._entries) >= self.max_entries:
                    self._remove(next(iter(self._entries)))
                self._entries[build.key] = (tuple(build.matches), build.size)
                self._bytes += build.size
            finally:
                self.abort(build)

    def abort(self, build):
        with self._lock:
            if build is not None and self._building is build:
                build.matches.clear()
                self._building = None

    def discard(self, task_id):
        with self._lock:
            for key in list(self._entries):
                if key[0] == task_id:
                    self._remove(key)
            if self._building is not None and self._building.key[0] == task_id:
                self.abort(self._building)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            self.abort(self._building)

    def info(self):
        with self._lock:
            building_bytes = self._building.size if self._building is not None else 0
            return {'entries': len(self._entries), 'bytes': self._bytes,
                    'building_bytes': building_bytes, 'total_bytes': self._bytes + building_bytes}
