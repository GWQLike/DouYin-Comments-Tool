"""Exercise the worker's -m module identity without opening a browser."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend.db import Database


@pytest.mark.parametrize('status', ['completed', 'private', 'paused'])
@pytest.mark.parametrize('exception', ['StopRequested', 'PageFailure'])
def test_cli_cleanup_does_not_overwrite_final_task_conclusion(tmp_path, status, exception):
    code = """
import runpy
import sys
import types

db_path, profile_path, task_id, status, exception = sys.argv[1:]
async def run_engine(db, args, stop, waiting):
    from backend import crawler
    db.update_task(task_id, status=status, collection_complete=status == 'completed', error='已保存的结论')
    raise getattr(crawler, exception)('迟到的退出异常')
engine = types.ModuleType('backend.engine')
engine.run_engine = run_engine
sys.modules[engine.__name__] = engine
sys.argv = ['backend.crawler', '--db', db_path, '--profile', profile_path, '--task', task_id]
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
"""
    db_path = tmp_path / 'worker.sqlite3'
    db = Database(db_path)
    task = db.create_task('')
    result = subprocess.run(
        [sys.executable, '-c', code, str(db_path), str(tmp_path / 'profile'), task['id'], status, exception],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8',
        timeout=15, env={**os.environ, 'PYTHONUTF8': '1'},
    )
    assert result.returncode == (0 if exception == 'StopRequested' else 1), result.stderr
    saved = db.get_task(task['id'])
    assert saved['status'] == status and saved['error'] == '已保存的结论'
    assert saved['collection_complete'] == (status == 'completed')


def test_cli_rejects_unsupported_likes_range_before_adapter_creation(tmp_path):
    code = """
import runpy
import sys
import types

db_path, profile_path, task_id = sys.argv[1:]
class ForbiddenAdapter:
    def __init__(self, *args):
        print('unexpected transport creation')
        raise AssertionError('preflight must run first')
adapter = types.ModuleType('backend.douyin_adapter')
adapter.DouyinAdapter = ForbiddenAdapter
sys.modules[adapter.__name__] = adapter
sys.argv = ['backend.crawler', '--db', db_path, '--profile', profile_path, '--task', task_id]
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
"""
    db_path = tmp_path / 'worker.sqlite3'
    db = Database(db_path)
    task = db.create_task('', kind='likes', target_account='test.account', start_time=0)
    db.save_likes_page(task['id'], [], 100, False)
    checkpoint = db.get_checkpoint(task['id'], 'likes')
    result = subprocess.run(
        [sys.executable, '-c', code, str(db_path), str(tmp_path / 'profile'), task['id']],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8',
        timeout=15, env={**os.environ, 'PYTHONUTF8': '1'},
    )
    assert result.returncode == 1, result.stderr
    assert 'unexpected transport creation' not in result.stdout + result.stderr
    assert '点赞时间' in db.get_task(task['id'])['error']
    assert db.get_checkpoint(task['id'], 'likes') == checkpoint


@pytest.mark.parametrize("exception,status,exit_code", [
    ("StopRequested", "logged_out", 0),
    ("PageFailure", "error", 1),
    ("ManualActionRequired", "error", 1),
    ("ValueError", "error", 1),
])
@pytest.mark.parametrize("task_kind", [None, "comments", "likes"])
def test_cli_adapter_startup_failures_preserve_exception_identity(tmp_path, exception, status, exit_code, task_kind):
    # runpy uses the same __main__ module identity as `python -m`; the injected
    # adapter imports exceptions exactly as the real adapter does after startup.
    code = """
import runpy
import sys
import types

kind, db_path, profile_path, task_id = sys.argv[1:]

class FakeAdapter:
    def __init__(self, *args):
        pass

    async def __aenter__(self):
        from backend.crawler import StopRequested, PageFailure, ManualActionRequired
        exceptions = dict(StopRequested=StopRequested, PageFailure=PageFailure,
                          ManualActionRequired=ManualActionRequired, ValueError=ValueError)
        raise exceptions[kind]('safe startup reason' if kind in ('PageFailure', 'ManualActionRequired') else 'private-cookie-token')

    async def __aexit__(self, *args):
        pass

adapter = types.ModuleType('backend.douyin_adapter')
adapter.DouyinAdapter = FakeAdapter
sys.modules[adapter.__name__] = adapter
sys.argv = ['backend.crawler', '--db', db_path, '--profile', profile_path]
if task_id:
    sys.argv.extend(['--task', task_id])
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
"""
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "worker.sqlite3"
    db = Database(db_path)
    task = db.create_task('', kind=task_kind, target_account='test.account') if task_kind else None
    result = subprocess.run(
        [sys.executable, "-c", code, exception, str(db_path), str(tmp_path / "profile"), task['id'] if task else ''],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=15,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == exit_code, result.stderr
    state = db.session_get()
    # Stopping a task preserves the login session; stopping login logs out.
    assert state["status"] == ('waiting' if task and exception == 'StopRequested' else status)
    if exception in {"PageFailure", "ManualActionRequired"}:
        assert state["error"] == "safe startup reason"
    else:
        assert "private-cookie-token" not in str(state)
    assert "private-cookie-token" not in result.stdout + result.stderr
    if task:
        saved = db.get_task(task['id'])
        assert saved['status'] == ('paused' if exception == 'StopRequested' else 'partial')
        assert "private-cookie-token" not in str(saved)
        if exception in {"PageFailure", "ManualActionRequired"}:
            assert saved['error'] == 'safe startup reason'


@pytest.mark.parametrize("task_kind,verify_only", [("comments", False), ("comments", True), ("likes", False)])
def test_cli_dispatches_task_kind_and_closes_context(tmp_path, task_kind, verify_only):
    code = """
import runpy
import sys
import types

db_path, profile_path, task_id, verify_only = sys.argv[1:]

class FakeCollector:
    label = 'comments'
    def __init__(self, db, client, task_id, **kwargs):
        self.db, self.task_id = db, task_id
    async def run(self, verify):
        self.db.update_task(self.task_id, status='completed', collection_complete=True,
                            title=f'{self.label}:{verify}')

class FakeLikesCollector(FakeCollector):
    label = 'likes'

class FakeAdapter:
    def __init__(self, profile, stop, waiting):
        self.waiting = waiting
    async def __aenter__(self):
        # The CLI alias is in sys.modules; runpy need not attach a package attr.
        self.context = self.page = self.client = object()
        self.policy = types.SimpleNamespace(check_control=lambda: None, paused=False)
        return self
    async def __aexit__(self, *args):
        print('context closed')
    async def wait_for_login(self):
        self.waiting(False)

adapter = types.ModuleType('backend.douyin_adapter')
adapter.DouyinAdapter = FakeAdapter
sys.modules[adapter.__name__] = adapter
extensions = types.ModuleType('backend.engine_tasks')
extensions.CommentTask = FakeCollector
extensions.LikesTask = extensions.RecommendationsTask = FakeLikesCollector
sys.modules[extensions.__name__] = extensions
sys.argv = ['backend.crawler', '--db', db_path, '--profile', profile_path, '--task', task_id]
if verify_only == 'True':
    sys.argv.append('--verify-only')
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
"""
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "worker.sqlite3"
    db = Database(db_path)
    task = db.create_task('', kind=task_kind, target_account='test.account')
    result = subprocess.run(
        [sys.executable, '-c', code, str(db_path), str(tmp_path / 'profile'), task['id'], str(verify_only)],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=15,
        env={**os.environ, 'PYTHONUTF8': '1'},
    )
    assert result.returncode == 0, result.stderr
    assert 'context closed' in result.stdout
    timing = json.loads(next(line.removeprefix('PERFORMANCE ') for line in result.stdout.splitlines()
                             if line.startswith('PERFORMANCE ')))
    assert timing['startup_seconds'] >= 0 and timing['collection_seconds'] >= 0
    assert timing['worker_elapsed_seconds'] == pytest.approx(
        timing['startup_seconds'] + timing['collection_seconds'], abs=.002)
    saved = db.get_task(task['id'])
    assert saved['status'] == 'completed'
    assert saved['title'] == f'{task_kind}:{verify_only}'
    assert db.session_get()['status'] == 'ready'
