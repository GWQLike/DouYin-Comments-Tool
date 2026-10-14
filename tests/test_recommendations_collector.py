"""Offline contracts for the account's recommendations, never its likes/feed."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.db import Database
from backend.douyin_adapter import DouyinAdapter
from backend.recommendations import RecommendationsCollector, recommendation_time
from test_likes_collector import FakeLikesClient, likes, profile, video


class FakeRecommendationsClient(FakeLikesClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.counts = []

    async def get_user_likes(self, *args, **kwargs):
        raise AssertionError('Recommendations must not request likes')

    async def get_user_recommendations(self, sec_uid, cursor=0, count=18):
        assert sec_uid == 'target_sec'
        self.counts.append(count)
        return self.response(self.pages, cursor, 'recommendations')


def recommended(vid='1', action_at=1791267788, user=None, stamp=1720000000):
    return {**video(vid, stamp=stamp), 'friend_recommend_info': {'label_user_list': [
        {'user': user if user is not None else {'uid': 'target_uid', 'sec_uid': 'target_sec'},
         'recommend_time': action_at},
    ]}}


def task(tmp_path, **options):
    db = Database(tmp_path / 'recommendations.sqlite3')
    result = db.create_task('', kind='recommendations', target_account='target.user',
                            list_order='activity', **options)
    return db, result['id']


async def run(db, tid, fake):
    await RecommendationsCollector(db, fake, tid, interval=(0, 0)).run()
    return db.get_task(tid)


@pytest.mark.asyncio
async def test_recommendations_ignore_likes_privacy_and_use_boolean_pagination(tmp_path):
    db, tid = task(tmp_path)
    fake = FakeRecommendationsClient(target=profile(show_favorite_list=False), pages={
        0: likes([recommended('1', stamp=1720000000)], 18, True),
        18: likes([recommended('1'), recommended('2', stamp=1791267788)], 0, False),
    })
    result = await run(db, tid, fake)
    assert result['status'] == 'completed' and result['visibility'] == 'public'
    assert result['collection_complete'] and result['video_count'] == 2
    assert [r['video_id'] for r in db.read_liked_videos(tid)] == ['1', '2']
    assert [cursor for family, cursor in fake.calls if family == 'recommendations'] == [0, 18]
    assert db.get_checkpoint(tid, 'recommendations')['done']
    assert not db.get_checkpoint(tid, 'likes')['done']


@pytest.mark.asyncio
async def test_explicit_recommendations_privacy_uses_its_own_message(tmp_path):
    db, tid = task(tmp_path)
    fake = FakeRecommendationsClient(pages={0: {'status_code': 403, 'message': '该账号未公开推荐列表'}})
    result = await run(db, tid, fake)
    assert result['status'] == result['visibility'] == 'private'
    assert result['error'] == '该账号未公开推荐列表'
    assert result['video_count'] == 0 and not result['collection_complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [likes([]), None, {'status_code': 403},
    {'status_code': 0, 'aweme_list': None, 'has_more': False},
    {'status_code': 0, 'aweme_list': [], 'has_more': 'false'},
    {'status_code': 0, 'message': '该账号未公开喜欢列表'},
])
async def test_empty_unknown_recommendations_cannot_prove_privacy_or_completion(tmp_path, response):
    db, tid = task(tmp_path)
    result = await run(db, tid, FakeRecommendationsClient(pages={0: response}))
    assert result['status'] == 'partial' and result['visibility'] == 'unknown'
    assert not result['collection_complete'] and not db.get_checkpoint(tid, 'recommendations')['done']


@pytest.mark.asyncio
async def test_recommendation_range_is_inclusive_and_does_not_use_publication_time(tmp_path):
    db, tid = task(tmp_path, start_time=1791267700, end_time=1791267800)
    fake = FakeRecommendationsClient(pages={0: likes([
        recommended('1', 1791267801, stamp=1791267750),
        recommended('2', 1791267800, stamp=1700000000),
        recommended('3', 1791267750, stamp=1700000000),
        recommended('4', 1791267700, stamp=1700000000),
        recommended('5', 1791267699, stamp=1791267750),
    ])})
    result = await run(db, tid, fake)
    assert result['collection_complete']
    rows = db.read_liked_videos(tid)
    assert [row['video_id'] for row in rows] == ['2', '3', '4']
    assert [row['action_at'] for row in rows] == [1791267800, 1791267750, 1791267700]
    assert all(row['created_at'] == 1700000000 for row in rows)


@pytest.mark.asyncio
async def test_filtered_empty_page_advances_to_in_range_page(tmp_path):
    db, tid = task(tmp_path, start_time=1791267700, end_time=1791267800)
    fake = FakeRecommendationsClient(pages={
        0: likes([recommended('1', 1791267900)], 18, True),
        18: likes([recommended('2', 1791267750)], 36, False),
    })
    result = await run(db, tid, fake)
    assert result['collection_complete'] and result['video_count'] == 1
    assert db.read_liked_videos(tid)[0]['video_id'] == '2'
    assert db.get_checkpoint(tid, 'recommendations')['cursor'] == 36
    assert [cursor for family, cursor in fake.calls if family == 'recommendations'] == [0, 18]


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [video('2'), recommended('2', user={'uid': 'another_uid'})])
async def test_unknown_target_recommendation_time_keeps_page_atomic(tmp_path, missing):
    db, tid = task(tmp_path, start_time=1791267700, end_time=1791267800)
    fake = FakeRecommendationsClient(pages={0: likes([recommended('1'), missing], 18, False)})
    result = await run(db, tid, fake)
    assert result['status'] == 'partial' and not result['collection_complete']
    assert result['video_count'] == 0 and '时间' in result['error']
    assert db.get_checkpoint(tid, 'recommendations')['cursor'] == 0


@pytest.mark.parametrize('user', [{'uid': 'target_uid'}, {'sec_uid': 'target_sec'},
                                {'uid': 'target_uid', 'sec_uid': 'target_sec'}])
def test_time_belongs_to_matching_target_identity(user):
    assert recommendation_time(recommended(user=user), {'uid': 'target_uid', 'sec_uid': 'target_sec'}) == 1791267788


@pytest.mark.parametrize('stamp', [None, False, True, 0, -1, 'bad', 1791267788.5, float('inf')])
def test_invalid_recommendation_time_is_unknown(stamp):
    assert recommendation_time(recommended(action_at=stamp), {'uid': 'target_uid'}) is None


def test_ambiguous_or_conflicting_identity_time_is_unknown():
    target = {'uid': 'target_uid', 'sec_uid': 'target_sec'}
    assert recommendation_time(recommended(user={'uid': 'other', 'sec_uid': 'target_sec'}), target) is None
    raw = recommended()
    raw['friend_recommend_info']['label_user_list'].append(
        {'user': target, 'recommend_time': 1791267799})
    assert recommendation_time(raw, target) is None
    assert recommendation_time({**video(), 'recommend_time': 1791267788,
                                'aweme_date': {'date_map': {'1': 1791267788}}}, target) is None


@pytest.mark.asyncio
async def test_adapter_recommendations_have_separate_endpoint_and_count(tmp_path):
    adapter = DouyinAdapter(tmp_path, lambda: False, lambda *args: None)
    adapter.client = SimpleNamespace(headers={})
    calls = []

    async def invoke(*args):
        calls.append(args)
        return {}

    adapter.invoke = invoke
    await adapter.get_user_recommendations('target_sec', 99, count=10)
    assert calls[0][:2] == ('get', '/aweme/v1/web/familiar/recommend/feed/')
    assert calls[0][2]['sec_user_id'] == 'target_sec'
    assert calls[0][2]['max_cursor'] == 99 and calls[0][2]['count'] == 10
    assert calls[0][2]['min_cursor'] == 0 and calls[0][2]['from'] == 1
    assert adapter.client.headers['Referer'] == 'https://www.douyin.com/user/target_sec'
