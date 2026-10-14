"""Bounded work must preserve coverage, identity checks and cancellation."""
import asyncio

import pytest

from backend.crawler import Collector
from backend.db import Database
from backend.likes import LikesCollector, normalize_liked_video
from test_crawler import FakeClient, comment, page
from test_likes_collector import FakeLikesClient, likes, video


@pytest.mark.asyncio
async def test_independent_replies_overlap_but_each_cursor_chain_stays_ordered(tmp_path):
    db = Database(tmp_path / 'parallel.sqlite3')
    tid = db.create_task('https://www.douyin.com/video/1', '1')['id']

    class Client(FakeClient):
        active = 0
        peak = 0

        async def get_sub_comments(self, video_id, parent_id, cursor):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().get_sub_comments(video_id, parent_id, cursor)
            finally:
                self.active -= 1

    parents = [f'm{i}' for i in range(8)]
    replies = {(p, 0): page([comment(p + 'a')], 20, 1) for p in parents}
    replies.update({(p, 20): page([comment(p + 'b')]) for p in parents})
    client = Client(main={0: page([comment(p, replies=2) for p in parents])}, replies=replies)
    await Collector(db, client, tid).run()
    assert client.peak == 3
    assert db.get_task(tid)['reply_count'] == 16
    assert db.get_task(tid)['collection_complete']
    for parent in parents:
        assert [c for c in client.calls if isinstance(c, tuple) and c[0] == parent] == [(parent, 0), (parent, 20)]


@pytest.mark.asyncio
async def test_profile_request_shared_but_conflicting_authors_are_checked_separately(tmp_path):
    db = Database(tmp_path / 'identity.sqlite3')
    tid = db.create_task('https://www.douyin.com/video/1', '1')['id']

    class Client(FakeClient):
        async def get_user_info(self, sec_uid):
            await asyncio.sleep(0.01)
            return await super().get_user_info(sec_uid)

    client = Client(main={0: page([
        comment('a', user={'uid': 'right', 'sec_uid': 'shared', 'nickname': 'A'}),
        comment('b', user={'uid': 'wrong', 'sec_uid': 'shared', 'nickname': 'B'}),
        comment('c', user={'sec_uid': 'shared', 'nickname': 'C'}),
    ])}, profiles={'shared': {'user': {'uid': 'right', 'sec_uid': 'shared',
                                     'unique_id': 'public.account', 'nickname': 'A'}}})
    await Collector(db, client, tid).run()
    rows = {r['comment_id']: r for r in db.read_comments(tid)}
    assert client.calls.count('shared') == 1
    assert rows['a']['public_number'] == rows['c']['public_number'] == 'public.account'
    assert rows['b']['public_number'] == '' and rows['b']['resolved_state'] != 'verified'


@pytest.mark.asyncio
async def test_stop_cancels_all_workers_and_does_not_start_queued_replies(tmp_path):
    db = Database(tmp_path / 'stop.sqlite3')
    tid = db.create_task('https://www.douyin.com/video/1', '1')['id']
    ready = asyncio.Event()

    class Client(FakeClient):
        started = 0
        cancelled = 0

        async def get_sub_comments(self, video_id, parent_id, cursor):
            self.started += 1
            if self.started == 3:
                ready.set()
            try:
                await asyncio.Future()
            finally:
                self.cancelled += 1

    client = Client(main={0: page([comment(f'm{i}', replies=1) for i in range(20)])})
    running = asyncio.create_task(Collector(db, client, tid).run())
    await asyncio.wait_for(ready.wait(), 2)
    db.update_task(tid, stop_requested=True)
    await asyncio.wait_for(running, 1)
    assert client.started == client.cancelled == 3
    assert db.get_task(tid)['status'] == 'paused'
    assert not db.get_task(tid)['collection_complete']


@pytest.mark.asyncio
async def test_liked_author_cache_does_not_share_identity_conclusions(tmp_path):
    db = Database(tmp_path / 'liked-authors.sqlite3')
    tid = db.create_task('', kind='likes', target_account='target.user')['id']
    client = FakeLikesClient(pages={0: likes([
        video('1', {'uid': 'right', 'sec_uid': 'shared'}),
        video('2', {'uid': 'wrong', 'sec_uid': 'shared'}),
    ])}, profiles={'shared': {'user': {'uid': 'right', 'sec_uid': 'shared',
                                     'unique_id': 'public.account', 'nickname': 'A'}}})
    await LikesCollector(db, client, tid).run()
    rows = {r['video_id']: r for r in db.read_liked_videos(tid)}
    assert client.calls.count(('profile', 'shared')) == 1
    assert rows['1']['author_douyin_id'] == 'public.account'
    assert rows['2']['author_douyin_id'] == ''


@pytest.mark.asyncio
async def test_conflicting_author_must_be_reverified_even_with_a_verified_alias(tmp_path):
    db = Database(tmp_path / 'conflict.sqlite3')
    tid = db.create_task('https://www.douyin.com/video/1', '1')['id']
    client = FakeClient(main={0: page([
        comment('a', user={'uid': 'right', 'sec_uid': 'shared', 'unique_id': 'old.one', 'nickname': 'A'}),
        comment('b', user={'uid': 'right', 'sec_uid': 'shared', 'unique_id': 'old.two', 'nickname': 'A'}),
        comment('alias', user={'sec_uid': 'shared', 'unique_id': 'old.one', 'nickname': 'A'}),
    ])}, profiles={'shared': {'user': {'uid': 'right', 'sec_uid': 'shared',
                                     'unique_id': 'current.account', 'nickname': 'A'}}})
    await Collector(db, client, tid).run()
    rows = {r['comment_id']: r for r in db.read_comments(tid)}
    assert client.calls.count('shared') == 1
    assert rows['a']['public_number'] == rows['b']['public_number'] == 'current.account'


def test_quota_reads_only_ids_once_and_never_loads_all_video_details(tmp_path, monkeypatch):
    db = Database(tmp_path / 'quota.sqlite3')
    tid = db.create_task('', kind='likes', target_account='target.user', max_videos=10)['id']
    calls = []
    original = db.liked_video_ids

    def ids(task_id):
        calls.append(task_id)
        return original(task_id)

    monkeypatch.setattr(db, 'liked_video_ids', ids)
    monkeypatch.setattr(db, 'read_liked_videos', lambda *args: pytest.fail('Full video load during quota check'))
    collector = LikesCollector(db, None, tid)
    for i in range(1, 10):
        assert collector.request_count() == 11 - i
        collector.save_list_page([normalize_liked_video(video(str(i)))], i, False)
    assert calls == [tid]
    assert db.get_task(tid)['video_count'] == 9
