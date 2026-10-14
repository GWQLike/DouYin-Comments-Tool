"""Offline behavior tests: every platform response below is an explicit fake."""
from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from backend.crawler import Collector, ManualActionRequired, PageFailure, StopRequested, normalize_comment, normalize_profile
from backend.db import Database
from backend.douyin_adapter import DouyinAdapter, video_id_from_url


def comment(cid, *, user=None, replies=0, text="测试评论", stamp=1720000000):
    return {"cid": cid, "text": text, "create_time": stamp, "ip_label": "广东",
            "reply_comment_total": replies,
            "user": user if user is not None else {"uid": "u1", "sec_uid": "s1", "unique_id": "public.one", "nickname": "测试用户"}}


def page(rows, cursor=0, more=0):
    return {"status_code": 0, "comments": rows, "cursor": cursor, "has_more": more}


class FakeClient:
    def __init__(self, main=None, replies=None, profiles=None):
        self.main = main or {0: page([])}
        self.replies = replies or {}
        self.profiles = profiles or {}
        self.calls = []
        self.attempts = defaultdict(int)
        self.manual_waits = 0

    def response(self, mapping, key):
        self.calls.append(key)
        result = mapping[key]
        if isinstance(result, list):
            count = self.attempts[key]
            result = result[min(count, len(result) - 1)]
            self.attempts[key] += 1
        if isinstance(result, Exception):
            raise result
        return result

    async def get_aweme_comments(self, video_id, cursor):
        return self.response(self.main, int(cursor))

    async def get_sub_comments(self, video_id, parent_id, cursor):
        return self.response(self.replies, (parent_id, int(cursor)))

    async def get_user_info(self, sec_uid):
        return self.response(self.profiles, sec_uid)

    async def get_video_by_id(self, video_id):
        return {"desc": "离线测试视频"}

    async def wait_for_manual(self):
        self.manual_waits += 1


@pytest.fixture
def task_db(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    task = db.create_task("https://www.douyin.com/video/123456789", "123456789")
    return db, task["id"]


def collector(db, task_id, client, **kwargs):
    return Collector(db, client, task_id, interval=(0, 0), **kwargs)


@pytest.mark.asyncio
async def test_all_main_pages_and_reply_pages_are_saved_without_filtering(task_db):
    db, tid = task_db
    client = FakeClient(
        main={0: page([comment("m1", replies=2)], 20, 1), 20: page([comment("m2")], 40)},
        replies={
            ("m1", 0): page([comment("r1", text="只有回复命中")], 20, 1),
            ("m1", 20): page([comment("r2")], 40),
        },
    )
    await collector(db, tid, client).run()
    task = db.get_task(tid)
    assert task["status"] == "completed"
    assert task["collection_complete"] and task["verification_complete"]
    assert (task["main_count"], task["reply_count"]) == (2, 2)
    assert db.get_checkpoint(tid, "m1")["done"]
    assert {r["parent_id"] for r in db.read_comments(tid) if r["comment_id"].startswith("r")} == {"m1"}


@pytest.mark.asyncio
async def test_missing_reply_count_still_inspects_reply_thread(task_db):
    db, tid = task_db
    raw = comment("m1")
    del raw["reply_comment_total"]
    client = FakeClient(main={0: page([raw])}, replies={("m1", 0): page([comment("r1")])})
    await collector(db, tid, client).run()
    assert db.get_task(tid)["reply_count"] == 1


@pytest.mark.asyncio
async def test_failed_reply_keeps_partial_and_other_threads_continue(task_db):
    db, tid = task_db
    client = FakeClient(
        main={0: page([comment("m1", replies=1), comment("m2", replies=1)])},
        replies={("m1", 0): [RuntimeError("private cookie payload")], ("m2", 0): page([comment("r2")])},
    )
    await collector(db, tid, client).run()
    task = db.get_task(tid)
    assert task["status"] == "partial" and not task["collection_complete"]
    assert task["reply_count"] == 1
    assert client.calls.count(("m1", 0)) == 3
    assert "private" not in task["error"]


@pytest.mark.asyncio
async def test_bad_page_retried_without_false_completion(task_db):
    db, tid = task_db
    client = FakeClient(main={0: [{"comments": [], "status_code": 0}]})
    await collector(db, tid, client).run()
    assert client.calls.count(0) == 3
    assert not db.get_checkpoint(tid)["done"]
    assert not db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_stalled_pages_keep_rows_and_never_mark_done(task_db):
    db, tid = task_db
    client = FakeClient(main={0: page([comment("m1")], 0, 1)})
    await collector(db, tid, client).run()
    assert client.calls.count(0) == 3
    task = db.get_task(tid)
    assert task["main_count"] == 1 and task["status"] == "partial"
    assert not task["collection_complete"]


@pytest.mark.asyncio
async def test_empty_page_with_more_does_not_advance_or_complete(task_db):
    db, tid = task_db
    client = FakeClient(main={0: page([], 20, 1)})
    await collector(db, tid, client).run()
    assert db.get_checkpoint(tid)["cursor"] == 0
    assert client.calls == [0, 0, 0]
    assert not db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_stale_resume_replays_thread_and_deduplicates(task_db):
    db, tid = task_db
    db.save_page(tid, "", [normalize_comment(comment("m1"), "123456789")], 20, False)
    client = FakeClient(main={20: page([], 20, 1), 0: page([comment("m1"), comment("m2")], 40)})
    await collector(db, tid, client).run()
    assert client.calls == [20, 20, 20, 0]
    assert db.get_task(tid)["main_count"] == 2
    assert db.get_task(tid)["collection_complete"]
    assert db.get_checkpoint(tid)["restarts"] == 1


@pytest.mark.asyncio
async def test_resume_skips_completed_main_and_replies(task_db):
    db, tid = task_db
    db.save_page(tid, "", [normalize_comment(comment("m1", replies=1), "123456789")], 20, True)
    db.save_page(tid, "m1", [normalize_comment(comment("r1"), "123456789", "m1")], 20, False)
    client = FakeClient(replies={("m1", 20): page([comment("r2")], 40)})
    await collector(db, tid, client).run()
    assert client.calls == [("m1", 20)]
    assert db.get_task(tid)["reply_count"] == 2


@pytest.mark.asyncio
async def test_cancel_pending_request_preserves_checkpoint_and_pauses(task_db):
    db, tid = task_db
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingClient(FakeClient):
        async def get_aweme_comments(self, video_id, cursor):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

    running = asyncio.create_task(collector(db, tid, HangingClient()).run())
    await asyncio.wait_for(started.wait(), timeout=2)
    db.update_task(tid, stop_requested=True)
    await asyncio.wait_for(running, timeout=1)
    assert cancelled.is_set()
    assert db.get_task(tid)["status"] == "paused"
    assert not db.get_checkpoint(tid)["done"]


@pytest.mark.asyncio
async def test_manual_verification_resumes_same_page(task_db):
    db, tid = task_db
    client = FakeClient(main={0: [{"status_code": 1001, "status_msg": "请登录"}, page([comment("m1")])]})
    await collector(db, tid, client).run()
    assert client.manual_waits == 1
    assert client.calls == [0, 0]
    assert db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_unknown_profile_never_becomes_verified_or_uses_internal_uid(task_db):
    db, tid = task_db
    user = {"uid": "123", "sec_uid": "sec123", "nickname": "同名用户", "short_id": "unconfirmed"}
    client = FakeClient(main={0: page([comment("m1", user=user)])}, profiles={"sec123": {"user": {"uid": "other", "sec_uid": "else", "unique_id": "fake_public"}}})
    await collector(db, tid, client).run()
    task = db.get_task(tid)
    assert task["collection_complete"] and not task["verification_complete"]
    assert task["unknown_authors"] == 1
    row = db.read_comments(tid)[0]
    assert row["public_number"] == "" and row["resolved_state"] == "unknown"


@pytest.mark.asyncio
async def test_profile_legacy_number_and_missing_nickname_are_hydrated(task_db):
    db, tid = task_db
    client = FakeClient(
        main={0: page([comment("m1", user={"uid": "123", "sec_uid": "sec123"})])},
        profiles={"sec123": {"user": {"uid": "123", "sec_uid": "sec123", "short_id": "public123", "nickname": "原始昵称"}}},
    )
    await collector(db, tid, client).run()
    row = db.read_comments(tid)[0]
    assert row["public_number"] == "public123" and row["display_name"] == "原始昵称"
    assert db.get_task(tid)["verification_complete"]


@pytest.mark.asyncio
async def test_aliases_with_same_sec_uid_use_verified_identity(task_db):
    db, tid = task_db
    client = FakeClient(main={0: page([
        comment("m1", user={"uid": "123", "sec_uid": "sec123", "unique_id": "public123", "nickname": "昵称"}),
        comment("m2", user={"sec_uid": "sec123", "nickname": "昵称"}),
    ])})
    await collector(db, tid, client).run()
    assert {r["public_number"] for r in db.read_comments(tid)} == {"public123"}
    assert db.get_task(tid)["verification_complete"]


@pytest.mark.asyncio
async def test_conflicting_public_numbers_do_not_silently_verify_every_comment(task_db):
    db, tid = task_db
    client = FakeClient(main={0: page([
        comment("m1", user={"uid": "123", "sec_uid": "sec123", "unique_id": "public.one", "nickname": "昵称"}),
        comment("m2", user={"uid": "123", "sec_uid": "sec123", "unique_id": "public.two", "nickname": "昵称"}),
    ])})
    await collector(db, tid, client).run()
    assert db.get_task(tid)["collection_complete"]
    assert not db.get_task(tid)["verification_complete"]
    assert all(row["resolved_state"] != "verified" for row in db.read_comments(tid))


@pytest.mark.asyncio
async def test_inline_replies_survive_when_reply_pagination_fails(task_db):
    db, tid = task_db
    main = comment("m1", replies=2)
    main["reply_comment"] = [comment("r1", text="主评论响应中已获得的回复")]
    client = FakeClient(main={0: page([main])}, replies={("m1", 0): RuntimeError("network unavailable")})
    await collector(db, tid, client).run()
    task = db.get_task(tid)
    assert not task["collection_complete"]
    replies = [row for row in db.read_comments(tid) if row["parent_id"] == "m1"]
    assert [row["comment_id"] for row in replies] == ["r1"]


@pytest.mark.asyncio
async def test_inline_replies_do_not_skip_full_reply_pages_or_duplicate_rows(task_db):
    db, tid = task_db
    main = comment("m1", replies=2)
    main["reply_comment"] = [comment("r1")]
    client = FakeClient(main={0: page([main])}, replies={("m1", 0): page([comment("r1"), comment("r2")])})
    await collector(db, tid, client).run()
    assert ("m1", 0) in client.calls
    assert db.get_task(tid)["collection_complete"]
    assert db.get_task(tid)["reply_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("previews", [{"cid": "r1"}, ["not a comment"]])
async def test_malformed_inline_reply_prevents_false_completion(task_db, previews):
    db, tid = task_db
    main = comment("m1", replies=1)
    main["reply_comment"] = previews
    client = FakeClient(main={0: page([main])})
    await collector(db, tid, client).run()
    assert not db.get_task(tid)["collection_complete"]
    assert not db.get_checkpoint(tid)["done"]


@pytest.mark.asyncio
async def test_rejected_resume_cursor_can_replay_the_thread(task_db):
    db, tid = task_db
    db.save_page(tid, "", [normalize_comment(comment("m1"), "123456789")], 20, False)
    client = FakeClient(main={
        20: {"status_code": 2, "status_msg": "invalid cursor"},
        0: page([comment("m1"), comment("m2")], 40),
    })
    await collector(db, tid, client).run()
    assert db.get_task(tid)["collection_complete"]
    assert db.get_task(tid)["main_count"] == 2
    assert db.get_checkpoint(tid)["restarts"] == 1


@pytest.mark.asyncio
async def test_rejected_resume_restarts_at_most_once(task_db):
    db, tid = task_db
    db.save_page(tid, "", [], 20, False)
    failure = {"status_code": 2, "status_msg": "invalid cursor"}
    client = FakeClient(main={20: failure, 0: failure})
    await collector(db, tid, client).run()
    assert client.calls == [20, 20, 20, 0, 0, 0]
    assert db.get_checkpoint(tid)["restarts"] == 1
    assert not db.get_task(tid)["collection_complete"]


@pytest.mark.asyncio
async def test_login_failure_does_not_restart_resume_checkpoint(task_db):
    db, tid = task_db
    db.save_page(tid, "", [], 20, False)
    client = FakeClient(main={20: {"status_code": 1001, "status_msg": "请登录"}})
    await collector(db, tid, client).run()
    assert client.calls == [20, 20, 20]
    assert db.get_checkpoint(tid)["restarts"] == 0
    assert db.get_checkpoint(tid)["cursor"] == 20
    assert not db.get_task(tid)["collection_complete"]
    assert db.get_task(tid)["status"] == "waiting_login"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["driver", "browser"])
async def test_stop_during_browser_startup_cancels_and_closes_owned_driver(tmp_path, monkeypatch, stage):
    import backend.douyin_adapter as adapter_module
    import playwright.async_api

    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()
    stop = False

    async def hang():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    class FakeBrowserType:
        async def connect_over_cdp(self, *args, **kwargs):
            await hang()

    class FakePlaywright:
        chromium = FakeBrowserType()

    class FakeManager:
        async def start(self):
            if stage == "driver":
                await hang()
            return FakePlaywright()

        async def __aexit__(self, *args):
            closed.set()

    monkeypatch.setattr(adapter_module, "load_upstream_client", lambda: object)
    monkeypatch.setattr(playwright.async_api, "async_playwright", FakeManager)
    monkeypatch.setenv("DOUYIN_CDP_URL", "http://127.0.0.1:9222")
    adapter = DouyinAdapter(tmp_path / "profile", lambda: stop, lambda *args: None)
    entering = asyncio.create_task(adapter.__aenter__())
    await asyncio.wait_for(started.wait(), timeout=2)
    stop = True
    with pytest.raises(StopRequested):
        await asyncio.wait_for(entering, timeout=1)
    assert cancelled.is_set() and closed.is_set()


@pytest.mark.asyncio
async def test_verify_only_never_changes_collection_completeness(task_db):
    db, tid = task_db
    db.save_page(tid, "", [normalize_comment(comment("m1"), "123456789")], 20, False)
    client = FakeClient()
    await collector(db, tid, client).run(verify_only=True)
    assert client.calls == []
    assert not db.get_task(tid)["collection_complete"]


@pytest.mark.parametrize("stamp", [None, "", "not-a-date", float("nan"), float("inf"), -1, 253402300800])
def test_invalid_timestamps_are_missing(stamp):
    assert normalize_comment(comment("m1", stamp=stamp), "v")["created_at"] is None


def test_matching_identity_is_required_for_profile_and_legacy_id():
    assert normalize_profile({"user": {"uid": "999", "unique_id": "public"}}, {"uid": "123"}) is None
    assert normalize_profile({"user": {"short_id": "public"}}, {"uid": "123"}) is None
    assert normalize_profile({"user": {"uid": "123", "short_id": "0"}}, {"uid": "123"})["identity_state"] == "unknown"


@pytest.mark.parametrize("url,expected", [
    ("https://www.douyin.com/video/123", "123"),
    ("https://www.douyin.com/note/123", "123"),
    ("https://www.iesdouyin.com/share/video/123/", "123"),
    ("https://www.douyin.com/user/abc?modal_id=123", "123"),
    ("https://v.douyin.com/abc/", ""),
])
def test_valid_platform_links(url, expected):
    assert video_id_from_url(url) == expected


@pytest.mark.parametrize("url", ["https://douyin.com.evil.test/video/123", "https://127.0.0.1/video/123", "file:///video/123", "https://user:pass@douyin.com/video/123", "https://douyin.com:8765/video/123"])
def test_reject_redirects_outside_platform(url):
    with pytest.raises(PageFailure):
        video_id_from_url(url)


def test_real_upstream_import_loads_core_and_login_without_default_stores():
    root = Path(__file__).resolve().parents[1]
    code = "from backend.douyin_adapter import load_upstream_client; import sys,inspect; c=load_upstream_client(); assert 'vendor' in inspect.getfile(c); assert 'media_platform.douyin.core' in sys.modules; assert 'media_platform.douyin.login' in sys.modules; assert 'store.douyin' not in sys.modules; print(c.__name__)"
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "DouYinClient" in result.stdout
