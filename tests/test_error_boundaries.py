"""Exceptions must cross module and CLI boundaries without loading old collectors."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.db import Database


ROOT = Path(__file__).resolve().parents[1]


def child(code, *args):
    return subprocess.run(
        [sys.executable, '-c', code, *map(str, args)], cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', timeout=15,
        env={**os.environ, 'PYTHONUTF8': '1'},
    )


def test_control_and_transport_import_without_legacy_collectors():
    result = child('''
import importlib, importlib.abc, sys
class ForbidCollectors(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname in {'backend.crawler', 'backend.likes', 'backend.recommendations'}
                or fullname.split('.')[0] in {'tests', 'baselines', 'support'}):
            raise AssertionError('unexpected legacy import: ' + fullname)
sys.meta_path.insert(0, ForbidCollectors())
for name in ('errors', 'responses', 'preflight', 'stable_policy', 'browser_session',
             'douyin_adapter', 'engine', 'runtime_control', 'normalization', 'activity',
             'identity', 'verification', 'engine_storage', 'engine_tasks', 'worker'):
    importlib.import_module('backend.' + name)
assert not any(name.startswith('media_platform.') for name in sys.modules)
''')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('preload', [False, True])
@pytest.mark.parametrize('kind,status,exit_code,reason', [
    ('stop', 'paused', 0, None),
    ('protection', 'paused', 0, 'protection pause'),
    ('network', 'paused', 0, '网络或平台服务连续 5 次暂时故障'),
    ('login', 'partial', 1, 'login required'),
    ('verification', 'partial', 1, 'manual verification'),
    ('rate_limit', 'partial', 1, 'rate limited'),
    ('page', 'partial', 1, 'invalid page'),
    ('permanent', 'partial', 1, '采集进程未能启动或浏览器已关闭'),
])
def test_cli_catches_shared_exceptions_with_or_without_preimport(
        tmp_path, preload, kind, status, exit_code, reason):
    db = Database(tmp_path / 'worker.sqlite3')
    task = db.create_task('')
    db.save_page(task['id'], '', [], 42, False)
    checkpoint = db.get_checkpoint(task['id'])
    result = child('''
import runpy, sys, types
db_path, task_id, preload, kind = sys.argv[1:]
if preload == 'True':
    import backend.crawler
async def run_engine(db, args, stop, waiting):
    from backend import crawler, errors, worker
    from backend.responses import (Kind, Outcome, RequestFailure, RequestRejected,
        LoginRequired, VerificationRequired, NetworkPause)
    from backend.preflight import PreflightRejected
    from backend.stable_policy import ProtectionPause
    cli = sys.modules['__main__']
    assert cli is not crawler
    assert cli.worker is crawler.worker is worker.worker
    for name in ('StopRequested', 'PageFailure', 'ManualActionRequired'):
        assert getattr(worker, name) is getattr(errors, name)
    assert issubclass(PreflightRejected, errors.PageFailure)
    assert issubclass(NetworkPause, errors.ControlledStop)
    assert issubclass(ProtectionPause, errors.ControlledStop)
    assert not issubclass(RequestRejected, errors.PageFailure)
    cases = {
        'stop': errors.StopRequested(),
        'protection': ProtectionPause('protection pause', kind='protection'),
        'network': NetworkPause(),
        'login': LoginRequired('login required'),
        'verification': VerificationRequired('manual verification'),
        'rate_limit': RequestFailure(Outcome(Kind.RATE_LIMIT, 'rate limited')),
        'page': errors.PageFailure('invalid page'),
        'permanent': RequestRejected('upstream-private-body', kind=Kind.PERMANENT),
    }
    print('shared exception identity confirmed', flush=True)
    raise cases[kind]
engine = types.ModuleType('backend.engine')
engine.run_engine = run_engine
sys.modules[engine.__name__] = engine
sys.argv = ['backend.crawler', '--db', db_path, '--profile', db_path + '.profile', '--task', task_id]
runpy.run_module('backend.crawler', run_name='__main__', alter_sys=True)
''', db.path, task['id'], preload, kind)
    assert result.returncode == exit_code, result.stderr
    assert 'shared exception identity confirmed' in result.stdout
    saved = db.get_task(task['id'])
    assert saved['status'] == status
    if reason:
        assert reason in saved['error']
    else:
        assert saved['error'] is None
    assert db.get_checkpoint(task['id']) == checkpoint
    assert 'upstream-private-body' not in str(saved) + result.stdout + result.stderr


@pytest.mark.parametrize('entry', ['backend.worker', 'backend.crawler'])
@pytest.mark.parametrize('case', ['unsupported_likes_range', 'already_stopped'])
def test_actual_cli_preserves_checkpoint_before_platform_start(tmp_path, case, entry):
    db = Database(tmp_path / 'worker.sqlite3')
    unsupported = case == 'unsupported_likes_range'
    task = db.create_task('', kind='likes' if unsupported else 'comments',
                          target_account='test.account' if unsupported else '',
                          start_time=0 if unsupported else None)
    key = 'likes' if unsupported else ''
    if unsupported:
        db.save_likes_page(task['id'], [], 42, False)
    else:
        db.save_page(task['id'], '', [], 42, False)
        db.update_task(task['id'], status='running', stop_requested=True)
    checkpoint = db.get_checkpoint(task['id'], key)
    profile = tmp_path / 'profile'
    result = subprocess.run(
        [sys.executable, '-m', entry, '--db', db.path,
         '--profile', str(profile), '--task', task['id']],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=15,
        env={**os.environ, 'PYTHONUTF8': '1'},
    )
    assert result.returncode == (1 if unsupported else 0), result.stderr
    saved = db.get_task(task['id'])
    assert saved['status'] == ('partial' if unsupported else 'paused')
    if unsupported:
        assert '点赞时间' in saved['error']
    assert db.get_checkpoint(task['id'], key) == checkpoint
    assert not profile.exists()
    # Both paths must end before engine diagnostics, browser and HTTP setup.
    assert not (tmp_path / 'collection.jsonl').exists()
