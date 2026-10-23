"""Real OS locks and subprocess failures, isolated from platform requests."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import WorkerManager, create_app
from backend.browser_session import DedicatedBrowser
from backend.crawler import PageFailure, StopRequested
from backend.db import Database
from backend.douyin_adapter import DouyinAdapter
from backend.engine_tasks import CommentTask
from backend.runtime_control import ProcessLease, OwnershipError, StopSignal, owner_alive
from test_search_api import FakeManager


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def child(code, *args):
    process = subprocess.Popen([sys.executable, '-c', code, *map(str, args)], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
        env={**os.environ, 'PYTHONUTF8': '1'})
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()  # Only the exact child created by this fixture.
        process.communicate(timeout=5)


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('Timed out waiting for local test process')
        time.sleep(.01)


SERVICE = '''
import sys, time
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import create_app
directory = Path(sys.argv[1])
with TestClient(create_app(directory)):
    (directory / 'service-ready').touch()
    time.sleep(60)
'''

WORKER = '''
import asyncio, runpy, sys, types
from pathlib import Path
directory, task_id, token = sys.argv[1:]
directory = Path(directory)
async def run_engine(db, args, stop, waiting):
    db.transition_task(task_id, 'start', stop_requested=False)
    db.save_page(task_id, '', [], 42, False)
    (directory / 'worker-ready').touch()
    try:
        await asyncio.sleep(60)
    finally:
        (directory / 'request-cancelled').touch()
engine = types.ModuleType('backend.engine')
engine.run_engine = run_engine
sys.modules[engine.__name__] = engine
sys.argv = ['backend.crawler', '--db', str(directory / 'comments.sqlite3'),
    '--profile', str(directory / 'profile'), '--task', task_id, '--service-token', token]
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
'''


def test_importing_app_does_not_initialize_database(tmp_path):
    code = 'import os,sys; os.environ["DOUYIN_DATA_DIR"]=sys.argv[1]; import backend.app'
    with child(code, tmp_path) as process:
        assert process.wait(timeout=10) == 0
    assert not list(tmp_path.iterdir())


def test_os_lock_survives_metadata_and_releases_on_crash(tmp_path):
    code = '''
import sys, time
from pathlib import Path
from backend.runtime_control import ProcessLease
with ProcessLease(sys.argv[1], 'service') as lease:
    lease.identify()
    Path(sys.argv[1], 'ready').write_text(lease.token)
    time.sleep(60)
'''
    with child(code, tmp_path) as process:
        until(lambda: (tmp_path / 'ready').exists())
        token = (tmp_path / 'ready').read_text()
        assert owner_alive(tmp_path, token)
        assert not owner_alive(tmp_path, 'stale-instance')
        with pytest.raises(OwnershipError):
            ProcessLease(tmp_path, 'service').acquire()
        process.kill()
        process.wait(timeout=5)
    assert not owner_alive(tmp_path, token)
    with ProcessLease(tmp_path, 'service') as replacement:
        replacement.identify()
        assert replacement.token != token
        assert not owner_alive(tmp_path, token)
        assert owner_alive(tmp_path, replacement.token)


def test_duplicate_service_does_not_recover_live_task(tmp_path):
    with child(SERVICE, tmp_path) as process:
        until(lambda: (tmp_path / 'service-ready').exists())
        db = Database(tmp_path / 'comments.sqlite3')
        tid = db.create_task('')['id']
        db.update_task(tid, status='running')
        db.save_page(tid, '', [], 29, False)
        before = db.get_task(tid)
        with pytest.raises(OwnershipError):
            with TestClient(create_app(tmp_path)):
                pytest.fail('Duplicate service started')
        assert db.get_task(tid) == before
        assert db.get_checkpoint(tid)['cursor'] == 29
        process.kill()
        process.wait(timeout=5)
    with TestClient(create_app(tmp_path)) as client:
        assert client.app.state.db.get_task(tid)['status'] == 'paused'
        assert client.app.state.db.get_checkpoint(tid)['cursor'] == 29


def test_live_worker_blocks_recovery_even_if_status_is_completed(tmp_path, monkeypatch):
    db = Database(tmp_path / 'comments.sqlite3')
    tid = db.create_task('')['id']
    db.update_task(tid, status='completed')
    db.session_set('ready')
    acquire = ProcessLease.acquire
    monkeypatch.setattr(ProcessLease, 'acquire', lambda self, timeout=0: acquire(self, min(timeout, .05)))
    with ProcessLease(tmp_path, 'worker'):
        with pytest.raises(OwnershipError):
            with TestClient(create_app(tmp_path)):
                pytest.fail('Live worker must block recovery')
        assert db.get_task(tid)['status'] == 'completed'
        assert db.session_get()['status'] == 'ready'
    with TestClient(create_app(tmp_path)) as client:
        assert client.app.state.db.session_get()['status'] == 'logged_out'


@pytest.mark.parametrize('stop_kind', ['service_exit', 'task_stop'])
def test_worker_cancels_request_and_keeps_checkpoint(tmp_path, stop_kind):
    with child(SERVICE, tmp_path) as service:
        until(lambda: (tmp_path / 'service-ready').exists())
        db = Database(tmp_path / 'comments.sqlite3')
        tid = db.create_task('')['id']
        with open(tmp_path / '.service.lock', 'rb') as stream:
            stream.seek(1)
            token = stream.read().decode()
        with child(WORKER, tmp_path, tid, token) as process:
            until(lambda: (tmp_path / 'worker-ready').exists())
            with child(WORKER, tmp_path, tid, token) as duplicate:
                assert duplicate.wait(timeout=5) == 1
            assert not (tmp_path / 'request-cancelled').exists()
            began = time.monotonic()
            if stop_kind == 'service_exit':
                service.kill()
                service.wait(timeout=5)
            else:
                db.transition_task(tid, 'request_stop')
            assert process.wait(timeout=3) == 0
            assert time.monotonic() - began < 1.5
            assert (tmp_path / 'request-cancelled').exists()
        assert db.get_task(tid)['status'] == 'paused'
        assert db.get_checkpoint(tid)['cursor'] == 42
        if stop_kind == 'service_exit':
            assert db.session_get()['status'] == 'logged_out'
            with TestClient(create_app(tmp_path)) as client:
                assert client.app.state.db.get_checkpoint(tid)['cursor'] == 42


@pytest.mark.parametrize('token', [None, 'wrong-service'])
def test_cli_cannot_join_another_service(tmp_path, token):
    db = Database(tmp_path / 'comments.sqlite3')
    db.session_set('ready')
    code = '''
import runpy,sys
directory,token=sys.argv[1:]
sys.argv=['backend.crawler','--db',directory+'/comments.sqlite3','--profile',directory+'/profile']
if token: sys.argv += ['--service-token',token]
runpy.run_module('backend.crawler',run_name='__main__',alter_sys=True)
'''
    with ProcessLease(tmp_path, 'service') as owner:
        owner.identify()
        with child(code, tmp_path, token or '') as process:
            assert process.wait(timeout=5) == 1
        assert db.session_get()['status'] == 'ready'


def test_manager_checks_worker_lock_before_browser_or_task_change(tmp_path):
    db = Database(tmp_path / 'comments.sqlite3')
    tid = db.create_task('')['id']
    manager = WorkerManager(db, tmp_path)
    manager.browser.ensure = Mock(side_effect=AssertionError('unexpected browser'))
    with ProcessLease(tmp_path, 'service') as owner, ProcessLease(tmp_path, 'worker'):
        owner.identify()
        manager.owner = owner
        with pytest.raises(HTTPException) as error:
            manager.start(tid)
        assert error.value.status_code == 409
        assert db.get_task(tid)['status'] == 'created'
    manager.browser.ensure.assert_not_called()


def test_stop_probe_shared_and_local_signal_immediate(tmp_path):
    now = [10.0]
    probe = Mock(return_value=False)
    stop = StopSignal(probe, clock=lambda: now[0])
    adapter = DouyinAdapter(tmp_path / 'profile', stop, lambda *args: None)
    task = CommentTask(None, adapter, 'fixture', stop=stop)
    for _ in range(100):
        task.check_stop()
        adapter.check_stop()
    assert probe.call_count == 1
    probe.return_value = True
    now[0] += .19
    task.check_stop()
    now[0] += .01
    with pytest.raises(StopRequested):
        adapter.check_stop()
    assert probe.call_count == 2
    other = StopSignal(Mock(return_value=False), clock=lambda: 0)
    assert not other()
    other.request()
    assert other()
    assert other.probe.call_count == 1


def test_idle_monitor_detects_browser_and_worker_exit(tmp_path):
    db = Database(tmp_path / 'comments.sqlite3')
    tid = db.create_task('')['id']
    db.update_task(tid, status='running')
    db.session_set('ready')
    manager = WorkerManager(db, tmp_path)
    browser_process = Mock()
    browser_process.poll.return_value = None
    manager.browser.launcher = SimpleNamespace(browser_process=browser_process)
    worker = Mock()
    worker.poll.return_value = None
    manager.process, manager.task_id = worker, tid
    manager.start_monitor(None)
    try:
        browser_process.poll.return_value = 0
        worker.poll.return_value = 1
        until(lambda: db.session_get()['status'] == 'logged_out', timeout=1)
        assert db.get_task(tid)['status'] == 'partial'
        assert manager.process is None
    finally:
        manager.stop_monitor()
        manager.close_browser()


def test_browser_close_invalidates_cache_but_worker_detach_does_not(tmp_path):
    adapter = DouyinAdapter(tmp_path / 'profile', lambda: False, lambda *args: None)
    adapter.session_closed = Mock()
    adapter._logged_in = True
    adapter._cookies_checked = adapter._session_checked = 123
    adapter.browser_closed()
    adapter.browser_closed()
    assert not adapter._logged_in and adapter._cookies_checked is None
    adapter.session_closed.assert_called_once()
    with pytest.raises(PageFailure):
        adapter.check_stop()
    detached = DouyinAdapter(tmp_path / 'other-profile', lambda: False, lambda *args: None)
    detached.session_closed = Mock()
    detached._closing = True
    detached.browser_closed()
    detached.session_closed.assert_not_called()


def test_orphan_browser_is_not_adopted_or_killed(tmp_path):
    with socket.socket() as other_browser:
        other_browser.bind(('127.0.0.1', 0))
        other_browser.listen()
        record = tmp_path / 'browser-endpoint.json'
        record.write_text(json.dumps({'port': other_browser.getsockname()[1]}))
        owner = DedicatedBrowser(tmp_path)
        with pytest.raises(PageFailure, match='仍在运行'):
            owner.ensure()
        owner.close()
        assert owner.launcher is None and record.exists()
        assert other_browser.fileno() != -1


def test_lifespan_exception_still_stops_manager_and_releases_lock(tmp_path):
    with pytest.raises(ValueError):
        with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
            manager = client.app.state.manager
            manager.stop = Mock()
            raise ValueError('fixture exit')
    manager.stop.assert_called_once()
    with ProcessLease(tmp_path, 'service'), ProcessLease(tmp_path, 'worker'):
        pass


def test_browser_cleanup_failure_retains_ownership(tmp_path):
    owner = DedicatedBrowser(tmp_path)
    process = Mock()
    process.poll.return_value = None
    owner.launcher = Mock(browser_process=process)
    owner.launcher.cleanup.side_effect = lambda: setattr(owner.launcher, 'browser_process', None)
    owner.endpoint = 'http://127.0.0.1:12345'
    owner.record.write_text('{"port":12345}')
    with pytest.raises(PageFailure, match='尚未退出'):
        owner.close()
    assert owner.launcher.browser_process is process and owner.record.exists()
    process.poll.return_value = 0
    owner.close()
    assert not owner.record.exists()


def test_actual_worker_crash_is_observed_without_api_poll(tmp_path):
    db = Database(tmp_path / 'comments.sqlite3')
    tid = db.create_task('')['id']
    manager = WorkerManager(db, tmp_path)
    with ProcessLease(tmp_path, 'service') as owner:
        owner.identify()
        with child(WORKER, tmp_path, tid, owner.token) as process:
            until(lambda: (tmp_path / 'worker-ready').exists())
            manager.process, manager.task_id = process, tid
            manager.start_monitor(owner)
            try:
                process.kill()
                process.wait(timeout=5)
                until(lambda: manager.process is None, timeout=1)
                assert db.get_task(tid)['status'] == 'partial'
                assert db.get_checkpoint(tid)['cursor'] == 42
                with ProcessLease(tmp_path, 'worker'):
                    pass
            finally:
                manager.stop_monitor()


def test_shutdown_failure_does_not_keep_service_lock(tmp_path):
    class FailedStop(FakeManager):
        def stop(self):
            raise RuntimeError('fixture stop failure')
        def close_browser(self):
            self.db.session_set('logged_out')
    with pytest.raises(RuntimeError, match='fixture stop failure'):
        with TestClient(create_app(tmp_path, manager_factory=FailedStop)):
            pass
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as restarted:
        assert restarted.app.state.db.session_get()['status'] == 'logged_out'


@pytest.mark.parametrize('task_run', [False, True])
def test_stop_pipe_survives_late_login_updates(tmp_path, task_run):
    code = '''
import asyncio, runpy, sys, types
from pathlib import Path
directory, task_id, token = sys.argv[1:]
directory = Path(directory)
async def run_engine(db, args, stop, waiting):
    if task_id:
        db.transition_task(task_id, 'start', stop_requested=False)
        db.save_page(task_id, '', [], 17, False)
    (directory / 'ready').touch()
    try:
        while True:
            waiting(False)
            await asyncio.sleep(.01)
    finally:
        (directory / 'cancelled').touch()
engine = types.ModuleType('backend.engine')
engine.run_engine = run_engine
sys.modules[engine.__name__] = engine
sys.argv = ['backend.crawler', '--db', str(directory / 'comments.sqlite3'),
    '--profile', str(directory / 'profile'), '--service-token', token, '--stop-pipe']
if task_id: sys.argv += ['--task', task_id]
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
'''
    db = Database(tmp_path / 'comments.sqlite3')
    tid = db.create_task('')['id'] if task_run else ''
    with ProcessLease(tmp_path, 'service') as owner:
        owner.identify()
        with child(code, tmp_path, tid, owner.token) as process:
            until(lambda: (tmp_path / 'ready').exists())
            process.stdin.close()
            process.stdin = None
            assert process.wait(timeout=2) == 0
        assert (tmp_path / 'cancelled').exists()
    if task_run:
        assert db.get_task(tid)['status'] == 'paused'
        assert db.get_checkpoint(tid)['cursor'] == 17
    else:
        assert db.session_get()['status'] == 'logged_out'
