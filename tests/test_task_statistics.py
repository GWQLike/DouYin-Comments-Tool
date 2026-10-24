"""Independent counter, ordering, migration and query-plan expectations."""
from contextlib import closing, contextmanager
from pathlib import Path
import random
import sqlite3
import subprocess
import sys

import pytest

from backend.db import Database
from backend import migrations
from backend.migrations import DatabaseMigrationError, Migration
from backend.runtime_control import ProcessLease
from backend.search import Filters, export_txt, export_likes_txt, select_comments
from test_search_api import row
from test_likes_api import video
from test_migrations import legacy_database, backups, snapshot, assert_existing_data_preserved


def assert_counts(db, tid):
    # Python sets count active unresolved identities independently of the SQL
    # trigger implementation and the maintenance tool's aggregate queries.
    with db.connection() as con:
        comments = [dict(r) for r in con.execute('SELECT * FROM comments WHERE task_id=?', (tid,))]
        videos = [dict(r) for r in con.execute('SELECT * FROM liked_videos WHERE task_id=?', (tid,))]
        referenced = {r['comment_id'] for r in comments} | {r['video_id'] for r in videos}
        unresolved = set()
        for v in con.execute('SELECT * FROM author_verifications WHERE task_id=?', (tid,)):
            if v['record_id'] in referenced and v['status'] != 'confirmed':
                unresolved.add((v['author_key'], v['source_uid'], v['source_sec_uid']))
        counts = (sum(c['parent_id'] == '' for c in comments),
                  sum(c['parent_id'] not in ('', None) for c in comments), len(videos),
                  sum(c['created_at'] is None for c in comments), len(unresolved))
        saved = con.execute('''SELECT main_count,reply_count,video_count,missing_times,unknown_authors
            FROM task_stats WHERE task_id=?''', (tid,)).fetchone()
        assert tuple(saved) == counts
    assert db.check_statistics(tid) == []
    task = db.get_task(tid)
    assert (task['main_count'], task['reply_count'], task['video_count'], task['unknown_authors']) == (*counts[:3], counts[4])
    assert task['verification_complete'] == (not unresolved)


def version(db, tid):
    return db.get_task(tid)['result_version']


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
def test_duplicate_pages_verification_and_delete_counts(tmp_path, kind):
    db = Database(tmp_path / 'stats.db')
    tid = db.create_task('', kind=kind)['id']
    assert version(db, tid) == 0
    if kind == 'comments':
        records = [row('1', douyin_id='', identity_state='unknown'),
                   row('2', parent_id='1', created_at=None, douyin_id='', identity_state='unknown')]
        save = lambda: db.save_page(tid, '', records, 10, False)
        table, id_column = 'comments', 'comment_id'
    else:
        records = [video('1', author_douyin_id=''), video('2', author_douyin_id='', created_at=None)]
        save = lambda: db.save_likes_page(tid, records, 10, False, checkpoint_key=kind)
        table, id_column = 'liked_videos', 'video_id'
    save()
    assert_counts(db, tid)
    before = version(db, tid)
    save()
    assert version(db, tid) == before
    assert db.get_task(tid)['unknown_authors'] == 1
    db.begin_verifications(tid, db.verification_records(tid))
    assert version(db, tid) == before  # Attempt timestamps are not result content.
    record = db.verification_records(tid)[0]
    db.finish_verification(tid, record, 'confirmed', fields={
        'uid': record['source_uid'], 'sec_uid': record['source_sec_uid'],
        'douyin_id': 'resolved.author', 'nickname': '已核实'})
    assert version(db, tid) > before
    assert db.get_task(tid)['unknown_authors'] == 1  # One of two records remains pending.
    assert_counts(db, tid)
    with db.connection() as con:
        con.execute(f'DELETE FROM {table} WHERE task_id=? AND {id_column}=?', (tid, '2'))
    assert_counts(db, tid)
    assert db.get_task(tid)['unknown_authors'] == 0
    db.delete_task(tid)
    with db.connection() as con:
        for name in ('task_stats', 'verification_groups', table, 'author_verifications'):
            assert con.execute(f'SELECT COUNT(*) FROM {name} WHERE task_id=?', (tid,)).fetchone()[0] == 0


def test_orphan_verification_references_and_shared_record_id(tmp_path):
    db = Database(tmp_path / 'refs.db')
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('1', douyin_id='', identity_state='unknown')], 0, False)
    with db.connection() as con:
        con.execute('DELETE FROM comments WHERE task_id=?', (tid,))
    assert_counts(db, tid)
    before = version(db, tid)
    record = db.verification_records(tid)
    assert not record
    with db.connection() as con:
        con.execute("UPDATE author_verifications SET nickname='orphan change' WHERE task_id=?", (tid,))
    assert version(db, tid) == before
    db.save_page(tid, '', [row('1', douyin_id='', identity_state='unknown')], 0, False)
    # Defensive coverage for a historical record referenced by both tables.
    with db.connection() as con:
        con.execute("INSERT INTO liked_videos(task_id,video_id,video_url,author_key) VALUES(?,'1','fixture','same')", (tid,))
    assert_counts(db, tid)
    with db.connection() as con:
        con.execute('DELETE FROM comments WHERE task_id=?', (tid,))
    assert_counts(db, tid)
    assert db.get_task(tid)['unknown_authors'] == 1
    with db.connection() as con:
        con.execute('DELETE FROM liked_videos WHERE task_id=?', (tid,))
    assert_counts(db, tid)


def test_identity_moves_parent_time_changes_and_failed_verification(tmp_path):
    db = Database(tmp_path / 'changes.db')
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('a', uid='', sec_uid='', author_key='comment:a', douyin_id='', identity_state='unknown'),
                          row('b', douyin_id='', identity_state='unknown')], 0, False)
    for fields in [dict(uid='new', sec_uid='new-sec', author_key='new'), dict(uid='conflict', sec_uid='conflict-sec')]:
        db.save_page(tid, '', [row('a', **fields, douyin_id='', identity_state='unknown')], 0, False)
        assert_counts(db, tid)
    for record in db.verification_records(tid):
        db.finish_verification(tid, record, 'request_failed', 'network')
    assert_counts(db, tid)
    for parent, stamp in [('a', None), (None, 0), ('', 12)]:
        with db.connection() as con:
            con.execute('UPDATE comments SET parent_id=?,created_at=? WHERE task_id=? AND comment_id=?', (parent, stamp, tid, 'b'))
        assert_counts(db, tid)


def test_result_version_noop_metadata_and_atomic_rollback(tmp_path):
    db = Database(tmp_path / 'version.db')
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('1')], 10, False)
    before = version(db, tid)
    db.update_task(tid, status='running', error='test', verify_authors=True)
    db.save_author(tid, 'unused-author', nickname='unused')
    assert version(db, tid) == before
    db.update_task(tid, collection_complete=True)
    assert version(db, tid) > before
    before = version(db, tid)
    db.update_task(tid, collection_complete=True)
    assert version(db, tid) == before
    with pytest.raises(RuntimeError):
        with db.connection() as con:
            con.execute("UPDATE comments SET text='rolled back' WHERE task_id=?", (tid,))
            con.execute("UPDATE author_verifications SET status='pending' WHERE task_id=?", (tid,))
            assert con.execute('SELECT result_version FROM task_stats WHERE task_id=?', (tid,)).fetchone()[0] > before
            raise RuntimeError('rollback fixture')
    assert version(db, tid) == before
    assert db.get_checkpoint(tid)['cursor'] == 10
    assert_counts(db, tid)


def test_random_record_updates_match_independent_counts(tmp_path):
    db = Database(tmp_path / 'random.db')
    tid = db.create_task('')['id']
    rng = random.Random(9)
    for index in range(80):
        cid = str(rng.randrange(12))
        operation = rng.randrange(4)
        if operation == 0:
            with db.connection() as con:
                con.execute('DELETE FROM comments WHERE task_id=? AND comment_id=?', (tid, cid))
        elif operation == 1:
            with db.connection() as con:
                con.execute('UPDATE author_verifications SET status=? WHERE task_id=? AND record_id=?',
                            (rng.choice(['confirmed', 'pending', 'conflict', 'no_query']), tid, cid))
        else:
            author = str(rng.randrange(4))
            db.save_page(tid, '', [row(cid, author_key=author, uid=author, sec_uid='s'+author,
                douyin_id='', identity_state='unknown', parent_id='root' if index % 3 else '',
                created_at=None if index % 2 else index)], index, False)
        assert_counts(db, tid)


def test_stats_check_and_rebuild_are_atomic_and_invalidate_version(tmp_path):
    db = Database(tmp_path / 'repair.db')
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('a', douyin_id='', identity_state='unknown')], 20, False)
    before = version(db, tid)
    with db.connection() as con:
        con.execute('UPDATE task_stats SET main_count=99,unknown_authors=0 WHERE task_id=?', (tid,))
        con.execute('UPDATE verification_groups SET pending_records=7 WHERE task_id=?', (tid,))
    assert db.check_statistics() == [tid]
    assert db.rebuild_statistics(tid) == 1
    assert version(db, tid) > before
    assert db.get_checkpoint(tid)['cursor'] == 20
    assert_counts(db, tid)
    assert Database(db.path).check_statistics() == []


def test_verification_identity_rekey_and_author_changes_invalidate_results(tmp_path):
    db = Database(tmp_path / 'identity.db')
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('a', douyin_id='', identity_state='unknown'),
                          row('b', douyin_id='', identity_state='unknown')], 1, False)
    before = version(db, tid)
    with db.connection() as con:
        con.execute("UPDATE author_verifications SET source_uid='different' WHERE task_id=? AND record_id='b'", (tid,))
    assert db.get_task(tid)['unknown_authors'] == 2
    assert version(db, tid) > before
    assert_counts(db, tid)
    before = version(db, tid)
    with db.connection() as con:
        con.execute("UPDATE authors SET nickname='updated' WHERE task_id=?", (tid,))
    assert version(db, tid) > before
    before = version(db, tid)
    with db.connection() as con:
        con.execute("UPDATE authors SET nickname='updated' WHERE task_id=?", (tid,))
    assert version(db, tid) == before
    with db.connection() as con:
        con.execute("DELETE FROM author_verifications WHERE task_id=? AND record_id='b'", (tid,))
        con.execute("UPDATE author_verifications SET record_id='orphan' WHERE task_id=? AND record_id='a'", (tid,))
    assert_counts(db, tid)
    assert db.get_task(tid)['unknown_authors'] == 0
    before = version(db, tid)
    with db.connection() as con:
        con.execute("UPDATE author_verifications SET record_id='b' WHERE task_id=? AND record_id='orphan'", (tid,))
    assert version(db, tid) > before
    assert_counts(db, tid)


def test_rollback_page_keeps_version_and_membership(tmp_path, monkeypatch):
    from backend import verification
    db = Database(tmp_path / 'page-rollback.db')
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('original')], 10, False)
    before = db.get_task(tid)
    def fail(*args):
        raise RuntimeError('injected verification write failure')
    monkeypatch.setattr(verification, 'sync_record', fail)
    with pytest.raises(RuntimeError):
        db.save_page(tid, '', [row('new')], 20, True)
    assert db.get_task(tid) == before
    assert db.get_checkpoint(tid)['cursor'] == 10
    assert [r['comment_id'] for r in db.read_comments(tid)] == ['original']
    assert_counts(db, tid)


def test_task_history_ties_keep_previous_row_order(tmp_path):
    db = Database(tmp_path / 'task-order.db')
    tids = [db.create_task(str(i))['id'] for i in range(8)]
    with db.connection() as con:
        for i, tid in enumerate(tids):
            con.execute('UPDATE tasks SET created_at=? WHERE id=?', (None if i < 2 else 10 if i < 5 else 20, tid))
    assert [t['id'] for t in db.list_tasks()] == tids[5:] + tids[2:5] + tids[:2]


def test_history_and_single_task_read_only_counter_tables(tmp_path, monkeypatch):
    db = Database(tmp_path / 'history.db')
    for i in range(25):
        tid = db.create_task(str(i))['id']
        db.save_page(tid, '', [row(str(j)) for j in range(10)], 10, False)
    original = db.connection
    tables = set()
    @contextmanager
    def observed():
        with original() as con:
            def authorize(action, first, second, *_):
                if action == sqlite3.SQLITE_READ:
                    tables.add(first)
                return sqlite3.SQLITE_OK
            con.set_authorizer(authorize)
            yield con
    monkeypatch.setattr(db, 'connection', observed)
    assert len(db.list_tasks()) == 25
    assert db.get_task(tid)['main_count'] == 10
    assert tables == {'tasks', 'task_stats'}


@pytest.mark.parametrize('kind', ['comments', 'publication', 'activity'])
def test_order_and_query_plan_keep_nulls_ties_and_legacy_rows(tmp_path, monkeypatch, kind):
    db = Database(tmp_path / 'order.db')
    tid = db.create_task('', kind='comments' if kind == 'comments' else 'likes',
                         list_order='activity' if kind == 'activity' else 'publication')['id']
    stamps = [('a', None), ('b', 0), ('c', 10), ('d', 10), ('e', -1), ('f', None)]
    if kind == 'comments':
        db.save_page(tid, '', [row(cid, created_at=stamp) for cid, stamp in stamps], 0, False)
    else:
        db.save_likes_page(tid, [video(cid, created_at=stamp) for cid, stamp in stamps], 0, False)
        with db.connection() as con:
            con.execute("UPDATE liked_videos SET collected_order=NULL WHERE video_id IN ('a','c','d','e')")
    expected_ids = ['b', 'f', 'd', 'c', 'e', 'a'] if kind == 'activity' else ['d', 'c', 'b', 'e', 'f', 'a']
    queries = []
    original = db.connection
    @contextmanager
    def traced():
        with original() as con:
            con.set_trace_callback(queries.append)
            yield con
    monkeypatch.setattr(db, 'connection', traced)
    if kind == 'comments':
        records = db.read_comments(tid)
        actual = [r['comment_id'] for r in records]
        page = db.read_comment_page(tid, Filters(), limit=3, offset=2)[0]
        assert [r['comment_id'] for r in page] == expected_ids[2:5]
    else:
        records = db.read_liked_videos(tid)
        actual = [r['video_id'] for r in records]
        assert [r['video_id'] for r in db.read_liked_videos(tid, limit=3, offset=2)] == expected_ids[2:5]
    assert actual == expected_ids
    with original() as con:
        for query in queries:
            if query.lstrip().startswith('SELECT') and 'ORDER BY' in query:
                plan = ' '.join(str(tuple(r)) for r in con.execute('EXPLAIN QUERY PLAN ' + query))
                assert 'TEMP B-TREE' not in plan, plan
                assert ('comments_time' if kind == 'comments' else 'likes_sequence' if kind == 'activity' else 'likes_time') in plan


def test_max_order_seeks_index_and_duplicate_pages_keep_order(tmp_path):
    db = Database(tmp_path / 'max.db')
    tid = db.create_task('', kind='recommendations', list_order='activity')['id']
    db.save_likes_page(tid, [video(str(i)) for i in range(400)], 400, False)
    with db.connection() as con:
        query = 'SELECT COALESCE(MAX(collected_order),0) FROM liked_videos WHERE task_id=?'
        plan = ' '.join(str(tuple(r)) for r in con.execute('EXPLAIN QUERY PLAN ' + query, (tid,)))
        assert 'likes_sequence' in plan
        # Load the schema before counting VM work for the query itself.
        steps = []
        con.set_progress_handler(lambda: steps.append(1) and 0, 1)
        assert con.execute(query, (tid,)).fetchone()[0] == 400
        con.set_progress_handler(None, 0)
        assert len(steps) < 35
    before = [r['video_id'] for r in db.read_liked_videos(tid)]
    db.save_likes_page(tid, [video('1'), video('new'), video('2')], 403, False)
    assert [r['video_id'] for r in db.read_liked_videos(tid)] == before + ['new']
    assert_counts(db, tid)


def version_four(path):
    legacy_database(path)
    with closing(sqlite3.connect(path)) as con:
        for migration in migrations.MIGRATIONS[:4]:
            migration.apply(con)
        con.execute('PRAGMA user_version=4')


def test_v4_upgrade_backup_restore_and_repeated_start(tmp_path):
    path = tmp_path / 'v4.db'
    version_four(path)
    before = snapshot(path)
    db = Database(path)
    backup, = backups(path)
    assert snapshot(backup) == before
    assert_existing_data_preserved(backup, path)
    for task in db.list_tasks():
        assert_counts(db, task['id'])
        assert task['result_version'] == 0
    upgraded = snapshot(path)
    Database(path)
    assert snapshot(path) == upgraded and backups(path) == [backup]
    restored = tmp_path / 'restored.db'
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target:
        source.backup(target)
    assert snapshot(restored) == before
    restored_db = Database(restored)
    assert restored_db.list_tasks() == db.list_tasks()
    for task in db.list_tasks():
        tid = task['id']
        if task['kind'] == 'comments':
            assert export_txt(select_comments(db.read_comments(tid), Filters()), False) == export_txt(select_comments(restored_db.read_comments(tid), Filters()), False)
        else:
            assert export_likes_txt(db.read_liked_videos(tid), False) == export_likes_txt(restored_db.read_liked_videos(tid), False)


def test_v5_failure_rolls_back_triggers_counters_and_schema(tmp_path, monkeypatch):
    path = tmp_path / 'rollback.db'
    version_four(path)
    before = snapshot(path)
    original = migrations.MIGRATIONS[-1].apply
    def fail(con):
        original(con)
        con.execute("INSERT INTO tasks(id,source_url) VALUES('rolled-back','')")
        raise RuntimeError('injected after all triggers')
    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:-1] + (Migration(5, fail),))
    with pytest.raises(DatabaseMigrationError):
        Database(path)
    assert snapshot(path) == before
    assert snapshot(backups(path)[0]) == before


def test_missing_trigger_stops_startup(tmp_path):
    db = Database(tmp_path / 'damaged.db')
    with db.connection() as con:
        con.execute('DROP TRIGGER stats_comments_insert')
    with pytest.raises(DatabaseMigrationError, match='统计触发器'):
        Database(db.path)


def test_maintenance_cli_checks_without_repair_and_respects_owner(tmp_path):
    db = Database(tmp_path / 'cli.db')
    tid = db.create_task('')['id']
    with db.connection() as con:
        con.execute('UPDATE task_stats SET video_count=9 WHERE task_id=?', (tid,))
    def run(*args):
        return subprocess.run([sys.executable, '-m', 'backend.check_stats', '--db', db.path, *args],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10).returncode
    assert run() == 1
    assert db.get_task(tid)['video_count'] == 9
    with ProcessLease(tmp_path, 'service'):
        assert run('--rebuild') == 1
    assert db.get_task(tid)['video_count'] == 9
    assert run('--rebuild') == 0
    assert db.get_task(tid)['video_count'] == 0
