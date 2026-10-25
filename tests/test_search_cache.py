"""Keyword cache correctness, bounded retention and SQLite snapshot isolation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.db import Database, TaskNotFound
from backend import search
from backend.search import Filters, export_txt, select_comments
from backend.search_cache import KeywordMatchCache, match_key
from test_search_api import FakeManager, row


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / 'search.db', keyword_cache=KeywordMatchCache())


def seed(db, count=12):
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row(f'{i:04}', text=f'謝謝分享 {i}', created_at=1791267788+i)
                          for i in range(count)], 10, False)
    return tid


def counter(monkeypatch):
    calls = []
    original = search.match_text

    def match(text, filters):
        calls.append(1)
        return original(text, filters)
    monkeypatch.setattr(search, 'match_text', match)
    return calls


def test_pages_only_match_once_and_fetch_selected_ids(db, monkeypatch):
    tid = seed(db, 600)
    filters = Filters(keyword='谢谢')
    expected = select_comments(db.read_comments(tid), filters)
    calls = counter(monkeypatch)
    assert db.read_comment_page(tid, filters, limit=50) == (expected[:50], 600, 0)
    assert len(calls) == 600
    connection, sql = db.connection, []

    @contextmanager
    def traced():
        with connection() as con:
            con.set_trace_callback(sql.append)
            yield con
    monkeypatch.setattr(db, 'connection', traced)
    items, total, _ = db.read_comment_page(tid, filters, limit=50, offset=250)
    assert items == expected[250:300] and total == 600 and len(calls) == 600
    result_queries = [q for q in sql if 'FROM comments c LEFT JOIN' in q]
    assert len(result_queries) == 1 and 'c.comment_id IN (' in result_queries[0]
    assert 'ORDER BY' not in result_queries[0]
    with db.task_snapshot(tid) as snapshot:
        exported, count, _ = snapshot.comments(filters)
    assert count == 600 and export_txt(exported, False) == export_txt(expected, False)
    assert len(calls) == 600  # Export also reuses the matches, in bounded SQL batches.
    assert db.read_comment_page(tid, filters, limit=0)[0] == []
    assert db.read_comment_page(tid, filters, limit=1, offset=10**100)[0] == []


@pytest.mark.parametrize('options,ids', [
    ({'keyword': '谢谢'}, ['a', 'c']),
    ({'keyword': 'ＡＢＣ'}, ['a']),
    ({'keyword': '这个视频真的非常精采', 'mode': 'fuzzy'}, ['b']),
    ({'keyword': '这个视频真的非常精采', 'mode': 'fuzzy', 'threshold': 100}, []),
    ({'keyword': '谢谢', 'account': '@alice'}, ['a']),
    ({'keyword': '谢谢', 'start': '2026-10-06', 'end': '2026-10-06'}, ['a']),
    ({'keyword': '谢谢', 'start': '2026-10-07'}, []),
    ({'keyword': '谢谢', 'end': '2026-10-05'}, []),
    ({'keyword': '%'}, []),
])
def test_filter_keys_and_independent_semantics(db, monkeypatch, options, ids):
    tid = db.create_task('')['id']
    db.save_page(tid, '', [
        row('a', text='謝謝分享 ＡＢＣ', created_at=1791267788, uid='a', sec_uid='sa', author_key='a', douyin_id='alice'),
        row('b', text='这个视频真的非常精彩', created_at=1791267788, uid='b', sec_uid='sb', author_key='b', douyin_id='bob'),
        row('c', text='谢谢', created_at=1791267790, uid='c', sec_uid='sc', author_key='c', douyin_id='bob'),
    ], 10, False)
    # Date-limited query has one excluded row with an independent known date.
    with db.connection() as con:
        con.execute("UPDATE comments SET created_at=NULL WHERE task_id=? AND comment_id='c'", (tid,))
    filters = Filters(**options)
    calls = counter(monkeypatch)
    db.read_comment_page(tid, Filters(keyword='warmup'), limit=1)
    first = db.read_comment_page(tid, filters, limit=None)
    assert [item['comment_id'] for item in first[0]] == ids
    assert first[1:] == (len(ids), 1)
    scanned = len(calls)
    again = db.read_comment_page(tid, filters, limit=None)
    assert again == first and len(calls) == scanned
    if options.get('mode') == 'fuzzy' and ids:
        assert first[0][0]['match_type'] == 'fuzzy' and 85 <= first[0][0]['score'] < 100


def test_each_filter_dimension_has_its_own_entry(db, monkeypatch):
    tid = seed(db)
    variants = [Filters(keyword='谢谢'), Filters(keyword='分享'), Filters(keyword='谢谢', mode='fuzzy'),
                Filters(keyword='谢谢', threshold=90), Filters(keyword='谢谢', account='test.account'),
                Filters(keyword='谢谢', start='2026-10-01'), Filters(keyword='谢谢', end='2026-10-30')]
    calls = counter(monkeypatch)
    for filters in variants:
        before = len(calls)
        db.read_comment_page(tid, filters, limit=2)
        assert len(calls) == before + 12
        db.read_comment_page(tid, filters, limit=2, offset=2)
        assert len(calls) == before + 12
    assert db.keyword_cache.info()['entries'] == len(variants)


def test_updates_from_another_database_invalidate_and_keep_missing_count(db, monkeypatch):
    tid = seed(db)
    filters = Filters(keyword='谢谢')
    calls = counter(monkeypatch)
    db.read_comment_page(tid, filters, limit=2)
    writer = Database(db.path)  # Isolated collector has no result cache.
    assert writer.keyword_cache is None
    writer.save_page(tid, '', [row('new', text='謝謝', created_at=None)], 20, False)
    items, total, missing = db.read_comment_page(tid, filters, limit=None)
    assert total == 13 and missing == 1 and items[-1]['comment_id'] == 'new'
    assert len(calls) == 25 and db.keyword_cache.info()['entries'] == 1
    writer.save_page(tid, '', [row('0000', text='不再匹配')], 30, False)
    assert db.read_comment_page(tid, filters, limit=2)[1] == 12
    assert len(calls) == 38


def test_author_verification_invalidates_empty_account_match(db, monkeypatch):
    tid = db.create_task('')['id']
    db.save_page(tid, '', [row('a', douyin_id='', identity_state='unknown')], 1, False)
    filters = Filters(keyword='谢谢', account='resolved')
    assert db.read_comment_page(tid, filters, limit=1)[1] == 0
    writer = Database(db.path)
    record, = writer.verification_records(tid)
    writer.finish_verification(tid, record, 'confirmed', fields={
        'uid': record['source_uid'], 'sec_uid': record['source_sec_uid'],
        'douyin_id': 'resolved', 'nickname': '核实结果'})
    items, total, _ = db.read_comment_page(tid, filters, limit=1)
    assert total == 1 and items[0]['douyin_id'] == 'resolved'
    calls = counter(monkeypatch)
    db.read_comment_page(tid, filters, limit=1)
    assert not calls


def test_unchanged_page_and_progress_keep_cached_matches(db, monkeypatch):
    tid = seed(db)
    filters = Filters(keyword='谢谢')
    calls = counter(monkeypatch)
    before = db.read_comment_page(tid, filters, limit=2)
    db.update_task(tid, status='paused', error='test')
    db.save_page(tid, '', [row('0000', text='謝謝分享 0', created_at=1791267788)], 30, False)
    assert db.read_comment_page(tid, filters, limit=2) == before
    assert len(calls) == 12


@pytest.mark.parametrize('warm', [False, True])
@pytest.mark.parametrize('change', ['collect', 'delete'])
def test_pinned_snapshot_during_commit_or_delete(db, warm, change):
    tid = seed(db)
    filters = Filters(keyword='谢谢')
    expected = select_comments(db.read_comments(tid), filters)
    if warm:
        db.read_comment_page(tid, filters, limit=1)
    with db.task_snapshot(tid) as old:
        if change == 'delete':
            db.delete_task(tid)
        else:
            Database(db.path).save_page(tid, '', [row('new', text='谢谢')], 20, False)
        items, total, _ = old.comments(filters)
        assert total == 12 and items == expected
    # A late old snapshot must not republish removed or outdated entries.
    if not warm or change == 'delete':
        assert db.keyword_cache.info()['entries'] == 0
    if change == 'delete':
        with pytest.raises(TaskNotFound), db.task_snapshot(tid):
            pass
    else:
        assert db.read_comment_page(tid, filters, limit=1)[1] == 13


def test_old_snapshot_cannot_replace_newer_entry(db, monkeypatch):
    tid = seed(db)
    filters = Filters(keyword='谢谢')
    with db.task_snapshot(tid) as old:
        Database(db.path).save_page(tid, '', [row('new', text='谢谢')], 20, False)
        assert db.read_comment_page(tid, filters, limit=1)[1] == 13
        assert old.comments(filters, limit=1)[1] == 12
    calls = counter(monkeypatch)
    assert db.read_comment_page(tid, filters, limit=1)[1] == 13
    assert calls == []


def test_delete_during_match_aborts_builder(db, monkeypatch):
    tid = seed(db)
    original = search.match_text
    deleted = []

    def match(text, filters):
        if not deleted:
            deleted.append(True)
            db.delete_task(tid)
        return original(text, filters)
    monkeypatch.setattr(search, 'match_text', match)
    assert db.read_comment_page(tid, Filters(keyword='谢谢'), limit=2)[1] == 12
    assert db.keyword_cache.info()['total_bytes'] == 0


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_failed_or_cancelled_scan_releases_budget(db, monkeypatch, failure):
    tid = seed(db)
    original = search.match_text

    def fail(*args):
        raise failure()
    monkeypatch.setattr(search, 'match_text', fail)
    with pytest.raises(failure):
        db.read_comment_page(tid, Filters(keyword='谢谢'), limit=1)
    assert db.keyword_cache.info()['total_bytes'] == 0
    monkeypatch.setattr(search, 'match_text', original)
    assert db.read_comment_page(tid, Filters(keyword='谢谢'), limit=1)[1] == 12


def test_sql_failure_releases_builder(db, monkeypatch):
    tid = seed(db)
    monkeypatch.setattr(db, 'COMMENT_SELECT', 'SELECT missing FROM comments c')
    with pytest.raises(sqlite3.OperationalError):
        db.read_comment_page(tid, Filters(keyword='谢谢'), limit=1)
    assert db.keyword_cache.info()['total_bytes'] == 0


def test_cache_admission_failure_leaves_valid_results(db, monkeypatch):
    tid = seed(db)
    monkeypatch.setattr(db, '_current_result_version', lambda _: None)
    assert db.read_comment_page(tid, Filters(keyword='谢谢'), limit=1)[1] == 12
    assert db.keyword_cache.info()['total_bytes'] == 0


@pytest.mark.parametrize('limits', [
    {'max_entries': 0}, {'max_bytes': 0}, {'max_entry_bytes': 1600},
])
def test_size_overflow_falls_back_without_losing_results(tmp_path, limits):
    db = Database(tmp_path / 'small.db', keyword_cache=KeywordMatchCache(**limits))
    tid = seed(db, 40)
    filters = Filters(keyword='谢谢')
    expected = select_comments(db.read_comments(tid), filters)
    for offset in (0, 10, 100):
        assert db.read_comment_page(tid, filters, limit=10, offset=offset) == (expected[offset:offset+10], 40, 0)
    assert db.keyword_cache.info()['total_bytes'] == 0


def test_lru_and_budget_include_inflight_matches():
    cache = KeywordMatchCache(max_entries=2, max_bytes=3500, max_entry_bytes=2500)
    keys = [match_key('task', 1, Filters(keyword=f'query{i}')) for i in range(20)]
    for key in keys:
        build = cache.begin(key)
        cache.append(build, 'id', 'contains', 100.0)
        assert cache.info()['total_bytes'] <= cache.max_bytes
        cache.finish(build, lambda: 1)
        assert cache.info()['entries'] <= 2 and cache.info()['total_bytes'] <= cache.max_bytes
    assert cache.get(keys[0]) is None and cache.get(keys[-1]) == (('id', 'contains', 100.0),)
    cache.get(keys[-2])  # Retain the recently read older entry.
    key = match_key('other', 1, Filters(keyword='q'))
    build = cache.begin(key)
    assert cache.begin(keys[0]) is None  # Only one builder can retain candidates.
    cache.finish(build, lambda: 1)
    assert cache.get(keys[-2]) is not None and cache.get(keys[-1]) is None
    cache.clear()
    assert cache.info()['total_bytes'] == 0


def test_parallel_miss_uses_normal_query_without_second_builder(db, monkeypatch):
    tid = seed(db)
    filters = Filters(keyword='谢谢')
    entered, release = threading.Event(), threading.Event()
    original = search.match_text

    def match(text, options):
        if threading.current_thread().name.startswith('cache-test') and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(text, options)
    monkeypatch.setattr(search, 'match_text', match)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='cache-test') as pool:
        future = pool.submit(db.read_comment_page, tid, filters, limit=2)
        try:
            assert entered.wait(5)
            assert db.keyword_cache.info()['building_bytes'] > 0
            expected = db.read_comment_page(tid, filters, limit=2)
        finally:
            release.set()
        assert future.result(timeout=5) == expected
    assert db.keyword_cache.info()['entries'] == 1 and db.keyword_cache.info()['building_bytes'] == 0


def test_task_isolation_bound_ids_and_cache_shutdown(tmp_path):
    cache = None
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        first, second = seed(db, 1), seed(db, 2)
        cache = db.keyword_cache
        strange_id = "id'); DELETE FROM comments; --"
        db.save_page(first, '', [row(strange_id, text='谢谢')], 20, False)
        for tid in (first, second):
            result = client.get(f'/api/tasks/{tid}/comments', params={'keyword': '谢谢'}).json()
            assert result['total'] == 2
            assert client.get(f'/api/tasks/{tid}/comments', params={'keyword': '谢谢'}).json() == result
        assert cache.info()['entries'] == 2
        assert client.delete(f'/api/tasks/{first}').status_code == 200
        assert cache.info()['entries'] == 1
        assert client.get(f'/api/tasks/{first}/comments', params={'keyword': '谢谢'}).status_code == 404
    assert cache.info()['total_bytes'] == 0
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        assert client.app.state.db.keyword_cache.info()['entries'] == 0


def test_queries_without_keyword_do_not_populate_cache(db):
    tid = seed(db)
    assert db.read_comment_page(tid, Filters(), limit=1)[1] == 12
    assert db.keyword_cache.info()['total_bytes'] == 0
