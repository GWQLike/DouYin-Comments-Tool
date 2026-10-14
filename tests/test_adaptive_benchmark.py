"""Compare adaptive pacing with the current one-second gate, without network.

Both sides use the real collector, bounded reply/profile workers, request policy,
SQLite writes, normalization, search and TXT exporter. Only pacing differs. Wall
time is scaled to keep the regression useful on Windows; these measurements are
not promises about external Douyin latency or rate limits.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from copy import deepcopy
import json
import time

import pytest

from backend.crawler import Collector
from backend.db import Database
from backend.request_policy import RequestPolicy
from backend.search import Filters, export_txt, select_comments


SCALE = 0.05
VIDEO_ID = "123456789"


def raw_comment(cid, owner, ordinal, *, alias=False, replies=0):
    user = {"sec_uid": f"p{owner}_sec"}
    if not alias:
        user["uid"] = f"p{owner}_uid"
    return {"cid": cid, "text": f"评论 {cid}：謝謝分享", "user": user,
            "create_time": 1791388800 + ordinal, "ip_label": "广东",
            "reply_comment_total": replies}


class SimulatedClient:
    def __init__(self, *, baseline, parent_count, network_seconds):
        self.policy = (RequestPolicy(base_interval=SCALE, max_concurrency=3) if baseline
                       else RequestPolicy(time_scale=SCALE, max_concurrency=3))
        self.network_delay = network_seconds * SCALE
        self.calls = []
        self.intervals = []
        self.active = self.peak_active = 0
        self.main = [raw_comment(f"m{i:02d}", i % 3, i, alias=bool(i % 2), replies=2)
                     for i in range(parent_count)]

    async def request(self, family, identity, cursor, response):
        async with self.policy.slot():
            await self.policy.wait_turn()
            generation = self.policy.generation
            self.intervals.append(self.policy.interval)
            self.calls.append((family, identity, cursor))
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            try:
                await asyncio.sleep(self.network_delay)
                self.policy.success(generation=generation)
                return deepcopy(response)
            finally:
                self.active -= 1

    async def get_aweme_comments(self, video_id, cursor):
        assert video_id == VIDEO_ID and cursor in (0, 20)
        midpoint = len(self.main) // 2
        rows = self.main[:midpoint] if cursor == 0 else self.main[midpoint:]
        response = {"status_code": 0, "comments": rows,
                    "cursor": 20 if cursor == 0 else 40, "has_more": int(cursor == 0)}
        return await self.request("main", "", cursor, response)

    async def get_sub_comments(self, video_id, parent_id, cursor):
        assert video_id == VIDEO_ID and cursor in (0, 20)
        number = int(parent_id[1:])
        part = int(cursor == 20)
        row = raw_comment(f"r{number:02d}_{part}", number % 3, 100 + number * 2 + part,
                          alias=bool(number % 2))
        response = {"status_code": 0, "comments": [row],
                    "cursor": 20 if part == 0 else 40, "has_more": int(part == 0)}
        return await self.request("reply", parent_id, cursor, response)

    async def get_user_info(self, sec_uid):
        owner = sec_uid.removeprefix("p").removesuffix("_sec")
        response = {"status_code": 0, "user": {"uid": f"p{owner}_uid", "sec_uid": sec_uid,
                    "nickname": f"用户 {owner}", "unique_id": f"public.{owner}"}}
        return await self.request("profile", sec_uid, 0, response)


async def run_scenario(directory, *, baseline, request_count, network_seconds):
    # Two main pages, two reply pages per parent and three distinct profiles.
    parent_count = (request_count - 5) // 2
    assert request_count == 2 + parent_count * 2 + 3
    directory.mkdir()
    db = Database(directory / "isolated.sqlite3")
    tid = db.create_task(f"https://www.douyin.com/video/{VIDEO_ID}", VIDEO_ID)["id"]
    db.update_task(tid, title="第二轮离线基准视频")
    fake = SimulatedClient(baseline=baseline, parent_count=parent_count,
                           network_seconds=network_seconds)
    collector = Collector(db, fake, tid, concurrency=3)
    started = time.perf_counter()
    await collector.run()
    elapsed = time.perf_counter() - started
    task = db.get_task(tid)
    assert task["status"] == "completed"
    assert task["collection_complete"] and task["verification_complete"]
    assert (task["main_count"], task["reply_count"], task["unknown_authors"]) == (
        parent_count, parent_count * 2, 0)

    rows = [{key: value for key, value in row.items() if key != "task_id"}
            for row in db.read_comments(tid)]
    items = select_comments(rows, Filters())
    output = export_txt(items)
    (directory / "comments.txt").write_bytes(output)
    assert len(rows) == len(items) == parent_count * 3
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)
    assert all(row["resolved_state"] == "verified" for row in rows)
    assert {item["douyin_id"] for item in items} == {f"public.{i}" for i in range(3)}
    assert len(output.decode("utf-8-sig").strip().split("\r\n\r\n")) == parent_count * 3

    ordered_cursors = defaultdict(list)
    for family, identity, cursor in fake.calls:
        if family != "profile":
            ordered_cursors[family, identity].append(cursor)
    assert len(ordered_cursors) == parent_count + 1
    assert all(cursors == [0, 20] for cursors in ordered_cursors.values())
    assert Counter(family for family, _, _ in fake.calls) == {
        "main": 2, "reply": parent_count * 2, "profile": 3}
    assert len(fake.calls) == request_count
    assert 1 <= fake.peak_active <= 3 and fake.active == 0
    if baseline:
        assert fake.intervals == [SCALE] * request_count
    else:
        assert fake.intervals[0] == 0.5 * SCALE
        assert fake.policy.interval == pytest.approx((0.3 if request_count == 41 else 0.25) * SCALE)

    return {"rows": rows, "items": items, "export": output, "calls": Counter(fake.calls),
            "measurement": {"wall_seconds": elapsed, "request_count": len(fake.calls),
                            "peak_active_http": fake.peak_active, "row_count": len(rows),
                            "profile_reuses": collector.metrics["profile_reuses"],
                            "final_interval_simulated_seconds": fake.policy.interval / SCALE,
                            "policy": fake.policy.summary()}}


@pytest.mark.asyncio
@pytest.mark.parametrize("request_count,network_seconds,required_reduction", [
    pytest.param(41, 0.28, 0.40, id="41-requests"),
    pytest.param(83, 0.28, 0.40, id="83-requests"),
    pytest.param(41, 2.0, None, id="high-latency"),
])
async def test_adaptive_pacing_preserves_results_and_improves_throughput(
        tmp_path, request_count, network_seconds, required_reduction):
    baseline = await run_scenario(tmp_path / "one_second_baseline", baseline=True,
                                  request_count=request_count, network_seconds=network_seconds)
    optimized = await run_scenario(tmp_path / "adaptive", baseline=False,
                                   request_count=request_count, network_seconds=network_seconds)
    for key in ("rows", "items", "export", "calls"):
        assert baseline[key] == optimized[key]
    if required_reduction is None:
        assert optimized["measurement"]["peak_active_http"] == 3
    reduction = 1 - optimized["measurement"]["wall_seconds"] / baseline["measurement"]["wall_seconds"]
    report = {"method": "scaled simulation with real collector, request policy, SQLite and TXT; no external network",
              "scale_seconds_per_simulated_second": SCALE,
              "simulated_network_seconds_per_request": network_seconds,
              "baseline_start_interval_simulated_seconds": 1,
              "concurrency_limit_both_runs": 3,
              "baseline": baseline["measurement"], "optimized": optimized["measurement"],
              "wall_time_reduction_fraction": reduction,
              "identical_database_rows_search_order_count_and_export_bytes": True}
    report_path = tmp_path / "adaptive-benchmark.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nAdaptive offline benchmark: {json.dumps(report, ensure_ascii=False)}\nReport: {report_path}")
    if required_reduction is not None:
        assert reduction >= required_reduction, f"Scheduling improvement was {reduction:.1%}; see {report_path}"
