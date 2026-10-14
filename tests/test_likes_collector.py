"""Offline fixtures for likes, including failures that must not imply privacy."""
from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from backend.crawler import ManualActionRequired, PageFailure, validate_response
from backend.db import Database
from backend.douyin_adapter import DouyinAdapter
from backend.likes import LikesCollector, normalize_liked_video, profile_visibility


def profile(account="target.user", sec="target_sec", uid="target_uid", **kwargs):
    return {"status_code": 0, "user": {"uid": uid, "sec_uid": sec, "unique_id": account,
                                        "nickname": "目标昵称", **kwargs}}


def video(vid="1", author=None, stamp=1720000000):
    return {"aweme_id": vid, "desc": "测试视频标题\n原始文本", "create_time": stamp,
            "author": author if author is not None else {"uid": "author_uid", "sec_uid": "author_sec",
                                                          "unique_id": "author.one", "nickname": "视频作者"}}


def likes(rows, cursor=0, more=0):
    return {"status_code": 0, "aweme_list": rows, "max_cursor": cursor, "has_more": more}


def users(rows, cursor=0, more=0):
    return {"status_code": 0, "user_list": [{"user_info": user} for user in rows],
            "cursor": cursor, "has_more": more}


class FakeLikesClient:
    def __init__(self, pages=None, search=None, target=None, profiles=None):
        target = target if target is not None else profile(show_favorite_list=True)
        self.pages = pages if pages is not None else {0: likes([])}
        self.search = search if search is not None else {0: users([target["user"]])}
        self.profiles = {"target_sec": target, **(profiles or {})}
        self.calls = []
        self.attempts = defaultdict(int)
        self.manual_waits = 0

    def response(self, mapping, key, family):
        self.calls.append((family, key))
        result = mapping[key]
        if isinstance(result, list):
            index = self.attempts[(family, key)]
            self.attempts[(family, key)] += 1
            result = result[min(index, len(result) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    async def search_users(self, account, offset=0, search_id=""):
        assert account == "target.user"
        return self.response(self.search, offset, "search")

    async def get_user_info(self, sec_uid):
        return self.response(self.profiles, sec_uid, "profile")

    async def get_user_likes(self, sec_uid, cursor=0):
        assert sec_uid == "target_sec"
        return self.response(self.pages, cursor, "likes")

    async def wait_for_manual(self):
        self.manual_waits += 1


@pytest.fixture
def task_db(tmp_path):
    db = Database(tmp_path / "likes.sqlite3")
    task = db.create_task("", kind="likes", target_account="target.user")
    return db, task["id"]


def collector(db, tid, client, **kwargs):
    return LikesCollector(db, client, tid, interval=(0, 0), **kwargs)


@pytest.mark.asyncio
async def test_exact_public_account_all_pages_deduplicate_and_sort(task_db):
    db, tid = task_db
    fake = FakeLikesClient(pages={0: likes([video("1")], 100, 1),
                                 100: likes([video("1"), video("2", stamp=1720000200)], 200)})
    await collector(db, tid, fake).run()
    task = db.get_task(tid)
    assert task["status"] == "completed" and task["collection_complete"]
    assert task["visibility"] == "public" and task["target_sec_uid"] == "target_sec"
    assert task["video_count"] == 2
    assert [v["video_id"] for v in db.read_liked_videos(tid)] == ["2", "1"]
    assert fake.calls[:2] == [("search", 0), ("profile", "target_sec")]


@pytest.mark.asyncio
async def test_explicit_private_profile_stops_without_fetching_likes(task_db):
    db, tid = task_db
    fake = FakeLikesClient(target=profile(show_favorite_list=False))
    await collector(db, tid, fake).run()
    task = db.get_task(tid)
    assert task["status"] == "private" and task["visibility"] == "private"
    assert task["error"] == "该账号未公开喜欢列表"
    assert not task["collection_complete"] and not any(call[0] == "likes" for call in fake.calls)


@pytest.mark.parametrize("flag", [None, 0, 1, "false", "true", "0", {}])
def test_only_boolean_privacy_flags_are_interpreted(flag):
    assert profile_visibility({"show_favorite_list": flag}) == "unknown"
    assert profile_visibility({"favorite_permission": 1}) == "unknown"


@pytest.mark.asyncio
async def test_unknown_empty_list_is_not_private_or_complete(task_db):
    db, tid = task_db
    await collector(db, tid, FakeLikesClient(target=profile())).run()
    task = db.get_task(tid)
    assert task["status"] == "partial" and task["visibility"] == "unknown"
    assert not task["collection_complete"] and not db.get_checkpoint(tid, "likes")["done"]


@pytest.mark.asyncio
async def test_explicit_public_empty_list_can_complete(task_db):
    db, tid = task_db
    await collector(db, tid, FakeLikesClient()).run()
    task = db.get_task(tid)
    assert task["status"] == "completed" and task["visibility"] == "public"
    assert task["collection_complete"] and task["video_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {"status_code": 403}, {"status_code": 0},
                                      {"status_code": 0, "aweme_list": None, "has_more": 0}])
async def test_blocked_or_malformed_likes_never_become_private(task_db, response):
    db, tid = task_db
    await collector(db, tid, FakeLikesClient(target=profile(), pages={0: response})).run()
    task = db.get_task(tid)
    assert task["status"] == "partial" and task["visibility"] == "unknown"
    assert not task["collection_complete"]


@pytest.mark.asyncio
async def test_search_never_matches_nickname_or_internal_uid(task_db):
    db, tid = task_db
    impostor = {"uid": "target.user", "sec_uid": "someone", "nickname": "target.user", "unique_id": "other"}
    fake = FakeLikesClient(search={0: users([impostor])})
    await collector(db, tid, fake).run()
    assert fake.calls == [("search", 0)]
    assert db.get_task(tid)["status"] == "partial"


@pytest.mark.asyncio
async def test_search_candidate_must_match_profile_identity_and_public_number(task_db):
    db, tid = task_db
    fake = FakeLikesClient(profiles={"target_sec": profile(sec="someone_else", show_favorite_list=False)})
    await collector(db, tid, fake).run()
    task = db.get_task(tid)
    assert task["status"] == "partial" and task["visibility"] == "unknown"
    assert not any(call[0] == "likes" for call in fake.calls)


@pytest.mark.asyncio
async def test_search_pages_to_exact_candidate_without_selecting_first(task_db):
    db, tid = task_db
    fake = FakeLikesClient(search={0: users([profile("other")["user"]], 20, 1),
                                  20: users([profile()["user"]])})
    await collector(db, tid, fake).run()
    assert fake.calls[:3] == [("search", 0), ("search", 20), ("profile", "target_sec")]
    assert db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_resume_does_not_mix_reassigned_public_number(task_db):
    db, tid = task_db
    db.update_task(tid, target_sec_uid="target_sec")
    fake = FakeLikesClient(target=profile("new.number", show_favorite_list=True))
    await collector(db, tid, fake).run()
    assert fake.calls == [("profile", "target_sec")]
    assert db.get_task(tid)["status"] == "partial"


@pytest.mark.asyncio
async def test_stale_resume_cursor_replays_once_and_preserves_rows(task_db):
    db, tid = task_db
    db.save_likes_page(tid, [normalize_liked_video(video("1"))], 100, False)
    fake = FakeLikesClient(pages={100: {"status_code": 2}, 0: likes([video("1"), video("2")])})
    await collector(db, tid, fake).run()
    assert [key for family, key in fake.calls if family == "likes"] == [100, 100, 100, 0]
    assert db.get_task(tid)["video_count"] == 2 and db.get_task(tid)["collection_complete"]
    assert db.get_checkpoint(tid, "likes")["restarts"] == 1


@pytest.mark.asyncio
async def test_resume_saved_rows_do_not_prove_current_visibility_when_flag_absent(task_db):
    db, tid = task_db
    db.update_task(tid, target_sec_uid="target_sec")
    db.save_likes_page(tid, [normalize_liked_video(video("1"))], 100, False)
    fake = FakeLikesClient(target=profile(), pages={100: likes([], 200)})
    await collector(db, tid, fake).run()
    task = db.get_task(tid)
    assert task["status"] == "partial" and task["visibility"] == "unknown"
    assert not task["collection_complete"] and not db.get_checkpoint(tid, "likes")["done"]
    assert task["video_count"] == 1 and db.get_checkpoint(tid, "likes")["cursor"] == 100
    assert fake.calls == [("profile", "target_sec"), ("likes", 100)]


@pytest.mark.asyncio
async def test_resume_replay_happens_at_most_once(task_db):
    db, tid = task_db
    db.save_likes_page(tid, [], 100, False)
    fake = FakeLikesClient(pages={0: {"status_code": 2}, 100: {"status_code": 2}})
    await collector(db, tid, fake).run()
    assert [key for family, key in fake.calls if family == "likes"] == [100, 100, 100, 0, 0, 0]
    assert db.get_checkpoint(tid, "likes")["restarts"] == 1
    assert not db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_login_failure_does_not_restart_saved_cursor(task_db):
    db, tid = task_db
    db.save_likes_page(tid, [], 100, False)
    fake = FakeLikesClient(pages={100: {"status_code": 1001}})
    await collector(db, tid, fake).run()
    assert db.get_task(tid)["status"] == "waiting_login"
    assert db.get_checkpoint(tid, "likes")["restarts"] == 0
    assert [key for family, key in fake.calls if family == "likes"] == [100, 100, 100]


@pytest.mark.asyncio
async def test_stalled_cursor_keeps_partial_rows(task_db):
    db, tid = task_db
    fake = FakeLikesClient(pages={0: likes([video("1")], 0, 1)})
    await collector(db, tid, fake).run()
    assert db.get_task(tid)["video_count"] == 1 and not db.get_task(tid)["collection_complete"]
    assert [key for family, key in fake.calls if family == "likes"] == [0, 0, 0]


@pytest.mark.asyncio
async def test_author_hydration_requires_matching_profile(task_db):
    db, tid = task_db
    first = {"uid": "first_uid", "sec_uid": "first_sec", "short_id": "unverified"}
    second = {"uid": "second_uid", "sec_uid": "second_sec"}
    fake = FakeLikesClient(pages={0: likes([video("1", first), video("2", second)])}, profiles={
        "first_sec": profile("", sec="first_sec", uid="first_uid", short_id="legacy.public"),
        "second_sec": profile("wrong.account", sec="other", uid="other"),
    })
    await collector(db, tid, fake).run()
    rows = {row["video_id"]: row for row in db.read_liked_videos(tid)}
    assert rows["1"]["author_douyin_id"] == "legacy.public"
    assert rows["2"]["author_douyin_id"] == ""
    assert db.get_task(tid)["unknown_authors"] == 1


@pytest.mark.asyncio
async def test_manual_login_retry_keeps_cursor(task_db):
    db, tid = task_db
    fake = FakeLikesClient(pages={0: [{"status_code": 1001}, likes([video()])]})
    await collector(db, tid, fake).run()
    assert fake.manual_waits == 1 and db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_stop_cancels_inflight_request_and_pauses(task_db):
    db, tid = task_db
    started, cancelled = asyncio.Event(), asyncio.Event()

    class HangingClient(FakeLikesClient):
        async def get_user_likes(self, sec_uid, cursor=0):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

    running = asyncio.create_task(collector(db, tid, HangingClient()).run())
    await asyncio.wait_for(started.wait(), 2)
    db.update_task(tid, stop_requested=True)
    await asyncio.wait_for(running, 1)
    assert cancelled.is_set() and db.get_task(tid)["status"] == "paused"
    assert not db.get_checkpoint(tid, "likes")["done"]


@pytest.mark.asyncio
async def test_resume_after_stop_during_author_hydration_keeps_finished_pages(task_db):
    db, tid = task_db
    started, cancelled = asyncio.Event(), asyncio.Event()
    author = {"uid": "author_uid", "sec_uid": "author_sec", "nickname": "视频作者"}

    class InterruptedAuthorClient(FakeLikesClient):
        async def get_user_info(self, sec_uid):
            if sec_uid == "author_sec":
                self.calls.append(("profile", sec_uid))
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
            return await super().get_user_info(sec_uid)

    first = InterruptedAuthorClient(pages={0: likes([video("1", author)])})
    running = asyncio.create_task(collector(db, tid, first).run())
    await asyncio.wait_for(started.wait(), 2)
    db.update_task(tid, stop_requested=True)
    await asyncio.wait_for(running, 1)
    paused = db.get_task(tid)
    assert cancelled.is_set() and paused["status"] == "paused"
    assert paused["collection_complete"] and paused["unknown_authors"] == 1
    assert db.get_checkpoint(tid, "likes")["done"]

    # The start endpoint clears this flag; restarting must hydrate saved rows
    # without requesting any favorite pages again.
    db.update_task(tid, stop_requested=False)
    resumed = FakeLikesClient(pages={}, profiles={
        "author_sec": profile("confirmed.author", sec="author_sec", uid="author_uid"),
    })
    await collector(db, tid, resumed).run()
    complete = db.get_task(tid)
    assert complete["status"] == "completed" and complete["verification_complete"]
    assert complete["unknown_authors"] == 0 and complete["video_count"] == 1
    assert db.read_liked_videos(tid)[0]["author_douyin_id"] == "confirmed.author"
    assert resumed.calls == [("profile", "target_sec"), ("profile", "author_sec")]


@pytest.mark.parametrize("stamp", [None, "bad", float("inf"), float("nan"), -1])
def test_missing_timestamp_and_canonical_link(stamp):
    row = normalize_liked_video({**video(stamp=stamp), "share_url": "javascript:alert(1)"})
    assert row["created_at"] is None and row["video_url"] == "https://www.douyin.com/video/1"


def test_invalid_video_identifier_prevents_checkpoint_advance():
    with pytest.raises(PageFailure):
        normalize_liked_video(video("1?redirect=evil"))


@pytest.mark.asyncio
async def test_adapter_uses_dedicated_endpoints_and_shared_signed_client(tmp_path):
    adapter = DouyinAdapter(tmp_path, lambda: False, lambda *args: None)
    calls = []

    class Client:
        headers = {}

    adapter.client = Client()

    async def fake_invoke(*args):
        calls.append(args)
        return {}

    adapter.invoke = fake_invoke
    await adapter.search_users("target.user", 20, "search-id")
    await adapter.get_user_likes("target_sec", 100)
    assert calls[0][0:2] == ("get", "/aweme/v1/web/discover/search/")
    assert calls[0][2]["search_channel"] == "aweme_user_web"
    assert calls[0][2]["offset"] == 20 and calls[0][2]["search_id"] == "search-id"
    assert calls[1][0:2] == ("get", "/aweme/v1/web/aweme/favorite/")
    assert calls[1][2]["sec_user_id"] == "target_sec" and calls[1][2]["max_cursor"] == 100


@pytest.mark.asyncio
async def test_search_opens_target_page_for_manual_verification(tmp_path):
    from unittest.mock import AsyncMock
    adapter = DouyinAdapter(tmp_path, lambda: False, lambda *args: None)

    class Page:
        url = 'https://www.douyin.com/'
        def is_closed(self): return False
        async def goto(self, url, **kwargs): self.url = url

    class Client:
        headers = {}

    adapter.page, adapter.client = Page(), Client()
    adapter.invoke = AsyncMock(return_value={})
    await adapter.search_users('target.user')
    assert adapter.page.url == 'https://www.douyin.com/search/target.user?type=user'
    assert adapter.client.headers['Referer'] == adapter.page.url


def test_search_challenge_inside_success_response_is_not_no_matches():
    with pytest.raises(ManualActionRequired):
        validate_response({'status_code':0, 'user_list':[], 'has_more':0,
                           'search_nil_info':{'search_nil_type':'verify_check','search_nil_item':'verify_check'}})


@pytest.mark.asyncio
async def test_cached_public_profile_is_reverified_before_use(task_db):
    db, tid = task_db
    other = db.create_task('', kind='likes', target_account='other.account')
    db.save_likes_page(other['id'], [normalize_liked_video(video('9', {
        'uid':'target_uid', 'sec_uid':'target_sec', 'unique_id':'target.user', 'nickname':'历史昵称'}))], 0, True)
    fake = FakeLikesClient(search={}, target=profile(show_favorite_list=False))
    await collector(db, tid, fake).run()
    assert fake.calls == [('profile', 'target_sec')]
    assert db.get_task(tid)['status'] == 'private'


@pytest.mark.asyncio
async def test_stale_cached_public_number_falls_back_to_search(task_db):
    db, tid = task_db
    other = db.create_task('', kind='likes', target_account='other.account')
    db.save_likes_page(other['id'], [normalize_liked_video(video('9', {
        'uid':'old_uid', 'sec_uid':'old_sec', 'unique_id':'target.user'}))], 0, True)
    fake = FakeLikesClient(profiles={'old_sec':profile('renamed.user',sec='old_sec',uid='old_uid')})
    await collector(db, tid, fake).run()
    assert ('search', 0) in fake.calls
    assert db.get_task(tid)['target_sec_uid'] == 'target_sec'
