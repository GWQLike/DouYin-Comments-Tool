"""Simulated transport for the real MediaCrawler engine and client."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.douyin_adapter import DouyinAdapter, load_upstream_client, pooled_client_type
from backend.search import export_txt, export_likes_txt, select_comments, Filters
from backend.upstream import module

from .pacing import Clock, policy
from .payloads import comment, likes, page, profile, users, video


class Fixture:
    def __init__(self, kind):
        self.kind, self.requests = kind, []
        missing = {'uid': 'author_uid', 'sec_uid': 'author_sec', 'nickname': '原始作者'}
        self.main = {0: page([comment('m1', replies=2, user=missing)], 20, 1),
                     20: page([comment('m2', text='謝謝分享', stamp=1720000200)], 40)}
        self.reply = {0: page([comment('r1', text='楼中楼')], 20, 1), 20: page([comment('r2')], 40)}
        first, second = video('1', author=missing), video('2')
        for i, row in enumerate((first, second)):
            row['friend_recommend_info'] = {'label_user_list': [{'user': {'sec_uid': 'target_sec'}, 'recommend_time': 1720001000 - i * 100}]}
        self.pages = {0: likes([first], 100, 1), 100: likes([first, second], 200)}

    def __call__(self, request):
        self.requests.append(request)
        path, params = request.url.path, request.url.params
        if path.endswith('/aweme/detail/'):
            payload = {'status_code': 0, 'aweme_detail': {'desc': '离线测试视频'}}
        elif path.endswith('/comment/list/'):
            payload = self.main[int(params['cursor'])]
        elif path.endswith('/comment/list/reply/'):
            payload = self.reply[int(params['cursor'])]
        elif path.endswith('/profile/other/'):
            payload = profile(show_favorite_list=True) if params['sec_user_id'] == 'target_sec' else profile('author.one', 'author_sec', 'author_uid')
        elif path.endswith('/discover/search/'):
            payload = users([profile()['user']])
        elif path.endswith('/favorite/') or path.endswith('/recommend/feed/'):
            payload = self.pages[int(params['max_cursor'])]
        else:
            raise AssertionError(path)
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)


class SimulatedTransport(DouyinAdapter):
    def __init__(self, *args, fixture):
        super().__init__(*args)
        self.fixture, self.clock = fixture, Clock()
        self.policy = policy(self.profile.parent / 'control.json', self.clock, check_stop=self.check_stop)
        self.closed = False

    async def __aenter__(self):
        self.context = SimpleNamespace(close=AsyncMock())
        self.page = SimpleNamespace(url='https://www.douyin.com/', evaluate=AsyncMock(return_value={'xmst': 'fixture'}),
                                    goto=AsyncMock(), is_closed=lambda: False)
        self.http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.fixture))
        self.sign_executor = ThreadPoolExecutor(max_workers=1)
        self.client = pooled_client_type(load_upstream_client(), self)(headers={'User-Agent': 'fixture', 'Cookie': '', 'Origin': 'https://www.douyin.com/'},
                                                                       playwright_page=self.page, cookie_dict={})
        self.client.pong = AsyncMock(return_value=True)
        self.client.update_cookies = AsyncMock()
        return self

    async def __aexit__(self, *args):
        await super().__aexit__(*args)
        self.closed = True


def args_for(tmp_path, task):
    return SimpleNamespace(profile=str(tmp_path / 'profile'), task=task['id'], verify_only=False)


def create(db, kind, **kwargs):
    kwargs.setdefault('verify_authors', True)
    return db.create_task('https://www.douyin.com/video/123456789' if kind == 'comments' else '',
                         '123456789' if kind == 'comments' else None, kind=kind, target_account='target.user', **kwargs)


def exported(db, tid, kind):
    complete = db.get_task(tid)['collection_complete']
    return export_txt(select_comments(db.read_comments(tid), Filters()), complete) if kind == 'comments' else export_likes_txt(db.read_liked_videos(tid), complete)


@pytest.fixture(autouse=True)
def fixture_signing(monkeypatch):
    monkeypatch.setattr(module('media_platform.douyin.help'), 'get_a_bogus_from_js', lambda *args: 'fixture-signature')
