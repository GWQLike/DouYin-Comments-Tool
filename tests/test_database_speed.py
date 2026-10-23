"""Persistence/API checks for bounded reads without changing saved results."""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import Database
from backend import search
from test_likes_api import video
from test_search_api import FakeManager, row


def test_history_aggregates_mixed_task_types_in_one_read(tmp_path, monkeypatch):
    db = Database(tmp_path / 'history.sqlite3')
    empty = db.create_task('empty')['id']
    comment = db.create_task('comment')['id']
    liked = db.create_task('likes', kind='likes')['id']
    recommended = db.create_task('recommendations', kind='recommendations')['id']
    db.save_page(comment, '', [row('main'), row('reply', parent_id='main'),
                              row('unverified', author_key='other', uid='other',
                                  sec_uid='other-sec', douyin_id='', identity_state='unknown')], 0, True)
    # Unknown author counts must count authors, not their number of videos.
    db.save_likes_page(liked, [video('v1', author_douyin_id=''),
                               video('v2', author_douyin_id=''),
                               video('v3', author_key='verified')], 0, True)
    db.save_likes_page(recommended, [video('v1')], 0, True, checkpoint_key='recommendations')
    for task_id in (comment, liked, recommended):
        db.update_task(task_id, collection_complete=True)
    for index in range(20):
        db.create_task(f'history-{index}')
    expected = [db.get_task(task['id']) for task in db.list_tasks()]
    calls = []
    statements = []
    original_connection = db.connection

    @contextmanager
    def counted_connection():
        calls.append(True)
        with original_connection() as con:
            con.set_trace_callback(statements.append)
            yield con

    monkeypatch.setattr(db, 'connection', counted_connection)
    actual = db.list_tasks()
    assert actual == expected
    assert len(calls) == 1
    assert len([sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]) == 1
    by_id = {task['id']: task for task in actual}
    assert by_id[empty]['main_count'] == by_id[empty]['video_count'] == 0
    assert by_id[comment]['main_count'] == 2 and by_id[comment]['reply_count'] == 1
    assert by_id[comment]['unknown_authors'] == by_id[liked]['unknown_authors'] == 1
    assert by_id[liked]['video_count'] == 3
    assert by_id[recommended]['verification_complete'] is True
    assert by_id[liked]['verification_complete'] is False
    assert all('stop_requested' not in task for task in actual)


@pytest.mark.parametrize('list_order,expected', [
    ('publication', ['b', 'a', 'c', 'missing']),
    ('activity', ['a', 'missing', 'b', 'c']),
])
def test_database_pagination_and_id_set_preserve_order_and_dedup(tmp_path, list_order, expected):
    db = Database(tmp_path / 'pages.sqlite3')
    task_id = db.create_task('account', kind='likes', list_order=list_order)['id']
    other_id = db.create_task('other', kind='likes')['id']
    db.save_likes_page(task_id, [video('a', created_at=10), video('missing', created_at=None),
                                video('b', created_at=10), video('c', created_at=1)], 4, False)
    db.save_likes_page(task_id, [video('a', created_at=10)], 8, True)
    db.save_likes_page(other_id, [video('unrelated')], 0, True)
    assert db.liked_video_ids(task_id) == {'a', 'b', 'c', 'missing'}
    assert db.liked_video_ids('absent') == set()
    all_rows = db.read_liked_videos(task_id)
    assert [item['video_id'] for item in all_rows] == expected
    assert db.read_liked_videos(task_id, limit=2) == all_rows[:2]
    assert db.read_liked_videos(task_id, limit=2, offset=2) == all_rows[2:]
    assert db.read_liked_videos(task_id, offset=1) == all_rows[1:]
    assert db.read_liked_videos(task_id, limit=2, offset=4) == []
    assert db.read_liked_videos(task_id, limit=0) == []


def test_video_count_and_page_share_snapshot_during_collector_commit(tmp_path, monkeypatch):
    db = Database(tmp_path / 'snapshot.sqlite3')
    task_id = db.create_task('account', kind='likes')['id']
    db.save_likes_page(task_id, [video('1')], 1, False)
    original_read = db._read_liked_videos

    def commit_after_count(con, task_id, **options):
        # A second connection commits after the reader has counted rows but
        # before it selects its page. WAL permits this without blocking.
        db.save_likes_page(task_id, [video('2')], 2, False)
        return original_read(con, task_id, **options)

    monkeypatch.setattr(db, '_read_liked_videos', commit_after_count)
    rows, total = db.read_liked_video_page(task_id, limit=10)
    assert total == 1
    assert [item['video_id'] for item in rows] == ['1']
    assert db.liked_video_ids(task_id) == {'1', '2'}


def test_video_api_opens_snapshot_after_last_committed_page(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        task_id = client.post('/api/likes/tasks', json={'account': 'target.demo'}).json()['id']
        db.save_likes_page(task_id, [video('1')], 1, False)
        original_snapshot = db.task_snapshot

        @contextmanager
        def commit_before_snapshot(task_id):
            db.save_likes_page(task_id, [video('2')], 2, False)
            with original_snapshot(task_id) as snapshot:
                yield snapshot

        monkeypatch.setattr(db, 'task_snapshot', commit_before_snapshot)
        result = client.get(f'/api/tasks/{task_id}/videos').json()
        assert result['total'] == len(result['items']) == 2
        assert [item['video_id'] for item in result['items']] == ['1', '2']


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
def test_video_api_formats_only_requested_page_and_export_stays_complete(tmp_path, monkeypatch, kind):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        task_id = client.post(f'/api/{kind}/tasks', json={'account': 'target.demo'}).json()['id']
        db = client.app.state.db
        db.save_likes_page(task_id, [video(str(index), title=f'作品{index}') for index in range(150)],
                           0, True, checkpoint_key=kind)
        formatted = []
        original_time_text = search.time_text

        def counted_time_text(value):
            formatted.append(value)
            return original_time_text(value)

        monkeypatch.setattr(search, 'time_text', counted_time_text)
        result = client.get(f'/api/tasks/{task_id}/videos?page=3&page_size=2').json()
        assert result['total'] == 150
        assert result['page'] == 3 and result['page_size'] == 2
        assert [item['video_id'] for item in result['items']] == ['4', '5']
        assert len(formatted) == 2
        formatted.clear()
        empty = client.get(f'/api/tasks/{task_id}/videos?page={10**30}&page_size=2')
        assert empty.status_code == 200
        assert empty.json()['items'] == [] and empty.json()['total'] == 150
        assert formatted == []
        exported = client.get(f'/api/tasks/{task_id}/export.txt').content.decode('utf-8-sig')
        assert exported.count('视频标题：') == 150
        assert '视频标题：作品149' in exported
