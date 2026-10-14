import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import Database
from backend.search import export_likes_txt
from test_search_api import FakeManager


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        yield client


def video(vid='9000000000000000001', **fields):
    return dict(video_id=vid, title='公開作品\n第二行', author_name='作者原名',
                author_douyin_id='author.demo', created_at=1791267788,
                video_url=f'https://www.douyin.com/video/{vid}',
                author_key='u1', uid='u1', sec_uid='sec1') | fields


def seed(client, count=1):
    task = client.post('/api/likes/tasks', json={'account':'@target.demo'}).json()
    db = client.app.state.db
    db.save_likes_page(task['id'], [video(str(9000000000000000001+i), created_at=1791267788+i)
                                  for i in range(count)], 0, True)
    db.update_task(task['id'], collection_complete=True, visibility='public', status='completed')
    return task['id']


def test_likes_create_and_comment_routes_remain_distinct(client):
    tid = seed(client)
    task = client.get('/api/tasks').json()[0]
    assert task['kind'] == 'likes' and task['target_account'] == 'target.demo'
    assert task['video_count'] == 1 and task['main_count'] == 0
    assert client.get(f'/api/tasks/{tid}/comments').status_code == 400
    assert client.post(f'/api/tasks/{tid}/verify').status_code == 400
    comment = client.post('/api/tasks', json={'url':'https://www.douyin.com/video/123'}).json()
    assert comment['kind'] == 'comments' and comment['video_count'] == 0
    assert client.get(f"/api/tasks/{comment['id']}/videos").status_code == 400


@pytest.mark.parametrize('account', ['', '   ', '@', '含 空格', 'a\nb', 'https://www.douyin.com/user/x', '@@abc', 'a'*129])
def test_likes_reject_invalid_account(client, account):
    assert client.post('/api/likes/tasks', json={'account':account}).status_code == 400


def test_likes_pagination_sort_export_all_fields_and_records(client):
    tid = seed(client, 55)
    db = client.app.state.db
    db.save_likes_page(tid, [video('9000000000000000999', created_at=None, author_key='u2',
                                author_name='', author_douyin_id='', uid='u2', sec_uid='sec2')], 0, True)
    first = client.get(f'/api/tasks/{tid}/videos').json()
    assert first['total'] == 56 and len(first['items']) == 50
    assert first['items'][0]['video_id'] == str(9000000000000000001)
    assert 'uid' not in first['items'][0] and 'author_key' not in first['items'][0]
    second = client.get(f'/api/tasks/{tid}/videos?page=2').json()
    assert len(second['items']) == 6 and second['items'][-1]['time_text'] == '未提供'
    response = client.get(f'/api/tasks/{tid}/export.txt?keyword=不存在&page=2')
    assert response.status_code == 200 and response.content.startswith(b'\xef\xbb\xbf')
    content = response.content.decode('utf-8-sig')
    blocks = content.rstrip('\r\n').split('\r\n\r\n')
    assert len(blocks) == 56
    assert all(len(block.split('\r\n')) == 5 for block in blocks)
    assert [line.split('：')[0] for line in blocks[0].split('\r\n')] == ['视频标题','作者名称','作者抖音号','发布时间','视频链接']
    assert '公開作品\\n第二行' in blocks[0]
    assert '作者抖音号：未提供' in blocks[-1]
    assert '评论内容：' not in content


def test_likes_resume_keeps_hydration_and_checkpoint_atomic(client):
    tid = seed(client)
    db = client.app.state.db
    db.save_likes_page(tid, [video(author_douyin_id='', author_name='')], 12, False)
    assert db.read_liked_videos(tid)[0]['author_douyin_id'] == 'author.demo'
    assert db.get_checkpoint(tid, 'likes')['cursor'] == 12
    assert db.get_task(tid)['video_count'] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.save_likes_page(tid, [video('added'), video(None)], 30, False)
    assert db.get_checkpoint(tid, 'likes')['cursor'] == 12
    assert db.get_task(tid)['video_count'] == 1
    # A changed author identity must not inherit the earlier author's public ID.
    db.save_likes_page(tid, [video(author_key='u3', uid='u3', sec_uid='sec3', author_douyin_id='')], 0, True)
    assert db.get_task(tid)['unknown_authors'] == 1
    db.save_liked_author(tid, 'u3', author_douyin_id='confirmed.author')
    assert db.get_task(tid)['unknown_authors'] == 0


def test_private_state_refresh_and_delete(client):
    task = client.post('/api/likes/tasks', json={'account':'private.demo'}).json()
    tid = task['id']
    db = client.app.state.db
    db.update_task(tid, status='private', visibility='private', error='该账号未公开喜欢列表')
    data = client.get(f'/api/tasks/{tid}/videos').json()
    assert data['visibility'] == 'private' and data['total'] == 0
    assert client.get(f'/api/tasks/{tid}/export.txt').status_code == 409
    refreshed = client.post(f'/api/tasks/{tid}/refresh').json()
    assert refreshed['id'] != tid and refreshed['kind'] == 'likes'
    assert refreshed['target_account'] == 'private.demo' and refreshed['visibility'] == 'unknown'
    db.save_likes_page(tid, [video()], 10, False)
    assert client.delete(f'/api/tasks/{tid}').status_code == 200
    assert db.read_liked_videos(tid) == []
    assert db.get_checkpoint(tid, 'likes')['cursor'] == 0


def test_old_comment_database_migrates_without_losing_data(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,source_url TEXT NOT NULL,video_id TEXT DEFAULT '',
                title TEXT DEFAULT '',status TEXT DEFAULT 'created',created_at REAL,updated_at REAL,
                error TEXT,stop_requested INTEGER DEFAULT 0,collection_complete INTEGER DEFAULT 0,
                verification_complete INTEGER DEFAULT 0
            );
            INSERT INTO tasks(id,source_url,video_id,title) VALUES('old','https://www.douyin.com/video/123','123','旧评论任务');
        """)
    db = Database(path)
    assert db.get_task('old')['kind'] == 'comments'
    assert db.get_task('old')['title'] == '旧评论任务'
    assert Database(path).get_task('old')['video_id'] == '123'


def test_partial_likes_export_keeps_missing_data_honest():
    content = export_likes_txt([{'title':'作品', 'video_url':'https://www.douyin.com/video/1'}], False).decode('utf-8-sig')
    assert content.startswith('采集尚未完成')
    assert '作者抖音号：未提供' in content and '发布时间：未提供' in content
