"""API contracts specific to homepage recommendations and shared-list isolation."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from test_likes_api import video
from test_search_api import FakeManager, row


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        yield client


def create_recommendations(client, account='@target.demo'):
    response = client.post('/api/recommendations/tasks', json={'account': account})
    assert response.status_code == 200
    return response.json()


def test_recommendations_keep_rows_authors_and_deletion_separate_from_likes_and_comments(client):
    recommendation = create_recommendations(client, '  @target.demo  ')
    liked = client.post('/api/likes/tasks', json={'account': 'target.demo'}).json()
    comment = client.post('/api/tasks', json={'url': 'https://www.douyin.com/video/123'}).json()
    rid, lid, cid = recommendation['id'], liked['id'], comment['id']
    db = client.app.state.db
    # Identical video and author IDs in two lists must not merge their snapshots.
    db.save_likes_page(rid, [video(title='推荐作品', author_douyin_id='')], 12, False,
                       checkpoint_key='recommendations')
    db.save_likes_page(lid, [video(title='喜欢作品')], 34, False)
    db.save_page(cid, '', [row(text='独立评论')], 0, True)
    tasks = {item['id']: item for item in client.get('/api/tasks').json()}
    assert tasks[rid]['kind'] == 'recommendations'
    assert tasks[rid]['target_account'] == 'target.demo'
    assert tasks[rid]['video_count'] == 1 and tasks[rid]['main_count'] == 0
    assert tasks[rid]['unknown_authors'] == 1
    assert tasks[lid]['kind'] == 'likes' and tasks[lid]['unknown_authors'] == 0
    assert tasks[cid]['kind'] == 'comments' and tasks[cid]['video_count'] == 0
    assert client.get(f'/api/tasks/{rid}/comments').status_code == 400
    assert client.post(f'/api/tasks/{rid}/verify').status_code == 200
    client.post(f'/api/tasks/{rid}/stop')
    assert client.get(f'/api/tasks/{cid}/videos').status_code == 400
    db.save_liked_author(rid, 'u1', author_douyin_id='recommendation.author')
    assert client.get(f'/api/tasks/{rid}/videos').json()['items'][0]['author_douyin_id'] == 'recommendation.author'
    assert client.get(f'/api/tasks/{lid}/videos').json()['items'][0]['author_douyin_id'] == 'author.demo'
    assert '推荐作品' in client.get(f'/api/tasks/{rid}/export.txt').content.decode('utf-8-sig')
    assert '喜欢作品' not in client.get(f'/api/tasks/{rid}/export.txt').content.decode('utf-8-sig')
    assert client.delete(f'/api/tasks/{rid}').status_code == 200
    assert client.get(f'/api/tasks/{rid}/videos').status_code == 404
    assert db.read_liked_videos(rid) == []
    assert db.get_checkpoint(rid, 'recommendations')['cursor'] == 0
    assert client.get(f'/api/tasks/{lid}/videos').json()['items'][0]['title'] == '喜欢作品'
    assert db.get_checkpoint(lid, 'likes')['cursor'] == 34
    assert client.get(f'/api/tasks/{cid}/comments').json()['items'][0]['text'] == '独立评论'


@pytest.mark.parametrize('account', ['', '   ', '@', '@@abc', '含 空格', 'a\nb',
                                     'https://www.douyin.com/user/x', 'a' * 129])
def test_recommendations_validate_public_account_before_creating_task(client, account):
    assert client.post('/api/recommendations/tasks', json={'account': account}).status_code == 400
    assert client.get('/api/tasks').json() == []


def test_recommendations_export_all_pages_in_source_order_with_five_fields(client):
    tid = create_recommendations(client)['id']
    db = client.app.state.db
    videos = [video(str(9000000000000000001 + i), title=f'推荐作品{i}',
                    created_at=1791267788 + i) for i in range(51)]
    videos.append(video('9000000000000000999', title='无日期作品', created_at=None,
                        author_key='unknown', author_name='', author_douyin_id=''))
    # Upstream order need not be publication order.
    db.save_likes_page(tid, videos, 0, True, checkpoint_key='recommendations')
    db.update_task(tid, collection_complete=True, visibility='public', status='completed')
    first = client.get(f'/api/tasks/{tid}/videos').json()
    second = client.get(f'/api/tasks/{tid}/videos?page=2').json()
    assert first['total'] == 52 and len(first['items']) == 50
    assert first['items'][0]['title'] == '推荐作品0'
    assert [item['title'] for item in second['items']] == ['推荐作品50', '无日期作品']
    assert second['items'][-1]['time_text'] == '未提供'
    assert set(first['items'][0]) == {'video_id', 'title', 'author_name', 'author_douyin_id',
                                    'created_at', 'time_text', 'video_url'}
    response = client.get(f'/api/tasks/{tid}/export.txt?page=2&keyword=不应筛掉视频')
    assert response.status_code == 200 and response.content.startswith(b'\xef\xbb\xbf')
    assert 'douyin-recommendations-' in response.headers['content-disposition']
    blocks = response.content.decode('utf-8-sig').rstrip('\r\n').split('\r\n\r\n')
    assert len(blocks) == 52
    labels = ['视频标题', '作者名称', '作者抖音号', '发布时间', '视频链接']
    assert all([line.split('：', 1)[0] for line in block.split('\r\n')] == labels for block in blocks)
    assert blocks[0].split('\r\n') == [
        '视频标题：推荐作品0', '作者名称：作者原名', '作者抖音号：author.demo',
        '发布时间：2026-10-06 14:23:08',
        f"视频链接：https://www.douyin.com/video/{9000000000000000001}",
    ]
    assert blocks[-1].split('\r\n')[1:4] == ['作者名称：未提供', '作者抖音号：未提供', '发布时间：未提供']


@pytest.mark.parametrize('visibility,status,complete', [
    ('unknown', 'created', False), ('unknown', 'partial', False), ('public', 'completed', True),
])
def test_empty_recommendations_are_not_reported_private_without_evidence(client, visibility, status, complete):
    tid = create_recommendations(client)['id']
    client.app.state.db.update_task(tid, visibility=visibility, status=status, collection_complete=complete)
    result = client.get(f'/api/tasks/{tid}/videos').json()
    assert result['total'] == 0 and result['visibility'] == visibility
    response = client.get(f'/api/tasks/{tid}/export.txt')
    assert response.status_code == 200
    text = response.content.decode('utf-8-sig')
    assert '未公开' not in text
    assert ('采集尚未完成' in text) is (not complete)


def test_private_recommendations_use_correct_message_and_refresh_preserves_kind(client):
    tid = create_recommendations(client, 'private.demo')['id']
    db = client.app.state.db
    db.update_task(tid, status='private', visibility='private', error='该账号未公开推荐列表')
    response = client.get(f'/api/tasks/{tid}/export.txt')
    assert response.status_code == 409
    assert response.json()['detail'] == '该账号未公开推荐列表，没有可导出的记录'
    refreshed = client.post(f'/api/tasks/{tid}/refresh').json()
    assert refreshed['id'] != tid and refreshed['kind'] == 'recommendations'
    assert refreshed['target_account'] == 'private.demo'
    assert refreshed['visibility'] == 'unknown' and refreshed['status'] == 'created'
    assert refreshed['video_count'] == 0 and refreshed['error'] is None
    assert db.get_task(tid)['visibility'] == 'private'


def test_recommendation_page_checkpoint_is_distinct_and_commits_with_rows(client):
    tid = create_recommendations(client)['id']
    db = client.app.state.db
    db.reset_checkpoint(tid, 'likes')
    db.save_likes_page(tid, [video()], 42, False, checkpoint_key='recommendations')
    assert db.get_checkpoint(tid, 'recommendations') == {'cursor': 42, 'done': False, 'restarts': 0}
    assert db.get_checkpoint(tid, 'likes') == {'cursor': 0, 'done': False, 'restarts': 1}
    assert db.get_checkpoint(tid, '') == {'cursor': 0, 'done': False, 'restarts': 0}
    with pytest.raises(sqlite3.IntegrityError):
        db.save_likes_page(tid, [video('new'), video(None)], 84, True, checkpoint_key='recommendations')
    assert db.get_task(tid)['video_count'] == 1
    assert db.get_checkpoint(tid, 'recommendations')['cursor'] == 42
    assert db.get_checkpoint(tid, 'recommendations')['done'] is False
