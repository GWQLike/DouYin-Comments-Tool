"""Independent expectations for sparse comments, author links and version 3."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from backend import migrations
from backend.crawler import normalize_comment
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.engine_storage import CommentPages
from backend.search import Filters, export_txt, select_comments
from test_crawler import comment, page
from test_engine_integration import Fixture, SimulatedTransport, args_for, fixture_signing
from test_migrations import backups, legacy_database, snapshot


@pytest.fixture
def saved(tmp_path):
    db = Database(tmp_path / 'comments.db')
    tid = db.create_task('fixture', 'video')['id']
    return db, tid


def save(db, tid, raw, *, preview=False, parent='parent', cursor=20):
    db.save_page(tid, parent, [normalize_comment(raw, 'video', parent, preview=preview)], cursor, False)
    return next(row for row in db.read_comments(tid) if row['comment_id'] == raw['cid'])


@pytest.mark.parametrize('user', [None, {}, [], {'uid': '0', 'sec_uid': '0', 'unique_id': '0'}])
@pytest.mark.parametrize('preview', [True, False])
def test_missing_or_empty_fields_preserve_full_comment_and_author(saved, user, preview):
    db, tid = saved
    expected = save(db, tid, comment('reply', text='完整正文'))
    actual = save(db, tid, {'cid': 'reply', 'user': user, 'create_time': None, 'ip_label': ''}, preview=preview)
    for field in ('text', 'created_at', 'ip_label', 'nickname', 'uid', 'sec_uid',
                  'douyin_id', 'author_key', 'resolved_state', 'reply_count', 'text_source'):
        assert actual[field] == expected[field]
    assert db.get_task(tid)['unknown_authors'] == 0
    assert [a['author_key'] for a in db.authors(tid, False)] == ['u1']


def test_preview_never_replaces_full_fields_even_after_restart(saved):
    db, tid = saved
    original = save(db, tid, comment('reply', text='完整的多段正文', stamp=1720000000))
    db = Database(db.path)
    preview = comment('reply', text='完整的…', stamp=1720000001)
    preview['ip_label'] = '其他地区'
    preview['user']['nickname'] = '预览昵称'
    actual = save(db, tid, preview, preview=True)
    assert actual['text'] == '完整的多段正文'
    assert actual['created_at'] == 1720000000
    assert actual['ip_label'] == '广东'
    assert actual['display_name'] == '测试用户'
    assert actual['text_source'] == 'full'
    assert actual == original


@pytest.mark.parametrize('text', ['更新后的完整正文', ''])
def test_full_response_can_explicitly_edit_text_but_null_is_not_an_edit(saved, text):
    db, tid = saved
    save(db, tid, comment('reply'))
    changed = comment('reply', text=text, stamp=1720000100)
    changed['ip_label'] = '浙江'
    changed['user']['nickname'] = '新昵称'
    actual = save(db, tid, changed)
    assert (actual['text'], actual['created_at'], actual['ip_label'], actual['nickname']) == (text, 1720000100, '浙江', '新昵称')
    save(db, tid, {'cid': 'reply', 'text': None})
    result = save(db, tid, {'cid': 'reply', 'text': '旧预览'}, preview=True)
    assert result['text'] == text
    assert result['text_source'] == 'full'


def test_preview_then_full_then_repeated_sparse_pages_keep_export_and_counts(saved):
    db, tid = saved
    sparse = {'cid': 'reply', 'text': '预览', 'user': {'nickname': '预览作者'}}
    save(db, tid, sparse, preview=True)
    assert db.get_task(tid)['unknown_authors'] == 1
    full = save(db, tid, comment('reply', text='完整回复', replies=0))
    assert full['author_key'] == 'u1'
    assert full['text_source'] == 'full'
    expected = export_txt(select_comments(db.read_comments(tid), Filters()), False)
    assert expected == ('\ufeff采集尚未完成，仅导出当前已获取的匹配结果。\r\n\r\n'
        '用户名：测试用户\r\n抖音号：public.one\r\n评论内容：完整回复\r\n'
        '时间：2024-07-03 17:46:40\r\nIP 属地：广东\r\n').encode('utf-8')
    for _ in range(3):
        save(db, tid, sparse, preview=True)
        save(db, tid, {'cid': 'reply'})
    assert export_txt(select_comments(db.read_comments(tid), Filters()), False) == expected
    task = db.get_task(tid)
    assert (task['main_count'], task['reply_count'], task['unknown_authors']) == (0, 1, 0)
    assert db.authors(tid) == []
    assert [a['author_key'] for a in db.authors(tid, False)] == ['u1']
    assert db.list_tasks()[0]['unknown_authors'] == 0
    with db.connection() as con:
        # Old records remain available locally, but no longer enter the queue.
        assert con.execute('SELECT COUNT(*) FROM authors WHERE task_id=?', (tid,)).fetchone()[0] == 2


def test_preview_can_fill_missing_fields_without_claiming_full_text(saved):
    db, tid = saved
    save(db, tid, {'cid': 'reply'})
    actual = save(db, tid, comment('reply', text='已获得的预览'), preview=True)
    assert actual['text_source'] == 'preview'
    assert (actual['created_at'], actual['ip_label'], actual['public_number']) == (1720000000, '广东', 'public.one')
    assert save(db, tid, comment('reply', text='完整正文'))['text'] == '完整正文'


@pytest.mark.parametrize('identity,value', [('uid', 'other'), ('sec_uid', 'other-sec'), ('unique_id', 'other.public')])
def test_conflicting_identity_keeps_original_link_pending_verification(saved, identity, value):
    db, tid = saved
    save(db, tid, comment('reply'))
    changed = comment('reply', text='更新正文')
    changed['user'][identity] = value
    changed['user']['nickname'] = '冲突昵称'
    actual = save(db, tid, changed)
    assert actual['text'] == '更新正文'
    assert (actual['author_key'], actual['uid'], actual['sec_uid'], actual['douyin_id']) == ('u1', 'u1', 's1', 'public.one')
    assert actual['nickname'] == '测试用户'
    assert actual['resolved_state'] == 'conflict'
    assert db.get_task(tid)['unknown_authors'] == 1
    assert [a['author_key'] for a in db.authors(tid)] == ['u1']
    save(db, tid, comment('reply'))
    assert db.authors(tid)[0]['identity_state'] == 'conflict'
    assert select_comments(db.read_comments(tid), Filters(account='public.one')) == []


def test_temporary_upgrade_into_conflicting_existing_author_remains_unverified(saved):
    db, tid = saved
    save(db, tid, comment('existing'))
    save(db, tid, {'cid': 'reply'})
    other = comment('reply')
    other['user']['sec_uid'] = 'conflicting-sec'
    save(db, tid, other)
    assert db.get_task(tid)['unknown_authors'] == 1
    assert len(db.authors(tid, False)) == 1
    assert {row['resolved_state'] for row in db.read_comments(tid)} == {'conflict'}


def test_missing_user_keeps_profile_hydration_and_sec_only_link(saved):
    db, tid = saved
    save(db, tid, comment('reply', user={'sec_uid': 's1'}))
    db.save_author(tid, 's1', uid='u1', sec_uid='s1', douyin_id='public.one', nickname='补齐昵称', identity_state='verified')
    result = save(db, tid, {'cid': 'reply'})
    assert (result['author_key'], result['public_number'], result['display_name']) == ('s1', 'public.one', '补齐昵称')
    result = save(db, tid, comment('reply'))
    assert result['author_key'] == 's1' and result['resolved_state'] == 'verified'
    assert db.authors(tid) == []


def test_resolved_conflict_is_not_reopened_by_stale_stored_fields(saved):
    db, tid = saved
    save(db, tid, comment('reply'))
    changed = comment('reply')
    changed['user']['unique_id'] = 'current.public'
    assert save(db, tid, changed)['resolved_state'] == 'conflict'
    db.save_author(tid, 'u1', uid='u1', sec_uid='s1', douyin_id='current.public', identity_state='verified')
    actual = save(db, tid, {'cid': 'reply'})
    assert actual['resolved_state'] == 'verified' and actual['public_number'] == 'current.public'
    assert save(db, tid, changed)['resolved_state'] == 'verified'
    assert db.authors(tid) == []


def test_orphans_are_excluded_in_each_task_but_referenced_unknowns_remain(saved):
    db, tid = saved
    other = db.create_task('other')['id']
    save(db, tid, comment('verified'))
    save(db, other, comment('unknown', user={'uid': 'orphan', 'sec_uid': 'orphan-sec'}))
    db.save_author(tid, 'orphan', uid='orphan', sec_uid='orphan-sec', identity_state='unknown')
    assert db.get_task(tid)['unknown_authors'] == 0
    assert db.authors(tid) == []
    assert db.get_task(other)['unknown_authors'] == 1
    assert [a['author_key'] for a in db.authors(other)] == ['orphan']
    assert {task['id']: task['unknown_authors'] for task in db.list_tasks()} == {tid: 0, other: 1}


def test_page_fields_author_upgrade_and_checkpoint_roll_back_together(saved):
    db, tid = saved
    save(db, tid, {'cid': 'reply', 'text': '原始预览'}, preview=True)
    before = snapshot(db.path)
    with pytest.raises(sqlite3.IntegrityError):
        db.save_page(tid, 'parent', [normalize_comment(comment('reply'), 'video', 'parent'),
                                   {'comment_id': 'broken', 'video_id': None}], 40, True)
    assert snapshot(db.path) == before
    assert db.get_checkpoint(tid, 'parent')['cursor'] == 20


def test_raw_page_adapter_marks_embedded_previews(saved):
    db, tid = saved
    task = SimpleNamespace(db=db, task_id=tid, request=None, check_stop=None)
    parent = comment('parent', replies=1)
    parent['reply_comment'] = [{'cid': 'reply', 'text': '预览'}]
    result = CommentPages(task, 'video').normalize(page([parent]), 0)
    full, preview = result['rows']
    assert full['_source'] == 'full' and preview['_source'] == 'preview'
    assert set(preview['_present_fields']) == {'text'}


async def test_real_engine_retains_full_reply_through_preview_and_sparse_reply_page(saved, tmp_path):
    db, tid = saved
    raw = comment('r1', text='保留完整回复')
    db.save_page(tid, '', [normalize_comment(raw, 'video', 'm1')], 0, False)
    parent = comment('m1', replies=1, text='主评论')
    parent['reply_comment'] = [{'cid': 'r1', 'text': '保留…'}]
    fixture = Fixture('comments')
    fixture.main = {0: page([parent], 20)}
    fixture.reply = {0: page([{'cid': 'r1', 'reply_comment_total': 0}], 20)}
    args = args_for(tmp_path, db.get_task(tid))
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    rows = {row['comment_id']: row for row in db.read_comments(tid)}
    assert set(rows) == {'m1', 'r1'}
    assert (rows['r1']['text'], rows['r1']['created_at'], rows['r1']['ip_label'], rows['r1']['public_number']) == (
        '保留完整回复', 1720000000, '广东', 'public.one')
    assert db.get_task(tid)['collection_complete']
    assert not any('/profile/other/' in str(r.url) for r in fixture.requests)


async def test_verify_only_queries_referenced_authors_after_upgrade(saved, tmp_path):
    db, tid = saved
    save(db, tid, {'cid': 'reply'})
    save(db, tid, comment('reply', user={'uid': 'author_uid', 'sec_uid': 'author_sec'}))
    db.save_author(tid, 'old-orphan', uid='old-uid', sec_uid='old-sec', identity_state='unknown')
    db.save_author(tid, 'verified-orphan', uid='author_uid', sec_uid='author_sec',
                   douyin_id='stale.public', nickname='孤立旧资料', identity_state='verified')
    before = db.get_checkpoint(tid, 'parent')
    fixture = Fixture('comments')
    args = args_for(tmp_path, db.get_task(tid))
    args.verify_only = True
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    assert len(fixture.requests) == 1
    assert fixture.requests[0].url.params['sec_user_id'] == 'author_sec'
    assert db.get_task(tid)['unknown_authors'] == 0
    assert db.read_comments(tid)[0]['public_number'] == 'author.one'
    assert db.get_checkpoint(tid, 'parent') == before


def historical_v2(path):
    legacy_database(path)
    with closing(sqlite3.connect(path)) as con:
        migrations.MIGRATIONS[0].apply(con)
        migrations.MIGRATIONS[1].apply(con)
        con.execute('PRAGMA user_version=2')
        # Known IDs on a temporary association are deterministic evidence. The
        # other reply lost its IDs and must not be guessed from the nickname.
        con.execute("UPDATE comments SET author_key='comment:c1' WHERE comment_id='c1'")
        con.execute("INSERT INTO authors SELECT task_id,'comment:c1',uid,sec_uid,douyin_id,nickname,identity_state FROM authors WHERE author_key='fixture.uid'")
        con.execute("INSERT INTO authors(task_id,author_key,uid,sec_uid) VALUES('comments','orphan','orphan','orphan-sec')")
        con.execute("UPDATE comments SET nickname='测试作者' WHERE comment_id='c2'")
        con.commit()


def test_version_three_repairs_only_proven_links_preserves_missing_fields_and_restores(tmp_path):
    path = tmp_path / 'v2.db'
    historical_v2(path)
    before = snapshot(path)
    db = Database(path)
    backup, = backups(path)
    assert snapshot(backup) == before
    assert snapshot(path)[0] == 3
    rows = {row['comment_id']: row for row in db.read_comments('comments')}
    assert rows['c1']['author_key'] == 'fixture.uid'
    assert rows['c1']['text_source'] == 'legacy'
    assert rows['c2']['author_key'] == 'comment:c2'
    assert rows['c2']['created_at'] is None and rows['c2']['ip_label'] == ''
    assert db.get_task('comments')['unknown_authors'] == 1
    assert db.get_task('comments')['updated_at'] > 1700000100
    assert [a['author_key'] for a in db.authors('comments')] == ['comment:c2']
    assert db.get_checkpoint('comments') == {'cursor': 40, 'done': False, 'restarts': 1}
    save(db, 'comments', {'cid': 'c1', 'text': '较短预览'}, preview=True, parent='')
    assert db.read_comments('comments')[0]['text'] == '第一条评论'
    after = snapshot(path)
    Database(path)
    assert snapshot(path) == after and backups(path) == [backup]
    restored = tmp_path / 'restored.db'
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as destination:
        source.backup(destination)
    assert snapshot(restored) == before
    restored_db = Database(restored)
    assert restored_db.read_comments('comments')[0]['author_key'] == 'fixture.uid'
    assert restored_db.get_task('comments')['unknown_authors'] == 1


@pytest.mark.parametrize('missing_author', [False, True])
def test_version_three_conflicting_history_stays_pending(tmp_path, missing_author):
    path = tmp_path / 'conflict.db'
    historical_v2(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE authors SET sec_uid='other' WHERE author_key='fixture.uid'")
        if missing_author:
            con.execute("DELETE FROM authors WHERE author_key='comment:c1'")
        con.commit()
    db = Database(path)
    rows = {row['comment_id']: row for row in db.read_comments('comments')}
    assert rows['c1']['author_key'] == 'comment:c1'
    assert rows['c1']['resolved_state'] == 'conflict'
    assert db.get_task('comments')['unknown_authors'] == 2
    with db.connection() as con:
        assert con.execute("SELECT sec_uid FROM authors WHERE author_key='fixture.uid'").fetchone()[0] == 'other'


@pytest.mark.parametrize('identity_source', ['comment', 'linked_author'])
def test_version_three_can_rebuild_missing_canonical_author_from_known_ids(tmp_path, identity_source):
    path = tmp_path / 'missing-author.db'
    historical_v2(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute("DELETE FROM authors WHERE author_key='fixture.uid'")
        if identity_source == 'comment':
            con.execute("DELETE FROM authors WHERE author_key='comment:c1'")
        else:
            con.execute("UPDATE comments SET uid='',sec_uid='' WHERE comment_id='c1'")
        con.commit()
    db = Database(path)
    row = next(row for row in db.read_comments('comments') if row['comment_id'] == 'c1')
    assert (row['author_key'], row['public_number'], row['resolved_state']) == ('fixture.uid', 'fixture.author', 'verified')
    # Linking known identities does not invent lost raw comment fields.
    assert row['uid'] == ('' if identity_source == 'linked_author' else 'fixture.uid')
    assert db.get_task('comments')['unknown_authors'] == 1
    assert {a['author_key'] for a in db.authors('comments', False)} == {'fixture.uid', 'comment:c2'}


def test_version_three_failure_rolls_back_links_sources_and_version(tmp_path, monkeypatch):
    path = tmp_path / 'rollback.db'
    historical_v2(path)
    before = snapshot(path)
    real = migrations.MIGRATIONS[2].apply
    def broken(con):
        real(con)
        raise RuntimeError('fixture migration interruption')
    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:2] + (migrations.Migration(3, broken),))
    with pytest.raises(migrations.DatabaseMigrationError):
        Database(path)
    assert snapshot(path) == snapshot(backups(path)[0]) == before
