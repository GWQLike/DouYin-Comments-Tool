"""Process ownership and shared stop polling for a single data directory."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import threading
import time
import uuid


POLL_SECONDS = 0.2


class OwnershipError(RuntimeError):
    pass


class ProcessLease:
    """The OS releases the lock on exit. Never unlink a lock file in use."""
    def __init__(self, directory, name):
        self.path = Path(directory).resolve() / f'.{name}.lock'
        self.file = None
        self.token = None

    def acquire(self, timeout=0):
        if self.file is not None:
            raise RuntimeError('Process lease already acquired')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b', buffering=0)
        # Lock a reserved byte; ownership metadata starts after it. Windows
        # byte locks are mandatory, so readers must not read the reserved byte.
        if self.path.stat().st_size == 0:
            handle.write(b'\0')
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise OwnershipError('该数据目录已有服务或采集进程在使用，请等待原进程退出后重试。') from None
                    time.sleep(min(.05, max(0, deadline - time.monotonic())))
            self.file = handle
            return self
        except BaseException:
            handle.close()
            raise

    def identify(self):
        if self.file is None:
            raise RuntimeError('Process lease is not held')
        self.token = uuid.uuid4().hex
        self.file.seek(1)
        self.file.truncate()
        self.file.write(self.token.encode('ascii'))
        return self.token

    def release(self):
        if self.file is not None:
            handle, self.file = self.file, None
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def owner_alive(directory, token):
    if not token:
        return False
    lease = ProcessLease(directory, 'service')
    try:
        with open(lease.path, 'rb') as stream:
            stream.seek(1)
            if stream.read(64).decode('ascii') != token:
                return False
        try:
            lease.acquire()
        except OwnershipError:
            return True
        else:
            lease.release()
            return False
    except (OSError, UnicodeError):
        return False


class StopSignal:
    """All layers share one cached probe; local signals remain immediate."""
    def __init__(self, probe, *, owner=None, clock=time.monotonic):
        self.probe, self.owner, self.clock = probe, owner, clock
        self.event = threading.Event()
        self.next_probe = 0
        self.stopped = self.parent_lost = False

    def request(self, *_):
        self.event.set()

    def __call__(self):
        if self.event.is_set() or self.stopped:
            return True
        now = self.clock()
        if now >= self.next_probe:
            self.next_probe = now + POLL_SECONDS
            self.parent_lost = self.owner is not None and not self.owner()
            self.stopped = self.parent_lost or bool(self.probe())
        return self.stopped
