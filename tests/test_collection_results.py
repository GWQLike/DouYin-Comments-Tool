"""Expected results from raw pages through collection, result APIs and TXT."""
from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.engine import WorkbenchRuntime, run_engine
from support.engine import Fixture, SimulatedTransport, args_for, create, fixture_signing
from support.payloads import comment, likes, page, video


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'workbench')) as client:
        yield client


async def collect(client, tid, source, *, max_requests=0, verify_only=False):
    db = client.app.state.db
    args = args_for(client.app.state.manager.data_dir, db.get_task(tid))
    args.max_requests = max_requests
    args.verify_only = verify_only
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *_: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=source))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)


def results(client, tid, path, **params):
    response = client.get(f'/api/tasks/{tid}/{path}', params=params)
    assert response.status_code == 200
    return response.json()


def assert_txt(client, tid, expected):
    response = client.get(f'/api/tasks/{tid}/export.txt')
    assert response.status_code == 200
    assert response.content == expected.encode('utf-8-sig')


def profile_requests(source):
    return [r.url.params['sec_user_id'] for r in source.requests
            if r.url.path.endswith('/profile/other/')]


async def test_comment_and_reply_resume_has_literal_results_and_export(client):
    db = client.app.state.db
    tid = create(db, 'comments', verify_authors=False)['id']
    source = Fixture('comments')
    main = comment('m1', text='首条评论', replies=2, user={
        'uid': 'author_uid', 'sec_uid': 'author_sec', 'nickname': '楼主',
    })
    main['reply_comment'] = [{'cid': 'r1', 'text': '回复预览'}]
    no_time = {'cid': 'm2', 'text': '没有时间', 'reply_comment_total': 0, 'user': {
        'uid': 'other_uid', 'sec_uid': 'author_sec', 'nickname': '另一作者',
    }}
    reply = comment('r1', text='謝謝\r\n分享\\原文😊', user={
        'uid': 'reply_uid', 'unique_id': 'reply.public', 'nickname': '回复作者',
    })
    reply['ip_label'] = '上海'
    source.main = {0: page([main], 20, 1), 20: page([no_time], 40)}
    source.reply = {0: page([reply], 20, 1), 20: page([
        {'cid': 'r1'}, comment('r2', text='第二条回复', stamp=1720000200),
    ], 40)}

    await collect(client, tid, source, max_requests=4)
    task = db.get_task(tid)
    assert (task['status'], task['main_count'], task['reply_count']) == ('paused', 2, 1)
    assert db.get_checkpoint(tid) == {'cursor': 40, 'done': True, 'restarts': 0}
    assert db.get_checkpoint(tid, 'm1') == {'cursor': 20, 'done': False, 'restarts': 0}

    source.requests.clear()
    await collect(client, tid, source)
    assert [(r.url.path, r.url.params['comment_id'], r.url.params['cursor'])
            for r in source.requests] == [('/aweme/v1/web/comment/list/reply/', 'm1', '20')]
    task = db.get_task(tid)
    assert (task['status'], task['main_count'], task['reply_count']) == ('completed', 2, 2)
    checkpoint = {'cursor': 40, 'done': True, 'restarts': 0}
    assert db.get_checkpoint(tid) == db.get_checkpoint(tid, 'm1') == checkpoint

    source.requests.clear()
    await collect(client, tid, source, verify_only=True)
    assert len(source.requests) == 1 and profile_requests(source) == ['author_sec']
    assert db.get_checkpoint(tid) == db.get_checkpoint(tid, 'm1') == checkpoint
    authors = db.verification_records(tid)
    assert [(row['record_id'], row['status'], row['failure_kind'], row['attempts']) for row in authors] == [
        ('m1', 'confirmed', '', 1), ('m2', 'conflict', 'identity_conflict', 1),
        ('r1', 'confirmed', '', 0), ('r2', 'confirmed', '', 0),
    ]
    data = results(client, tid, 'comments')
    assert (data['total'], data['missing_times'], data['unknown_authors']) == (4, 1, 1)
    assert data['collection_complete'] and not data['verification_complete']
    assert [(row['comment_id'], row['parent_id'], row['created_at'], row['douyin_id'])
            for row in data['items']] == [
        ('r2', 'm1', 1720000200, 'public.one'),
        ('r1', 'm1', 1720000000, 'reply.public'),
        ('m1', '', 1720000000, 'author.one'),
        ('m2', '', None, ''),
    ]
    assert_txt(client, tid,
        '用户名：测试用户\r\n抖音号：public.one\r\n评论内容：第二条回复\r\n'
        '时间：2024-07-03 17:50:00\r\nIP 属地：广东\r\n\r\n'
        '用户名：回复作者\r\n抖音号：reply.public\r\n评论内容：謝謝\\n分享\\\\原文😊\r\n'
        '时间：2024-07-03 17:46:40\r\nIP 属地：上海\r\n\r\n'
        '用户名：楼主\r\n抖音号：author.one\r\n评论内容：首条评论\r\n'
        '时间：2024-07-03 17:46:40\r\nIP 属地：广东\r\n\r\n'
        '用户名：另一作者\r\n抖音号：未提供\r\n评论内容：没有时间\r\n时间：未提供\r\nIP 属地：未提供\r\n')


def activity_video(vid, title, author, publication, recommendation):
    return {
        **video(vid, author=author, stamp=publication), 'desc': title,
        'friend_recommend_info': {'label_user_list': [
            {'user': {'sec_uid': 'target_sec'}, 'recommend_time': recommendation},
        ]},
    }


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
async def test_list_resume_keeps_unique_limit_and_source_order_in_export(client, kind):
    db = client.app.state.db
    bounds = {'start_time': 1720001000, 'end_time': 1720001100} if kind == 'recommendations' else {}
    tid = create(db, kind, max_videos=2, list_order='activity', verify_authors=False, **bounds)['id']
    source = Fixture(kind)
    first = activity_video('11', '先出现\r\n原题', {
        'uid': 'author_uid', 'sec_uid': 'author_sec', 'nickname': '甲',
    }, 1720000000, 1720001100)
    second = activity_video('22', '后出现', {
        'uid': 'wrong_uid', 'sec_uid': 'author_sec', 'nickname': '乙',
    }, 1720000200, 1720001000)
    overflow = activity_video('33', '超出数量', {}, 1720000000, 1720001000)
    outside = activity_video('44', '只在发布时间范围内', {}, 1720001050, 1720001200)
    tail = [first, second, overflow]
    if kind == 'recommendations':
        tail.insert(1, outside)
    source.pages = {0: likes([first], 100, 1), 100: likes(tail, 200, 0)}

    await collect(client, tid, source, max_requests=3)
    checkpoint = {'cursor': 100, 'done': False, 'restarts': 0}
    assert db.get_checkpoint(tid, kind) == checkpoint
    task = db.get_task(tid)
    assert (task['status'], task['video_count']) == ('paused', 1)
    assert [(r.url.params['max_cursor'], r.url.params['count']) for r in source.requests
            if 'max_cursor' in r.url.params] == [('0', '2')]

    source.requests.clear()
    await collect(client, tid, source)
    endpoint = ('/aweme/v1/web/aweme/favorite/' if kind == 'likes'
                else '/aweme/v1/web/familiar/recommend/feed/')
    assert [(r.url.path, r.url.params['max_cursor'], r.url.params['count'])
            for r in source.requests if 'max_cursor' in r.url.params] == [
        (endpoint, '100', '1'),
    ]
    assert profile_requests(source) == ['target_sec']
    task = db.get_task(tid)
    assert (task['status'], task['video_count'], task['collection_complete']) == ('partial', 2, False)
    assert '2 条采集上限' in task['error']
    assert db.get_checkpoint(tid, kind) == checkpoint
    assert [(row['video_id'], row['action_at']) for row in db.read_liked_videos(tid)] == (
        [('11', 1720001100), ('22', 1720001000)] if kind == 'recommendations'
        else [('11', None), ('22', None)]
    )

    source.requests.clear()
    await collect(client, tid, source, verify_only=True)
    assert len(source.requests) == 1 and profile_requests(source) == ['author_sec']
    assert db.get_checkpoint(tid, kind) == checkpoint
    verified = db.get_task(tid)
    assert verified['status'] == 'partial' and verified['error'] == task['error']
    assert [(row['record_id'], row['status'], row['failure_kind'], row['attempts'])
            for row in db.verification_records(tid)] == [
        ('11', 'confirmed', '', 1), ('22', 'conflict', 'identity_conflict', 1),
    ]
    first_page = results(client, tid, 'videos', page=1, page_size=1)
    second_page = results(client, tid, 'videos', page=2, page_size=1)
    assert first_page['total'] == second_page['total'] == 2
    assert first_page['unknown_authors'] == second_page['unknown_authors'] == 1
    assert [(row['video_id'], row['author_douyin_id'], row['created_at'])
            for row in first_page['items'] + second_page['items']] == [
        ('11', 'author.one', 1720000000), ('22', '', 1720000200),
    ]
    assert_txt(client, tid,
        '采集尚未完成，仅导出当前已获取的匹配结果。\r\n\r\n'
        '视频标题：先出现\\n原题\r\n作者名称：甲\r\n作者抖音号：author.one\r\n'
        '发布时间：2024-07-03 17:46:40\r\n视频链接：https://www.douyin.com/video/11\r\n\r\n'
        '视频标题：后出现\r\n作者名称：乙\r\n作者抖音号：未提供\r\n'
        '发布时间：2024-07-03 17:50:00\r\n视频链接：https://www.douyin.com/video/22\r\n')
