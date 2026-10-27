"""TXT bytes, read snapshots and owned temporary-file lifetimes."""
import asyncio
from contextlib import contextmanager
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc

import anyio
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.exports import ExportResponse, ExportStore, write_batches
from backend.search import COMMENT_FIELDS, VIDEO_FIELDS, Filters, export_txt, export_likes_txt
from test_likes_api import video
from test_search_api import FakeManager, row


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        yield client


def populate(db, kind='comments', count=600, complete=True, **options):
    tid = db.create_task('', '123', kind=kind, **options)['id']
    if kind == 'comments':
        db.save_page(tid, '', [row(str(i), text='謝謝分享' if i % 2 else '另一条评论',
                                 created_at=None if i % 7 == 0 else 1791267788 + i % 3)
                                for i in range(count)], 10, complete)
    else:
        db.save_likes_page(tid, [video(str(i), created_at=None if i % 7 == 0 else 1791267788 + i % 3)
                                 for i in range(count)], 10, complete, checkpoint_key=kind)
    db.update_task(tid, collection_complete=complete)
    return tid


@pytest.mark.parametrize('fields', [COMMENT_FIELDS, VIDEO_FIELDS])
@pytest.mark.parametrize('complete', [True, False])
@pytest.mark.parametrize('empty', [True, False])
def test_writer_literal_format(tmp_path, fields, complete, empty):
    values = ['甲\\乙\r\n丙\r丁\n😊', '', '謝謝分享', '2026-10-06 14:23:08', None]
    items = [] if empty else [dict(zip((field for _, field in fields), values))]
    path = tmp_path / 'export.txt'
    write_batches(path, iter([items]), fields, complete, lambda: None)
    # Expected bytes are independent of the production formatting helpers.
    labels = ['用户名', '抖音号', '评论内容', '时间', 'IP 属地'] if fields == COMMENT_FIELDS else [
        '视频标题', '作者名称', '作者抖音号', '发布时间', '视频链接']
    body = ''
    if not complete:
        body = '采集尚未完成，仅导出当前已获取的匹配结果。'
    if not empty:
        body += '\r\n\r\n' if body else ''
        body += '\r\n'.join(f'{label}：{value}' for label, value in zip(labels, [
            '甲\\\\乙\\n丙\\n丁\\n😊', '未提供', '謝謝分享', '2026-10-06 14:23:08', '未提供']))
    assert path.read_bytes() == b'\xef\xbb\xbf' + (body + ('\r\n' if body else '')).encode('utf-8')


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('complete', [True, False])
def test_api_batches_keep_bytes_and_release_files(client, monkeypatch, kind, complete):
    db = client.app.state.db
    tid = populate(db, kind, complete=complete, list_order='activity')
    with db.task_snapshot(tid) as snapshot:
        expected = export_txt(snapshot.comments(Filters())[0], complete) if kind == 'comments' else export_likes_txt(snapshot.videos()[0], complete)
    def forbidden(*args, **kwargs):
        pytest.fail('Export must not materialize all results')
    monkeypatch.setattr(db, '_read_comment_page', forbidden)
    monkeypatch.setattr(db, '_read_liked_videos', forbidden)
    name = '_iter_comment_batches' if kind == 'comments' else '_iter_video_batches'
    original = getattr(db, name)
    sizes = []
    def bounded(*args):
        for batch in original(*args):
            sizes.append(len(batch))
            yield batch
    monkeypatch.setattr(db, name, bounded)
    response = client.get(f'/api/tasks/{tid}/export.txt', headers={'Range': 'bytes=0-2'})
    assert response.status_code == 200 and response.content == expected
    assert response.headers['content-length'] == str(len(expected))
    assert response.headers['content-type'] == 'text/plain; charset=utf-8'
    assert response.headers['cache-control'] == 'no-store'
    assert sizes == [256, 256, 88]
    assert not list(client.app.state.exports.root.iterdir())


@pytest.mark.parametrize('warm', [True, False])
@pytest.mark.parametrize('options', [
    {'keyword': '谢谢'}, {'keyword': '谢谢分亨', 'mode': 'fuzzy', 'threshold': 70},
    {'keyword': '谢谢', 'account': 'test.account', 'start': '2026-10-06', 'end': '2026-10-06'},
    {'keyword': '不匹配'},
])
def test_export_filters_with_and_without_cache(client, monkeypatch, warm, options):
    db = client.app.state.db
    tid = populate(db)
    filters = Filters(**options)
    with db.task_snapshot(tid) as snapshot:
        expected = export_txt(snapshot.comments(filters)[0])
    if not warm:
        db.keyword_cache.clear()
    else:
        monkeypatch.setattr('backend.search.match_text', lambda *a: pytest.fail('Warm export rematched text'))
    response = client.get(f'/api/tasks/{tid}/export.txt', params=options)
    assert response.status_code == 200 and response.content == expected
    assert not list(client.app.state.exports.root.iterdir())


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
def test_between_batches_commit_keeps_original_snapshot(client, monkeypatch, kind):
    db = client.app.state.db
    tid = populate(db, kind, complete=False)
    before = client.get(f'/api/tasks/{tid}/export.txt').content
    name = '_iter_comment_batches' if kind == 'comments' else '_iter_video_batches'
    original = getattr(db, name)
    changed = []
    def interleaved(*args):
        for batch in original(*args):
            yield batch
            if not changed:
                changed.append(True)
                if kind == 'comments':
                    db.save_page(tid, '', [row('new', text='新评论')], 20, True)
                else:
                    db.save_likes_page(tid, [video('new')], 20, True, checkpoint_key=kind)
                db.update_task(tid, collection_complete=True)
    monkeypatch.setattr(db, name, interleaved)
    assert client.get(f'/api/tasks/{tid}/export.txt').content == before
    assert changed and client.get(f'/api/tasks/{tid}/export.txt').content != before


def test_failed_generation_closes_iterator_and_removes_partial_file(client, monkeypatch):
    db = client.app.state.db
    tid = populate(db, count=1)
    closed, connections = [], []
    def broken(con, *args):
        connections.append(con)
        try:
            yield [{'text': 'partial'}]
            raise OSError('private path must not be exposed')
        finally:
            closed.append(True)
    monkeypatch.setattr(db, '_iter_comment_batches', broken)
    response = client.get(f'/api/tasks/{tid}/export.txt')
    assert response.status_code == 500 and 'private path' not in response.text
    assert closed and not list(client.app.state.exports.root.iterdir())
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')


def test_missing_and_private_exports_leave_no_files(client):
    assert client.get('/api/tasks/missing/export.txt').status_code == 404
    for kind in ('likes', 'recommendations'):
        tid = populate(client.app.state.db, kind, count=0)
        client.app.state.db.update_task(tid, visibility='private')
        assert client.get(f'/api/tasks/{tid}/export.txt').status_code == 409
    assert not list(client.app.state.exports.root.iterdir())


async def no_disconnect():
    await asyncio.Future()


def export_endpoint(app):
    return next(route.endpoint for route in app.routes if route.path == '/api/tasks/{task_id}/export.txt')


@pytest.mark.asyncio
async def test_download_releases_sqlite_before_first_byte(tmp_path, monkeypatch):
    app = create_app(tmp_path, manager_factory=FakeManager)
    async with app.router.lifespan_context(app):
        db = app.state.db
        tid = populate(db)
        connections = []
        original = db.task_snapshot
        @contextmanager
        def track(*args):
            with original(*args) as snapshot:
                connections.append(snapshot.con)
                yield snapshot
        monkeypatch.setattr(db, 'task_snapshot', track)
        sent = []
        async def slow_send(message):
            if message['type'] == 'http.response.start':
                with pytest.raises(sqlite3.ProgrammingError):
                    connections[0].execute('SELECT 1')
                db.save_page(tid, '', [row('new')], 20, True)
                with db.connection() as con:
                    assert con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
                assert list(app.state.exports.root.iterdir())
                await asyncio.sleep(0.03)
            sent.append(message)
        await export_endpoint(app)(tid, Filters())({}, no_disconnect, slow_send)
        assert sent[-1]['more_body'] is False
        assert not list(app.state.exports.root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_mode', ['disconnect', 'task', 'scope'])
async def test_cancel_generation_waits_for_reader_exit(tmp_path, monkeypatch, cancel_mode):
    app = create_app(tmp_path, manager_factory=FakeManager)
    async with app.router.lifespan_context(app):
        db = app.state.db
        tid = populate(db, count=1)
        started, exited = threading.Event(), threading.Event()
        connections, sent = [], []
        def blocked(con, task_id, filters, check_cancel):
            connections.append(con)
            started.set()
            try:
                while True:
                    check_cancel()
                    time.sleep(0.005)
                yield []
            finally:
                exited.set()
        monkeypatch.setattr(db, '_iter_comment_batches', blocked)
        disconnect = asyncio.Event()
        async def receive():
            await disconnect.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            sent.append(message)
        response = export_endpoint(app)(tid, Filters())
        with anyio.CancelScope() as scope:
            task = asyncio.create_task(response({}, receive, send))
            assert await anyio.to_thread.run_sync(started.wait, 3)
            if cancel_mode == 'disconnect':
                disconnect.set()
            elif cancel_mode == 'task':
                task.cancel()
            else:
                scope.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert exited.is_set() and not sent
        with pytest.raises(sqlite3.ProgrammingError):
            connections[0].execute('SELECT 1')
        assert not list(app.state.exports.root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['disconnect', 'send_error'])
async def test_interrupted_download_removes_file(tmp_path, failure):
    store = ExportStore(tmp_path / 'exports')
    def generate(job):
        job.path.write_bytes(b'x' * 200000)
        return 'example.txt'
    disconnect = asyncio.Event()
    async def receive():
        await disconnect.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        if message['type'] == 'http.response.body':
            if failure == 'send_error':
                raise OSError('connection closed')
            disconnect.set()
            await asyncio.Future()
    try:
        await ExportResponse(store, generate)({}, receive, send)
    except ExceptionGroup as exc:
        assert failure == 'send_error' and isinstance(exc.exceptions[0], OSError)
    assert not list(store.root.iterdir())


def test_recovery_removes_only_known_owned_files(tmp_path):
    store = ExportStore(tmp_path / 'exports')
    own = store.create()
    own.path.write_bytes(b'incomplete')
    unknown = store.create()
    unknown.path.write_bytes(b'keep')
    (unknown.directory / 'user.txt').write_text('saved')
    bad = store.create()
    (bad.directory / 'owner.json').write_text('{}')
    bad.path.write_bytes(b'keep')
    loose = store.root / 'user-export.txt'
    loose.write_text('saved')
    elsewhere = tmp_path / 'download.txt'
    elsewhere.write_text('saved')
    store.recover()
    assert not own.directory.exists()
    assert unknown.path.read_bytes() == bad.path.read_bytes() == b'keep'
    assert loose.read_text() == elsewhere.read_text() == 'saved'


def test_recovery_preserves_link_and_target(tmp_path):
    store = ExportStore(tmp_path / 'exports')
    job = store.create()
    outside = tmp_path / 'saved.txt'
    outside.write_bytes(b'keep')
    try:
        job.path.symlink_to(outside)
    except OSError:
        pytest.skip('Creating symlinks is unavailable on this host')
    store.recover()
    assert job.path.is_symlink() and outside.read_bytes() == b'keep'


def test_restart_cleans_crashed_export(tmp_path):
    root = tmp_path / 'export-temp'
    script = ('import os,sys; from pathlib import Path; from backend.exports import ExportStore; '
              'store=ExportStore(Path(sys.argv[1])); job=store.create(); '
              'job.path.write_bytes(b"partial export"); os._exit(0)')
    subprocess.run([sys.executable, '-c', script, str(root)], check=True, timeout=10)
    assert len(list(root.iterdir())) == 1
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)):
        assert not list(root.iterdir())
    assert not list(root.iterdir())


@pytest.mark.asyncio
async def test_parallel_exports_have_independent_lifetimes(tmp_path):
    store = ExportStore(tmp_path / 'exports')
    ready = asyncio.Event()
    release = asyncio.Event()
    paths = []
    def generate(job):
        paths.append(job.path)
        job.path.write_bytes(b'content')
        return 'export.txt'
    async def held_send(message):
        if message['type'] == 'http.response.start':
            ready.set()
            await release.wait()
    async def fast_send(message):
        pass
    pending = asyncio.create_task(ExportResponse(store, generate)({}, no_disconnect, held_send))
    await ready.wait()
    await ExportResponse(store, generate)({}, no_disconnect, fast_send)
    assert paths[0] != paths[1] and paths[0].exists() and not paths[1].exists()
    release.set()
    await pending
    assert not list(store.root.iterdir())


@pytest.mark.asyncio
async def test_cancel_interrupts_sql_execution(tmp_path, monkeypatch):
    app = create_app(tmp_path, manager_factory=FakeManager)
    async with app.router.lifespan_context(app):
        tid = populate(app.state.db, count=1)
        started, closed = threading.Event(), threading.Event()
        def expensive(con, *args):
            started.set()
            try:
                con.execute('''WITH RECURSIVE n(x) AS (
                    VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000)
                    SELECT SUM(x) FROM n''').fetchone()
                yield []
            finally:
                closed.set()
        monkeypatch.setattr(app.state.db, '_iter_comment_batches', expensive)
        async def send(message):
            pytest.fail('Cancelled generation sent a response')
        response = export_endpoint(app)(tid, Filters())
        task = asyncio.create_task(response({}, no_disconnect, send))
        assert await anyio.to_thread.run_sync(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert closed.is_set() and not list(app.state.exports.root.iterdir())


def test_output_write_failure_closes_database_iterator(tmp_path, monkeypatch):
    closed = []
    def batches():
        try:
            yield [{'text': 'content'}]
        finally:
            closed.append(True)
    from backend import exports
    def cannot_format(*args):
        raise OSError('write failed')
    monkeypatch.setattr(exports, 'record_block', cannot_format)
    with pytest.raises(OSError):
        write_batches(tmp_path / 'out.tmp', batches(), COMMENT_FIELDS, True, lambda: None)
    assert closed == [True]


def test_cleanup_preserves_windows_reparse_entry(tmp_path, monkeypatch):
    from backend import exports
    store = ExportStore(tmp_path / 'exports')
    job = store.create()
    job.path.write_bytes(b'keep')
    original = exports.regular_path
    monkeypatch.setattr(exports, 'regular_path', lambda path, **kw: False if path == job.path else original(path, **kw))
    store.recover()
    assert job.path.read_bytes() == b'keep'


def test_marker_write_failure_removes_only_new_directory(tmp_path, monkeypatch):
    store = ExportStore(tmp_path / 'exports')
    old = store.create()
    old.path.write_bytes(b'keep')
    def disk_full(marker, output):
        output.write('{')
        raise OSError('disk full')
    monkeypatch.setattr('backend.exports.json.dump', disk_full)
    with pytest.raises(OSError):
        store.create()
    assert list(store.root.iterdir()) == [old.directory]
    assert old.path.read_bytes() == b'keep'


def test_windows_reparse_attribute_is_rejected():
    from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_IFDIR, S_IFREG
    from types import SimpleNamespace
    from backend.exports import regular_path
    for mode in (S_IFDIR, S_IFREG):
        path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=mode, st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT))
        assert not regular_path(path, directory=mode == S_IFDIR)


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
def test_empty_api_export_keeps_bom(client, kind):
    tid = populate(client.app.state.db, kind, count=0)
    response = client.get(f'/api/tasks/{tid}/export.txt')
    assert response.status_code == 200 and response.content == b'\xef\xbb\xbf'


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
def test_export_memory_is_bounded_by_batches(client, kind):
    peaks = []
    db, store = client.app.state.db, client.app.state.exports
    for size in (500, 5000):
        tid = populate(db, kind, count=size)
        job = store.create()
        tracemalloc.start()
        try:
            with db.task_snapshot(tid) as snapshot:
                batches = snapshot.comment_batches(Filters(), job.check_cancel) if kind == 'comments' else snapshot.video_batches(job.check_cancel)
                write_batches(job.path, batches, COMMENT_FIELDS if kind == 'comments' else VIDEO_FIELDS, True, job.check_cancel)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
            store.cleanup(job)
    # Ten times the records must not retain ten times the formatted results.
    # Allow allocator and batch-boundary variation across Python versions.
    assert peaks[1] < peaks[0] * 3
