"""Collection bounds are task options, not publication-time or display filters."""
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import Database
from backend.likes import LikesCollector, normalize_liked_video
from backend.recommendations import RecommendationsCollector
from test_likes_collector import FakeLikesClient, likes, video
from test_recommendations_collector import FakeRecommendationsClient
from test_search_api import FakeManager


class CountedLikesClient(FakeLikesClient):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.counts = []

    async def get_user_likes(self, sec_uid, cursor=0, count=18):
        self.counts.append(count)
        return await super().get_user_likes(sec_uid, cursor)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        yield client


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
def test_options_round_trip_and_refresh_preserve_bounds(client, kind):
    bounds = {'start': '2026-10-05', 'end': '2026-10-06'} if kind == 'recommendations' else {}
    response = client.post(f'/api/{kind}/tasks', json={
        'account': 'target.user', 'max_videos': 10, **bounds,
    })
    assert response.status_code == 200
    task = response.json()
    cst = timezone(timedelta(hours=8))
    assert task['start_time'] == (datetime(2026, 10, 5, tzinfo=cst).timestamp() if bounds else None)
    assert task['end_time'] == (datetime(2026, 10, 6, 23, 59, 59, 999999, tzinfo=cst).timestamp() if bounds else None)
    assert task['max_videos'] == 10 and task['list_order'] == 'activity'
    refreshed = client.post(f"/api/tasks/{task['id']}/refresh").json()
    assert refreshed['id'] != task['id']
    for name in ('kind', 'target_account', 'max_videos', 'start_time', 'end_time', 'list_order'):
        assert refreshed[name] == task[name]


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
@pytest.mark.parametrize('options', [
    {'max_videos': -1}, {'max_videos': 1.5}, {'max_videos': True}, {'max_videos': '10'},
    {'max_videos': None}, {'max_videos': 100001}, {'start': 'bad'}, {'end': '2026-02-30'},
    {'start': '2026-10-07', 'end': '2026-10-06'},
])
def test_invalid_options_do_not_create_task(client, kind, options):
    response = client.post(f'/api/{kind}/tasks', json={'account': 'target.user', **options})
    assert response.status_code in (400, 422)
    assert client.get('/api/tasks').json() == []


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
def test_default_unlimited_and_iso_bound(client, kind):
    bounds = {'start': '2026-10-06T14:23:08+08:00'} if kind == 'recommendations' else {}
    task = client.post(f'/api/{kind}/tasks', json={
        'account': 'target.user', **bounds,
    }).json()
    assert task['max_videos'] == 0 and task['end_time'] is None
    assert task['start_time'] == (1791267788 if bounds else None) and task['list_order'] == 'activity'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
async def test_limit_counts_unique_videos_even_if_service_ignores_requested_count(tmp_path, kind):
    db = Database(tmp_path / 'bounded.sqlite3')
    task = db.create_task('', kind=kind, target_account='target.user', max_videos=10, list_order='activity')
    tid = task['id']
    # A repeated row must neither spend a slot nor reorder the first occurrence.
    rows = [video(str(i), stamp=1720000000 + i) for i in range(1, 16)]
    rows.insert(1, video('1'))
    factory, collector = ((CountedLikesClient, LikesCollector) if kind == 'likes'
                          else (FakeRecommendationsClient, RecommendationsCollector))
    fake = factory(pages={0: likes(rows, 18, False)})
    await collector(db, fake, tid, interval=(0, 0)).run()
    result = db.get_task(tid)
    assert result['video_count'] == 10 and not result['collection_complete']
    assert result['status'] == 'partial' and '10' in result['error']
    assert [row['video_id'] for row in db.read_liked_videos(tid)] == [str(i) for i in range(1, 11)]
    assert fake.counts == [10]
    assert db.get_checkpoint(tid, kind)['cursor'] == 0
    # Resuming a bounded result must not silently request the next page or overrun.
    resumed = factory(pages={})
    await collector(db, resumed, tid, interval=(0, 0)).run()
    assert not resumed.counts and db.get_task(tid)['video_count'] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
async def test_remaining_capacity_is_requested_and_exact_end_can_complete(tmp_path, kind):
    db = Database(tmp_path / 'resume.sqlite3')
    tid = db.create_task('', kind=kind, target_account='target.user', max_videos=10,
                         list_order='activity')['id']
    db.save_likes_page(tid, [normalize_liked_video(video(str(i))) for i in range(1, 9)],
                       18, False, checkpoint_key=kind)
    factory, collector = ((CountedLikesClient, LikesCollector) if kind == 'likes'
                          else (FakeRecommendationsClient, RecommendationsCollector))
    fake = factory(pages={18: likes([video('9'), video('10')], 36, False)})
    await collector(db, fake, tid, interval=(0, 0)).run()
    assert fake.counts == [2]
    assert db.get_task(tid)['video_count'] == 10 and db.get_task(tid)['collection_complete']


@pytest.mark.asyncio
async def test_likes_range_never_uses_video_publication_time(tmp_path):
    db = Database(tmp_path / 'time.sqlite3')
    tid = db.create_task('', kind='likes', target_account='target.user', start_time=1700000000,
                         end_time=1800000000, list_order='activity')['id']
    fake = CountedLikesClient(pages={0: likes([video('1', stamp=1791267788)])})
    await LikesCollector(db, fake, tid, interval=(0, 0)).run()
    result = db.get_task(tid)
    assert result['status'] == 'partial' and not result['collection_complete']
    assert result['video_count'] == 0 and '时间' in result['error']
    assert not db.get_checkpoint(tid, 'likes')['done']


def test_legacy_task_retains_publication_order(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    db = Database(path)
    tid = db.create_task('', kind='likes', target_account='target.user')['id']
    db.save_likes_page(tid, [normalize_liked_video(video('1', stamp=1720000000)),
                            normalize_liked_video(video('2', stamp=1791267788))], 0, True)
    reopened = Database(path)
    assert reopened.get_task(tid)['list_order'] == 'publication'
    assert [item['video_id'] for item in reopened.read_liked_videos(tid)] == ['2', '1']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
@pytest.mark.parametrize('failure', ['request', 'stall'])
async def test_activity_resume_never_merges_a_changed_first_page(tmp_path, kind, failure):
    db = Database(tmp_path / 'activity-resume.sqlite3')
    tid = db.create_task('', kind=kind, target_account='target.user', max_videos=3,
                         list_order='activity')['id']
    db.save_likes_page(tid, [normalize_liked_video(video('1')), normalize_liked_video(video('2'))],
                       18, False, checkpoint_key=kind)
    factory, collector = ((CountedLikesClient, LikesCollector) if kind == 'likes'
                          else (FakeRecommendationsClient, RecommendationsCollector))
    failure_page = {'status_code': 2} if failure == 'request' else likes([video('2')], 18, True)
    fake = factory(pages={
        18: failure_page,
        # Replaying this changed first page would put new video 3 after 1/2,
        # and consume the remaining quota with the wrong activity ordering.
        0: likes([video('3'), video('1'), video('2'), video('4')]),
    })
    await collector(db, fake, tid, interval=(0, 0)).run()
    result = db.get_task(tid)
    assert result['status'] == 'partial' and not result['collection_complete']
    assert '新建批次' in result['error'] and '顺序' in result['error']
    assert [row['video_id'] for row in db.read_liked_videos(tid)] == ['1', '2']
    assert [cursor for family, cursor in fake.calls if family == kind] == [18, 18, 18]
    assert db.get_checkpoint(tid, kind) == {'cursor': 18, 'done': False, 'restarts': 0}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
@pytest.mark.parametrize('failure', ['request', 'stall'])
async def test_legacy_publication_resume_keeps_its_single_replay(tmp_path, kind, failure):
    db = Database(tmp_path / 'legacy-resume.sqlite3')
    tid = db.create_task('', kind=kind, target_account='target.user', list_order='publication')['id']
    db.save_likes_page(tid, [normalize_liked_video(video('1'))], 18, False, checkpoint_key=kind)
    factory, collector = ((CountedLikesClient, LikesCollector) if kind == 'likes'
                          else (FakeRecommendationsClient, RecommendationsCollector))
    failure_page = {'status_code': 2} if failure == 'request' else likes([video('1')], 18, True)
    fake = factory(pages={18: failure_page, 0: likes([video('2'), video('1')])})
    await collector(db, fake, tid, interval=(0, 0)).run()
    assert db.get_task(tid)['collection_complete']
    assert [cursor for family, cursor in fake.calls if family == kind] == [18, 18, 18, 0]
    assert db.get_checkpoint(tid, kind)['restarts'] == 1
    assert [row['video_id'] for row in db.read_liked_videos(tid)] == ['2', '1']
