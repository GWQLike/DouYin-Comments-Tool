from datetime import datetime
import threading

from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from backend.db import Database
from backend.search import Filters, SHANGHAI, date_bound, export_txt, match_text, select_comments
from backend.urls import parse_input


class FakeManager:
    def __init__(self, db, data_dir):
        self.db, self.task_id = db, None
        self.lock = threading.RLock()
    def check(self):
        return self.task_id is not None
    def start(self, task_id=None, verify=False):
        self.task_id = task_id
        if task_id:
            self.db.update_task(task_id, status='verifying' if verify else 'running', stop_requested=False)
        else:
            self.db.session_set('waiting')
    def stop(self):
        if self.task_id:
            self.db.update_task(self.task_id, status='paused', stop_requested=True)
        self.task_id = None


def row(cid='9223372036854775881', **overrides):
    return dict(comment_id=cid, video_id='7525082444551310602', parent_id='',
                text='謝謝分享，裡面的ＡＢＣ很清楚😊', created_at=1791267788, ip_label='广东',
                nickname='作者原名', douyin_id='test.account', uid='9923372036854775889',
                sec_uid='MS4wLjAB_test', author_key='9923372036854775889',
                identity_state='verified', reply_count=0) | overrides


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, manager_factory=FakeManager)
    with TestClient(app) as client:
        yield client


def seeded(client, count=3):
    task = client.post('/api/tasks', json={'url':'https://www.douyin.com/video/7525082444551310602'}).json()
    db = client.app.state.db
    db.save_page(task['id'], '', [row(str(9223372036854775881+i), created_at=1791267788+i) for i in range(count)], 0, True)
    db.update_task(task['id'], collection_complete=True,status='completed')
    return task['id']


def test_traditional_width_case_and_literal_search():
    assert match_text('謝謝分享，裡面ＡＢＣ和裏面', Filters(keyword='谢谢分享'))[0]
    assert match_text('謝謝分享，裡面ＡＢＣ和裏面', Filters(keyword='abc'))[0]
    assert match_text('裡面', Filters(keyword='里面'))[0]
    assert match_text('裏面', Filters(keyword='里面'))[0]
    assert not match_text('滑鼠', Filters(keyword='鼠标'))[0]
    assert not match_text('喜欢', Filters(keyword='喜饭', mode='fuzzy',threshold=70))[0]
    assert not match_text('北京大学', Filters(keyword='这是北京大学的视频', mode='fuzzy'))[0]
    assert not match_text('任何文字', Filters(keyword='%'))[0]
    assert not match_text('喜 欢', Filters(keyword='喜欢'))[0]


def test_fuzzy_is_opt_in():
    text = '这个视频真的非常精彩'
    keyword = '这个视频真的非常精采'
    assert not match_text(text, Filters(keyword=keyword))[0]
    assert match_text(text, Filters(keyword=keyword,mode='fuzzy'))[1] == 'fuzzy'


def test_date_boundaries():
    assert date_bound('2026-10-06') == datetime(2026,10,6,tzinfo=SHANGHAI).timestamp()
    assert date_bound('2026-10-06',True) < date_bound('2026-10-07')
    assert date_bound('2026-10-06',True) > date_bound('2026-10-06T23:59:59')
    with pytest.raises(ValueError):
        Filters(start='2026-10-07',end='2026-10-06')


def test_query_sort_and_export_all_pages(client):
    tid = seeded(client, 57)
    first = client.get(f'/api/tasks/{tid}/comments?page_size=2&keyword=谢谢').json()
    assert first['total'] == 57
    assert len(first['items']) == 2
    assert first['items'][0]['comment_id'] == str(9223372036854775881+56)
    assert first['items'][0]['text'].startswith('謝謝')
    result = client.get(f'/api/tasks/{tid}/export.txt?keyword=谢谢&page_size=2')
    assert result.content.startswith(b'\xef\xbb\xbf')
    text = result.content.decode('utf-8-sig')
    assert text.count('用户名：') == 57
    assert '视频链接：' not in text
    assert 'https://' not in text
    assert [line.split('：')[0] for line in text.split('\r\n')[:5]] == ['用户名','抖音号','评论内容','时间','IP 属地']
    assert text.split('\r\n')[5] == ''


def test_unknown_identity_is_not_false_account_match(client):
    tid = seeded(client)
    db = client.app.state.db
    db.save_page(tid,'',[row('unknown',uid='123',author_key='123',douyin_id='',identity_state='unknown',nickname='same')],0,True)
    data = client.get(f'/api/tasks/{tid}/comments?account=123').json()
    assert data['total'] == 0 and data['unknown_authors'] == 1
    assert not data['verification_complete']
    db.save_author(tid,'123',douyin_id='actual',identity_state='verified')
    assert client.get(f'/api/tasks/{tid}/comments?account=actual').json()['total'] == 1


def test_combined_filters_missing_timestamp_and_profile(client):
    tid = seeded(client)
    db = client.app.state.db
    db.save_page(tid,'',[row('no-time',created_at=None,ip_label='')],0,True)
    data = client.get(f'/api/tasks/{tid}/comments?account=test.account&keyword=不存在').json()
    assert data['total'] == 0
    data = client.get(f'/api/tasks/{tid}/comments?account=test.account&keyword=谢谢&start=2020-01-01').json()
    assert data['total'] == 3 and data['missing_times'] == 1
    all_data = client.get(f'/api/tasks/{tid}/comments').json()
    assert all_data['items'][-1]['time_text'] == '未提供'


def test_partial_export_escape_newlines():
    content = export_txt([{'nickname':'名\n字','douyin_id':'x','text':'一\r\n二','time_text':'未提供','ip_label':''}],False).decode('utf-8-sig')
    assert content.startswith('采集尚未完成')
    assert '名\\n字' in content and '一\\n二' in content
    assert 'IP 属地：未提供' in content


def test_resume_atomic_dedup_and_deletion(client):
    tid = seeded(client)
    db = client.app.state.db
    db.save_page(tid,'',[row()],20,False)
    assert db.get_task(tid)['main_count'] == 3
    assert db.get_checkpoint(tid) == {'cursor':20,'done':False,'restarts':0}
    new = client.post(f'/api/tasks/{tid}/refresh').json()
    assert new['id'] != tid and new['main_count'] == 0
    assert client.delete(f'/api/tasks/{tid}').status_code == 200
    assert client.get(f'/api/tasks/{tid}/comments').status_code == 404
    assert not db.read_comments(tid)
    assert not db.authors(tid,False)


def test_invalid_inputs_and_cross_origin(client):
    assert client.post('/api/tasks',json={'url':'https://evil.example/video/123'}).status_code == 400
    assert client.post('/api/session/login',headers={'Origin':'https://evil.example'}).status_code == 403
    tid = seeded(client)
    assert client.get(f'/api/tasks/{tid}/comments?start=bad').status_code == 400
    assert client.get(f'/api/tasks/{tid}/comments?page=0').status_code == 400
    assert client.get(f'/api/tasks/{tid}/comments?threshold=2').status_code == 400


def test_share_links():
    assert parse_input('分享视频 https://v.douyin.com/abcd/ 复制链接')[0] == 'https://v.douyin.com/abcd/'
    assert parse_input('https://www.douyin.com/user/name?modal_id=123')[1] == '123'
    assert parse_input('https://www.iesdouyin.com/share/video/123/')[1] == '123'
    for bad in ('http://127.0.0.1/video/123','https://douyin.com.evil.example/video/123','https://user@douyin.com/video/123'):
        with pytest.raises(ValueError):
            parse_input(bad)
