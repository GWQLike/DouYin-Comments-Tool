"""Independent cursor and reply-growth expectations for the production engine."""
import sqlite3
from contextlib import closing

import pytest

from backend import migrations
from backend.crawler import PageFailure, StopRequested, normalize_comment
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.likes import normalize_liked_video
from backend.upstream import module
from test_crawler import comment, page
from test_engine_integration import Fixture, SimulatedTransport, args_for, create, fixture_signing
from test_likes_collector import likes, video
from test_migrations import legacy_database, backups, snapshot, assert_existing_data_preserved


async def run(db, task, tmp_path, fixture):
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    return runtime


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('order', ['activity', 'publication'])
@pytest.mark.parametrize('failure', ['empty', 'stalled', 'missing_cursor', 'loop', 'duplicate'])
async def test_resume_never_replays_an_unproven_bad_cursor(tmp_path, kind, order, failure):
    db = Database(tmp_path / 'resume.db')
    task = create(db, kind, verify_authors=False, list_order=order)
    tid, fixture = task['id'], Fixture(kind)
    if kind == 'comments':
        key, cursor_key = '', 'cursor'
        db.update_task(tid, title='fixture')
        db.save_page(tid, key, [normalize_comment(comment('old'), '123456789')], 20, False)
        response = lambda ids, cursor: page([comment(cid) for cid in ids], cursor, 1)
    else:
        key, cursor_key = kind, 'max_cursor'
        db.update_task(tid, target_sec_uid='target_sec')
        db.save_likes_page(tid, [normalize_liked_video(video('1'))], 20, False, checkpoint_key=key)
        response = lambda ids, cursor: likes([video(cid) for cid in ids], cursor, 1)
    mapping = {20: response(['2'], 40)}
    if failure == 'empty':
        mapping = {20: response([], 40)}
    elif failure == 'stalled':
        mapping = {20: response(['2'], 20)}
    elif failure == 'missing_cursor':
        mapping[20].pop(cursor_key)
    elif failure == 'loop':
        mapping[40] = response(['3'], 20)
    else:
        mapping[40] = response(['2'], 60)
    if kind == 'comments':
        fixture.main = mapping
    else:
        fixture.pages = mapping
    await run(db, task, tmp_path, fixture)
    result = db.get_task(tid)
    assert result['status'] == 'partial' and not result['collection_complete']
    assert '已保留原断点' in result['error']
    expected_cursor = 40 if failure in ('loop', 'duplicate') else 20
    assert db.get_checkpoint(tid, key) == {'cursor': expected_cursor, 'done': False, 'restarts': 0}
    calls = [int(request.url.params[cursor_key]) for request in fixture.requests if cursor_key in request.url.params]
    assert calls == ([20, 40, 40, 40] if expected_cursor == 40 else [20, 20, 20])
    rows = db.read_comments(tid) if kind == 'comments' else db.read_liked_videos(tid)
    assert ('old' if kind == 'comments' else '1') in {row['comment_id' if kind == 'comments' else 'video_id'] for row in rows}


async def test_transient_empty_page_retries_same_position_then_finishes(tmp_path):
    db = Database(tmp_path / 'empty.db')
    task = create(db, 'comments', verify_authors=False)
    db.update_task(task['id'], title='fixture')
    db.save_page(task['id'], '', [], 20, False)
    fixture = Fixture('comments')
    fixture.main = {20: page([], 40, 1)}
    def handler(request):
        result = fixture(request)
        fixture.main[20] = page([comment('after-empty')], 40, 0)
        return result
    await run(db, task, tmp_path, handler)
    assert db.get_task(task['id'])['collection_complete']
    assert db.get_checkpoint(task['id']) == {'cursor': 40, 'done': True, 'restarts': 0}
    assert [int(r.url.params['cursor']) for r in fixture.requests] == [20, 20]


@pytest.mark.parametrize('error', [PageFailure('temporary fixture failure'), StopRequested()])
async def test_hook_failure_propagates_without_restart(error):
    class Hooks:
        page_error = PageFailure
        checkpoint = lambda self: {'cursor': 42, 'done': False}
        check_stop = lambda self: None
        async def request(self, call):
            raise error
        def restart(self):
            pytest.fail('Unknown failure must not replay')
    with pytest.raises(type(error)):
        await module('media_platform.douyin.pagination').collect_pages(lambda _: None, Hooks())


async def test_cancellation_after_normalization_does_not_commit_or_mark_done():
    class Hooks:
        page_error = PageFailure
        cancelled = False
        checkpoint = lambda self: {'cursor': 42, 'done': False}
        def check_stop(self):
            if self.cancelled:
                raise StopRequested()
        async def request(self, call):
            self.cancelled = True
            return {'rows': [{'comment_id': 'c1'}], 'done': True, 'next_cursor': 0, 'has_cursor': True}
        def commit(self, *args):
            pytest.fail('Cancelled page must not be committed')
    with pytest.raises(StopRequested):
        await module('media_platform.douyin.pagination').collect_pages(lambda _: None, Hooks())


async def test_repeated_identifiers_ignore_order_and_duplicate_rows():
    calls, committed = [], []
    class Hooks:
        page_error = PageFailure
        checkpoint = lambda self: {'cursor': 20, 'done': False}
        check_stop = lambda self: None
        normalize = lambda self, raw, cursor: raw
        filter = lambda self, rows, done: rows
        async def request(self, call):
            return await call()
        def commit(self, rows, cursor, done):
            committed.append((cursor, done))
    async def fetch(cursor):
        calls.append(cursor)
        return {'rows': [{'comment_id': cid} for cid in (['1', '2'] if cursor == 20 else ['2', '1', '1'])],
                'done': False, 'next_cursor': cursor + 20, 'has_cursor': True}
    with pytest.raises(PageFailure, match='重复页'):
        await module('media_platform.douyin.pagination').collect_pages(fetch, Hooks())
    assert calls == [20, 40, 40, 40]
    assert committed == [(40, False)] * 4


def finished_thread(tmp_path, *, declared=1):
    db = Database(tmp_path / 'growth.db')
    tid = db.create_task('fixture', '123456789')['id']
    parent = normalize_comment(comment('parent', replies=declared), '123456789')
    reply = normalize_comment(comment('r1'), '123456789', 'parent')
    db.save_page(tid, '', [parent], 20, True)
    db.save_page(tid, 'parent', [reply], 20, True)
    db.update_task(tid, collection_complete=True)
    return db, tid, parent, reply


def observations(db, tid):
    with db.connection() as con:
        row = con.execute('SELECT reply_count_seen,reply_rows_seen FROM checkpoints WHERE task_id=? AND thread_id=?',
                          (tid, 'parent')).fetchone()
    return tuple(row)


@pytest.mark.parametrize('preview_only', [False, True])
async def test_new_main_page_reopens_and_fetches_full_replies_through_engine(tmp_path, preview_only):
    db, tid, parent, _ = finished_thread(tmp_path)
    db.save_page(tid, '', [], 40, False)
    raw = comment('parent', replies=0 if preview_only else 2)
    if preview_only:
        raw['reply_comment'] = [comment('r2')]
    fixture = Fixture('comments')
    fixture.main = {40: page([raw], 60, 0)}
    fixture.reply = {0: page([comment('r1'), comment('r2')], 20, 0)}
    await run(db, db.get_task(tid), tmp_path, fixture)
    assert db.get_task(tid)['collection_complete']
    assert db.get_checkpoint(tid, 'parent') == {'cursor': 20, 'done': True, 'restarts': 1}
    assert {r['comment_id'] for r in db.read_comments(tid)} == {'parent', 'r1', 'r2'}
    comment_requests = [r for r in fixture.requests if '/comment/list/' in r.url.path]
    assert [int(r.url.params['cursor']) for r in comment_requests] == [40, 0]
    assert comment_requests[1].url.params['comment_id'] == 'parent'


@pytest.mark.parametrize('growth', ['declared', 'preview', 'both'])
async def test_completed_reply_thread_rechecks_only_on_new_evidence(tmp_path, growth):
    db, tid, parent, first = finished_thread(tmp_path)
    new_reply = normalize_comment(comment('r2'), '123456789', 'parent')
    rows = [{**parent, 'reply_count': 2 if growth != 'preview' else 1}]
    if growth != 'declared':
        rows.append(new_reply)
    db.save_page(tid, '', rows, 40, True)
    assert not db.get_task(tid)['collection_complete']
    assert db.get_checkpoint(tid, 'parent') == {'cursor': 0, 'done': False, 'restarts': 1}
    assert [r['comment_id'] for r in db.pending_threads(tid)] == ['parent']
    fixture = Fixture('comments')
    fixture.reply = {0: page([comment('r1'), comment('r2')], 20, 0)}
    await run(db, db.get_task(tid), tmp_path, fixture)
    assert db.get_task(tid)['collection_complete']
    assert {r['comment_id'] for r in db.read_comments(tid)} == {'parent', 'r1', 'r2'}
    assert db.pending_threads(tid) == []
    assert observations(db, tid) == (2 if growth != 'preview' else 1, 2)
    for _ in range(3):
        db.save_page(tid, '', rows, 40, True)
    assert db.get_checkpoint(tid, 'parent') == {'cursor': 20, 'done': True, 'restarts': 1}
    assert db.get_task(tid)['collection_complete']


def test_declared_actual_mismatch_and_count_oscillation_do_not_reopen_forever(tmp_path):
    db, tid, parent, reply = finished_thread(tmp_path, declared=20)
    for declared in (20, 1, 0, 20, 2, 20):
        db.save_page(tid, '', [{**parent, 'reply_count': declared}, reply], 20, True)
        assert db.pending_threads(tid) == [] and db.comments_complete(tid)
        assert db.get_checkpoint(tid, 'parent')['restarts'] == 0
    assert observations(db, tid) == (20, 1)
    db.save_page(tid, '', [{**parent, 'reply_count': 21}], 40, True)
    db.save_page(tid, 'parent', [], 0, True)
    db.save_page(tid, '', [{**parent, 'reply_count': 21}], 40, True)
    assert db.comments_complete(tid)
    assert db.get_checkpoint(tid, 'parent')['restarts'] == 1


def test_growth_during_unfinished_recheck_keeps_partial_cursor(tmp_path):
    db, tid, parent, _ = finished_thread(tmp_path)
    db.save_page(tid, '', [{**parent, 'reply_count': 2}], 40, True)
    db.save_page(tid, 'parent', [normalize_comment(comment('r2'), '123456789', 'parent')], 20, False)
    db.save_page(tid, '', [{**parent, 'reply_count': 3}], 40, True)
    # A later sparse parent cannot remove a thread already awaiting collection.
    db.save_page(tid, '', [{**parent, 'reply_count': 0}], 40, True)
    assert db.get_checkpoint(tid, 'parent') == {'cursor': 20, 'done': False, 'restarts': 1}
    assert len(db.pending_threads(tid)) == 1 and not db.comments_complete(tid)
    db.save_page(tid, 'parent', [], 40, True)
    db.save_page(tid, '', [{**parent, 'reply_count': 3}], 40, True)
    assert db.pending_threads(tid) == [] and observations(db, tid) == (3, 2)


def test_page_and_growth_invalidation_roll_back_together(tmp_path):
    db, tid, parent, _ = finished_thread(tmp_path)
    before = snapshot(db.path)
    # Failure after the growth decision must undo both the page and its reopen.
    with pytest.raises(sqlite3.IntegrityError):
        db.save_page(tid, '', [{**parent, 'reply_count': 2}, {'comment_id': 'invalid', 'video_id': None}], 40, True)
    assert snapshot(db.path) == before
    assert db.get_task(tid)['collection_complete']


@pytest.mark.parametrize('version', [0, 1])
def test_historical_reply_observations_migrate_without_reopening_and_restore(tmp_path, version):
    path = tmp_path / 'historical.db'
    legacy_database(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute(f'PRAGMA user_version={version}')
        con.execute("UPDATE checkpoints SET done=1 WHERE task_id='comments' AND thread_id='c1'")
        con.commit()
    before = snapshot(path)
    db = Database(path)
    backup, = backups(path)
    assert snapshot(backup) == before
    assert_existing_data_preserved(backup, path)
    assert db.get_checkpoint('comments', 'c1') == {'cursor': 20, 'done': True, 'restarts': 0}
    with db.connection() as con:
        assert tuple(con.execute("SELECT reply_count_seen,reply_rows_seen FROM checkpoints WHERE thread_id='c1'").fetchone()) == (2, 1)
        assert tuple(con.execute("SELECT reply_count_seen,reply_rows_seen FROM checkpoints WHERE thread_id='likes'").fetchone()) == (None, None)
        plan = con.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM comments WHERE task_id=? AND parent_id=?", ('comments', 'c1')).fetchall()
        assert any('comments_parent' in str(tuple(row)) for row in plan)
    Database(path)
    assert backups(path) == [backup]
    restored = tmp_path / 'restored.db'
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target:
        source.backup(target)
    restored_db = Database(restored)
    assert_existing_data_preserved(backup, restored)
    parent = dict(next(r for r in restored_db.read_comments('comments') if r['comment_id'] == 'c1'))
    restored_db.save_page('comments', '', [parent], 40, False)
    assert restored_db.get_checkpoint('comments', 'c1')['done']
    restored_db.save_page('comments', '', [{**parent, 'reply_count': 3}], 40, False)
    assert not restored_db.get_checkpoint('comments', 'c1')['done']


def test_version_two_failure_rolls_back_observations_and_keeps_backup(tmp_path, monkeypatch):
    path = tmp_path / 'rollback.db'
    legacy_database(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute('PRAGMA user_version=1')
    before = snapshot(path)
    real = migrations.MIGRATIONS[1].apply
    def interrupted(con):
        real(con)
        raise RuntimeError('fixture after adding reply observations')
    monkeypatch.setattr(migrations, 'MIGRATIONS', (migrations.MIGRATIONS[0], migrations.Migration(2, interrupted)))
    with pytest.raises(migrations.DatabaseMigrationError):
        Database(path)
    assert snapshot(path) == snapshot(backups(path)[0]) == before
