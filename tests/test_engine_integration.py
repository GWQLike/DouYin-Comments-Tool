"""Real MediaCrawler lifecycle/client/paginator with only browser/HTTP simulated."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.crawler import Collector, normalize_comment
from backend.db import Database
from backend.douyin_adapter import DouyinAdapter, load_upstream_client, pooled_client_type
from backend.engine import WorkbenchRuntime, run_engine
from backend.likes import LikesCollector
from backend.recommendations import RecommendationsCollector
from backend.search import export_txt, export_likes_txt, select_comments, Filters
from backend.upstream import module
from test_stable_policy import Clock, policy
from test_crawler import comment, page
from test_likes_collector import profile, video, likes, users


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
    return db.create_task('https://www.douyin.com/video/123456789' if kind == 'comments' else '',
                         '123456789' if kind == 'comments' else None, kind=kind, target_account='target.user', **kwargs)


def exported(db, tid, kind):
    complete = db.get_task(tid)['collection_complete']
    return export_txt(select_comments(db.read_comments(tid), Filters()), complete) if kind == 'comments' else export_likes_txt(db.read_liked_videos(tid), complete)


@pytest.fixture(autouse=True)
def fixture_signing(monkeypatch):
    monkeypatch.setattr(module('media_platform.douyin.help'), 'get_a_bogus_from_js', lambda *args: 'fixture-signature')


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('resumed', [False, True])
async def test_real_engine_matches_legacy_bytes_without_calling_legacy_loops(tmp_path, monkeypatch, kind, resumed):
    old, new = Database(tmp_path / 'old.db'), Database(tmp_path / 'new.db')
    old_task, new_task = create(old, kind), create(new, kind)
    if resumed:
        for db, task in [(old, old_task), (new, new_task)]:
            if kind == 'comments':
                db.save_page(task['id'], '', [normalize_comment(Fixture(kind).main[0]['comments'][0], '123456789')], 20, False)
            else:
                from backend.likes import normalize_liked_video
                item = normalize_liked_video(Fixture(kind).pages[0]['aweme_list'][0])
                item['action_at'] = 1720001000 if kind == 'recommendations' else None
                db.save_likes_page(task['id'], [item], 100, False, checkpoint_key=kind)
                db.update_task(task['id'], target_sec_uid='target_sec')
    cls = {'comments': Collector, 'likes': LikesCollector, 'recommendations': RecommendationsCollector}[kind]
    async with SimulatedTransport(tmp_path / 'old/profile', lambda: False, lambda *args: None, fixture=Fixture(kind)) as transport:
        await cls(old, transport, old_task['id']).run()
    expected = exported(old, old_task['id'], kind)
    assert old.get_task(old_task['id'])['status'] == 'completed'

    async def forbidden(*args, **kwargs):
        raise AssertionError('legacy collector is disabled')
    for legacy in (Collector, LikesCollector, RecommendationsCollector):
        for name in ('run', 'collect_thread', 'collect_likes'):
            if hasattr(legacy, name):
                monkeypatch.setattr(legacy, name, forbidden)

    seen = []
    engine_cls = module('media_platform.douyin.core').DouYinCrawler
    real_start = engine_cls.start
    async def start(engine):
        seen.append('engine')
        await real_start(engine)
    monkeypatch.setattr(engine_cls, 'start', start)
    client_cls = load_upstream_client()
    method = 'get_aweme_all_comments' if kind == 'comments' else 'get_activity_all_videos'
    real_pages = getattr(client_cls, method)
    async def pages(client, *args, **kwargs):
        seen.append('client_pages')
        return await real_pages(client, *args, **kwargs)
    monkeypatch.setattr(client_cls, method, pages)
    runtime = WorkbenchRuntime(new, args_for(tmp_path / 'new', new_task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=Fixture(kind)))
    await run_engine(new, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    assert seen[0] == 'engine' and 'client_pages' in seen
    assert runtime.transport.closed and runtime.transport.http_client.is_closed
    runtime.transport.context.close.assert_not_awaited()
    actual = new.get_task(new_task['id'])
    assert actual['status'] == 'completed' and actual['verification_complete']
    assert exported(new, new_task['id'], kind) == expected
    assert runtime.transport.metrics['http_peak_active'] == 1


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
async def test_limits_and_missing_action_time_preserved(tmp_path, kind):
    db = Database(tmp_path / 'new.db')
    task = create(db, kind, max_videos=1, list_order='activity')
    fixture = Fixture(kind)
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    assert db.get_task(task['id'])['video_count'] == 1
    assert not db.get_task(task['id'])['collection_complete']
    assert all(request.url.params['count'] == '1' for request in fixture.requests if 'max_cursor' in request.url.params)


@pytest.mark.parametrize('response', [httpx.Response(403, text='denied'), httpx.Response(200, text='blocked')])
async def test_unknown_denial_is_not_human_verification(tmp_path, response):
    fixture = Fixture('comments')
    fixture.main = {0: response}
    db = Database(tmp_path / 'new.db')
    task = create(db, 'comments')
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    assert db.get_task(task['id'])['status'] == 'partial'
    assert not runtime.transport.policy.paused and not runtime.transport.policy.state.get('events')
    assert runtime.transport.metrics['http_request_categories']['comments'] == 3
    assert not db.get_checkpoint(task['id'])['done']


async def test_first_challenge_stops_bounded_real_validation_before_retry(tmp_path):
    db = Database(tmp_path / 'new.db')
    task = create(db, 'comments')
    fixture = Fixture('comments')
    fixture.main = {0: {'status_code': 0, 'verify_check': True}}
    args = args_for(tmp_path, task)
    args.stop_on_challenge = True
    args.max_requests = 100
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    assert db.get_task(task['id'])['status'] == 'paused'
    assert '首次人机验证' in db.get_task(task['id'])['error']
    assert runtime.transport.metrics['http_request_categories']['comments'] == 1
    assert runtime.transport.policy.state['manual_required']


async def test_budget_and_cancellation_leave_atomic_checkpoint(tmp_path):
    db = Database(tmp_path / 'new.db')
    task = create(db, 'comments')
    args = args_for(tmp_path, task)
    args.max_requests = 2  # detail + first comment page
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=Fixture('comments')))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    assert runtime.transport.metrics['http_requests'] == 2
    assert db.get_checkpoint(task['id'])['cursor'] == 20
    assert db.get_task(task['id'])['main_count'] == 1
    assert db.get_task(task['id'])['status'] == 'paused'


async def test_malformed_successful_http_page_backs_off_without_false_done(tmp_path):
    db = Database(tmp_path / 'new.db')
    task = create(db, 'comments')
    fixture = Fixture('comments')
    fixture.main = {0: {'status_code': 0, 'comments': [], 'cursor': 20}}
    runtime = WorkbenchRuntime(db, args_for(tmp_path, task), lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, runtime.args, runtime.stop, runtime.waiting, runtime=runtime)
    assert runtime.transport.policy._level == 3
    assert runtime.transport.policy.metrics['temporary_failures'] == 3
    assert not db.get_checkpoint(task['id'])['done']
    assert db.get_task(task['id'])['status'] == 'partial'
