from contextlib import contextmanager

import pytest

from backend.db import Database
from backend import search
from test_search_api import row


@pytest.mark.parametrize('filters', [search.Filters(), search.Filters(account='test.account'),
    search.Filters(keyword='谢谢分享'), search.Filters(keyword='谢谢分亨这里很好', mode='fuzzy', threshold=70),
    search.Filters(start='2026-10-01', end='2026-10-10'), search.Filters(account='test.account', keyword='ABC'),
    search.Filters(account='missing'), search.Filters(keyword='%')])
def test_sql_page_matches_original_search_and_only_formats_page(tmp_path, monkeypatch, filters):
    db = Database(tmp_path / 'page.db')
    tid = db.create_task('sample')['id']
    rows = [row(str(i), created_at=None if i % 11 == 0 else 1791267788 + i,
                author_key=f'u{i % 7}', uid=f'u{i % 7}', sec_uid=f's{i % 7}',
                text='谢谢分享这里很好' if i % 3 else '謝謝分享，裡面ＡＢＣ') for i in range(310)]
    rows += [row('unknown', author_key='unknown', douyin_id='', identity_state='unknown')]
    db.save_page(tid, '', rows, 20, True)
    expected = search.select_comments(db.read_comments(tid), filters)
    original, formatted = search.format_comment, []
    def format_page(row, *args):
        formatted.append(row['comment_id'])
        return original(row, *args)
    monkeypatch.setattr(search, 'format_comment', format_page)
    for offset in (0, 2, 300, 10**30):
        formatted.clear()
        items, total, missing = db.read_comment_page(tid, filters, limit=2, offset=offset)
        assert items == expected[offset:offset+2] and total == len(expected)
        assert len(formatted) == len(items) <= 2
        assert missing == sum(r['created_at'] is None for r in rows)


def test_comment_page_consistent_snapshot_while_writer_commits(tmp_path, monkeypatch):
    db = Database(tmp_path / 'snapshot.db')
    tid = db.create_task('sample')['id']
    db.save_page(tid, '', [row('one')], 1, False)
    connection = db.connection
    @contextmanager
    def concurrent_connection():
        with connection() as con:
            def trace(sql):
                if sql.startswith('SELECT COUNT(*) FROM ('):
                    with connection() as writer:
                        writer.execute('INSERT INTO comments SELECT task_id,?,video_id,parent_id,text,created_at,ip_label,nickname,douyin_id,uid,sec_uid,author_key,identity_state,reply_count FROM comments WHERE comment_id=?', ('two', 'one'))
            con.set_trace_callback(trace)
            yield con
    monkeypatch.setattr(db, 'connection', concurrent_connection)
    items, total, _ = db.read_comment_page(tid, search.Filters(), limit=50)
    assert total == len(items) == 1
    monkeypatch.setattr(db, 'connection', connection)
    assert len(db.read_comments(tid)) == 2


def test_only_pending_threads_and_explicit_completion_are_used(tmp_path):
    db = Database(tmp_path / 'threads.db')
    tid = db.create_task('sample')['id']
    db.save_page(tid, '', [row('done', reply_count=2), row('todo', reply_count=1)], 20, True)
    db.save_page(tid, 'done', [row('reply', parent_id='done')], 20, True)
    assert [r['comment_id'] for r in db.pending_threads(tid)] == ['todo']
    assert not db.comments_complete(tid)
    db.save_page(tid, 'todo', [], 0, True)
    assert db.pending_threads(tid) == [] and db.comments_complete(tid)
