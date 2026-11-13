import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from backend.diagnostics import Diagnostics


def records(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_phases_counts_and_allowlist(tmp_path):
    now = [0.0]
    log = Diagnostics(tmp_path, clock=lambda: now[0])
    log.emit('start')
    with log.span('replies'):
        log.add('requests', 3)
        log.add('new_records', 2)
        log.add('duplicate_records', 1)
        log.add('Cookie', 123)
        log.add('requests', float('nan'))
        log.outcome('success')
        log.outcome('secret-response-body')
        now[0] = 4
    log.close()
    row = records(tmp_path / 'collection.jsonl')[-1]
    assert row['stages']['replies'] == {
        'requests': 3, 'new_records': 2, 'duplicate_records': 1,
        'outcomes': {'success': 1, 'error': 1}, 'elapsed_seconds': 4}
    assert 'secret' not in json.dumps(row) and 'Cookie' not in json.dumps(row)


def test_rotation_and_run_identity(tmp_path):
    log = Diagnostics(tmp_path, max_bytes=1024, backups=2)
    first = log.run_id
    for _ in range(100):
        log.add('requests')
        log.emit('periodic')
    log.close()
    files = list(tmp_path.glob('collection.jsonl*'))
    assert len(files) == 3
    assert all(f.stat().st_size <= 1024 for f in files)
    log = Diagnostics(tmp_path)
    assert log.run_id != first
    log.close()


def test_failed_write_is_counted_without_exception_text(tmp_path, monkeypatch, capsys):
    log = Diagnostics(tmp_path)
    def fail(*args):
        raise OSError('secret-path')
    monkeypatch.setattr(log.handler, 'shouldRollover', fail)
    log.emit('periodic')
    assert log.dropped == 1
    assert not capsys.readouterr().err
    log.close()


@pytest.mark.asyncio
async def test_periodic_startup_and_cancel(tmp_path, monkeypatch):
    from backend import engine
    class WaitingEngine:
        def __init__(self, **kwargs):
            pass
        async def start(self):
            time.sleep(.04)
            await asyncio.sleep(60)
    monkeypatch.setattr(engine, 'module', lambda _: SimpleNamespace(DouYinCrawler=WaitingEngine))
    monkeypatch.setattr(engine, 'SUMMARY_INTERVAL', .01)
    args = SimpleNamespace(task=None, verify_only=False)
    runtime = engine.WorkbenchRuntime(SimpleNamespace(path=str(tmp_path/'db')), args, lambda: False, lambda *_: None)
    pending = asyncio.create_task(engine.run_engine(runtime.db, args, runtime.stop, runtime.waiting, runtime=runtime))
    await asyncio.sleep(.06)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    rows = records(tmp_path/'collection.jsonl')
    assert any(r['event'] == 'periodic' and r['phase'] == 'engine_import' for r in rows)
    assert rows[-1]['event'] == 'finish'
    assert rows[-1]['termination'] == 'cancelled'
    assert len({r['run_id'] for r in rows}) == 1


def test_committed_page_counts_include_duplicates(tmp_path):
    from backend.db import Database
    from backend.engine_tasks import CommentTask
    from backend.normalization import normalize_comment
    db = Database(tmp_path/'db')
    task = db.create_task('https://www.douyin.com/video/123456789', '123456789')
    log = Diagnostics(tmp_path)
    client = SimpleNamespace(diagnostics=log)
    extension = CommentTask(db, client, task['id'])
    row = normalize_comment({'cid': 'one', 'text': 'private-comment'}, '123456789', '')
    with extension.phase('main_comments'):
        extension.write(db.save_page, task['id'], '', [row], 20, False)
        extension.write(db.save_page, task['id'], '', [row], 40, True)
    log.close()
    rows = records(tmp_path/'collection.jsonl')
    stage = rows[-1]['stages']['main_comments']
    assert stage['new_records'] == 1 and stage['duplicate_records'] == 1
    assert stage['local_seconds'] >= 0
    assert 'private-comment' not in (tmp_path/'collection.jsonl').read_text()


def test_forced_exit_preserves_last_phase(tmp_path):
    source = "from pathlib import Path; from backend.diagnostics import Diagnostics; import time; d=Diagnostics(Path(__import__('sys').argv[1])); d.set_phase('browser_connect'); d.emit('periodic'); time.sleep(60)"
    child = subprocess.Popen([sys.executable, '-c', source, str(tmp_path)])
    try:
        deadline = time.monotonic()+10
        path = tmp_path/'collection.jsonl'
        while time.monotonic() < deadline:
            if path.exists() and len(records(path)) >= 2:
                break
            time.sleep(.05)
        else:
            pytest.fail('Child did not write diagnostics')
        child.terminate()
        child.wait(timeout=10)
        assert records(path)[-1]['phase'] == 'browser_connect'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected,seconds', [
    ('normal', 'normal_wait_seconds', 1.5),
    ('backoff', 'backoff_wait_seconds', 4),
    ('rate_limit', 'protection_wait_seconds', 60),
])
async def test_wait_categories_are_disjoint(tmp_path, mode, expected, seconds):
    from support.pacing import Clock, policy
    clock = Clock()
    p = policy(tmp_path/'control.json', clock)
    log = Diagnostics(tmp_path, clock=clock)
    p.diagnostics = log
    await p.wait_turn()
    if mode != 'normal':
        p.failure(rate_limited=mode == 'rate_limit')
    await p.wait_turn()
    assert log.stages['startup'][expected] == pytest.approx(seconds)
    assert sum(value for key, value in log.stages['startup'].items() if key.endswith('_wait_seconds')) == pytest.approx(seconds)
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
async def test_real_engine_diagnostic_totals(tmp_path, monkeypatch, kind):
    from support.engine import Fixture, SimulatedTransport, create, args_for
    from backend.db import Database
    from backend.engine import WorkbenchRuntime, run_engine
    from backend.upstream import module
    monkeypatch.setattr(module('media_platform.douyin.help'), 'get_a_bogus_from_js', lambda *args: 'test-sign')
    db = Database(tmp_path/'db')
    task = create(db, kind)
    fixture = Fixture(kind)
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    stages = records(tmp_path/'collection.jsonl')[-1]['stages']
    result = db.get_task(task['id'])
    assert sum(s.get('requests', 0) for s in stages.values()) == len(fixture.requests)
    assert sum(s.get('new_records', 0) for s in stages.values()) == result['main_count']+result['reply_count']+result['video_count']
    assert sum(s.get('outcomes', {}).get('success', 0) for s in stages.values()) == len(fixture.requests)
    if kind != 'comments':
        assert stages['list']['duplicate_records'] == 1
    assert result['collection_complete'] and result['verification_complete']


@pytest.mark.asyncio
async def test_retry_records_failure_once_in_actual_engine(tmp_path, monkeypatch):
    import httpx
    from support.engine import Fixture, SimulatedTransport, create, args_for
    from backend.db import Database
    from backend.engine import WorkbenchRuntime, run_engine
    from backend.upstream import module
    monkeypatch.setattr(module('media_platform.douyin.help'), 'get_a_bogus_from_js', lambda *args: 'test-sign')
    fixture = Fixture('comments')
    failed = False
    def respond(request):
        nonlocal failed
        if request.url.path.endswith('/comment/list/') and not failed:
            failed = True
            fixture.requests.append(request)
            return httpx.Response(503, text='private-response-body')
        return fixture(request)
    db = Database(tmp_path/'db')
    task = create(db, 'comments')
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=respond))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    stage = records(tmp_path/'collection.jsonl')[-1]['stages']['main_comments']
    assert stage['outcomes'] == {'temporary': 1, 'success': 2}
    assert stage['requests'] == 3 and stage['backoff_wait_seconds'] > 0
    assert 'private-response-body' not in (tmp_path/'collection.jsonl').read_text()
