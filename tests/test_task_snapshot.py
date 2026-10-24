"""Concurrent commits must not mix result data and completeness metadata."""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import Database
from backend.search import Filters, export_txt, export_likes_txt, select_comments
from test_likes_api import video
from test_search_api import FakeManager, row


def seed(db, kind):
    task = db.create_task('', '123', kind=kind)
    tid = task['id']
    if kind == 'comments':
        db.save_page(tid, '', [row('1', text='謝謝分享', douyin_id='', identity_state='unknown')], 10, False)
    else:
        db.save_likes_page(tid, [video('1', author_douyin_id='')], 10, False, checkpoint_key=kind)
    db.update_task(tid, status='running')
    return tid


def commit_change(db, tid, kind, change):
    if change == 'collect':
        if kind == 'comments':
            db.save_page(tid, '', [row('2', text='谢谢分享', created_at=None)], 20, True)
        else:
            db.save_likes_page(tid, [video('2')], 20, True, checkpoint_key=kind)
        db.update_task(tid, collection_complete=True, visibility='public', status='completed')
    elif change == 'verify':
        record, = db.verification_records(tid)
        db.finish_verification(tid, record, 'confirmed', fields={
            'uid': record['source_uid'], 'sec_uid': record['source_sec_uid'],
            'douyin_id': 'resolved.author', 'nickname': '已核实作者'})
    else:
        db.delete_task(tid)


def interleave(db, kind, change, monkeypatch):
    name = '_read_comment_page' if kind == 'comments' else '_read_liked_videos'
    original = getattr(db, name)
    writes = []

    def after_metadata(con, tid, *args, **kwargs):
        if not writes:
            writes.append(True)
            commit_change(db, tid, kind, change)
        return original(con, tid, *args, **kwargs)
    monkeypatch.setattr(db, name, after_metadata)
    return writes


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('change', ['collect', 'verify', 'delete'])
def test_api_metadata_and_rows_share_snapshot(tmp_path, monkeypatch, kind, change):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        tid = seed(db, kind)
        route = 'comments' if kind == 'comments' else 'videos'
        before = client.get(f'/api/tasks/{tid}/{route}').json()
        writes = interleave(db, kind, change, monkeypatch)
        response = client.get(f'/api/tasks/{tid}/{route}')
        assert response.status_code == 200 and response.json() == before
        assert writes == [True]
        after = client.get(f'/api/tasks/{tid}/{route}')
        if change == 'delete':
            assert after.status_code == 404
        elif change == 'collect':
            assert after.json()['total'] == 2 and after.json()['collection_complete']
            if kind == 'comments':
                assert after.json()['missing_times'] == 1
        else:
            assert after.json()['unknown_authors'] == 0 and after.json()['verification_complete']
            field = 'douyin_id' if kind == 'comments' else 'author_douyin_id'
            assert after.json()['items'][0][field] == 'resolved.author'


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('change', ['collect', 'verify', 'delete'])
def test_export_uses_same_snapshot_for_warning_and_records(tmp_path, monkeypatch, kind, change):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        tid = seed(db, kind)
        if kind == 'comments':
            expected = export_txt(select_comments(db.read_comments(tid), Filters()), False)
        else:
            expected = export_likes_txt(db.read_liked_videos(tid), False)
        writes = interleave(db, kind, change, monkeypatch)
        response = client.get(f'/api/tasks/{tid}/export.txt')
        assert writes == [True] and response.status_code == 200
        assert response.content == expected


@pytest.mark.parametrize('options', [
    {'keyword': '谢谢'}, {'keyword': '谢谢分享', 'mode': 'fuzzy'},
    {'account': 'test.account', 'start': '2026-10-01', 'end': '2026-10-30'},
])
def test_filter_and_export_match_same_old_data_during_write(tmp_path, monkeypatch, options):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        tid = seed(db, 'comments')
        db.save_page(tid, '', [row('2', text='谢谢分亨')], 20, False)
        filters = Filters(**options)
        expected = select_comments(db.read_comments(tid), filters)
        writes = interleave(db, 'comments', 'collect', monkeypatch)
        result = client.get(f'/api/tasks/{tid}/comments', params=options).json()
        assert result['items'] == expected and result['total'] == len(expected)
        assert not result['collection_complete'] and writes


def test_get_task_pins_counts_to_first_metadata_read(tmp_path, monkeypatch):
    db = Database(tmp_path / 'test.db')
    tid = seed(db, 'comments')
    before = db.get_task(tid)
    format_task = Database._format_task
    writes = []

    def after_row_read(row):
        if not writes:
            writes.append(True)
            commit_change(db, tid, 'comments', 'collect')
        return format_task(row)
    monkeypatch.setattr(Database, '_format_task', staticmethod(after_row_read))
    assert db.get_task(tid) == before and writes
    assert db.get_task(tid)['main_count'] == 2


def test_failed_reader_releases_snapshot(tmp_path):
    db = Database(tmp_path / 'test.db')
    tid = seed(db, 'comments')
    with pytest.raises(ValueError):
        with db.task_snapshot(tid) as snapshot:
            con = snapshot.con
            snapshot.comments(Filters(), limit=-1)
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute('SELECT 1')
    commit_change(db, tid, 'comments', 'collect')
    assert db.get_task(tid)['collection_complete']
