"""Network-free, scaled wall-clock benchmark; never a claim about Douyin latency.

The baseline retains the former mean 3-second delay and sequential independent
work. Both runs use the real request gate, the same fake pages, network latency,
database, normalization, identity verification and TXT exporter. Giving even the
baseline the new gate/connection model makes this a conservative comparison of
the scheduling change, not a synthetic estimate of all potential improvements.
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


SCALE = 0.035  # One simulated second takes 35 ms of actual wall-clock time.
NETWORK_DELAY = 2 * SCALE
VIDEO_ID = "123456789"


def raw_comment(cid, owner, ordinal, *, alias=False, replies=0):
    user = {"sec_uid": f"p{owner}_sec"}
    if not alias:
        user["uid"] = f"p{owner}_uid"
    return {"cid": cid, "text": f"评论 {cid}：謝謝分享", "user": user,
            "create_time": 1791388800 + ordinal, "ip_label": "广东",
            "reply_comment_total": replies}


class SimulatedClient:
    def __init__(self):
        self.policy = RequestPolicy(base_interval=SCALE, max_concurrency=3)
        self.calls = []
        self.active = 0
        self.peak_active = 0
        self.main = [raw_comment(f"m{i:02d}", i // 2, i, alias=bool(i % 2), replies=2)
                     for i in range(12)]

    async def request(self, family, identity, cursor, response):
        async with self.policy.slot():
            await self.policy.wait_turn()
            self.calls.append((family, identity, cursor))
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            try:
                await asyncio.sleep(NETWORK_DELAY)
                self.policy.success()
                return deepcopy(response)
            finally:
                self.active -= 1

    async def get_aweme_comments(self, video_id, cursor):
        assert video_id == VIDEO_ID and cursor in (0, 20)
        response = {"status_code": 0, "comments": self.main[:6] if cursor == 0 else self.main[6:],
                    "cursor": 20 if cursor == 0 else 40, "has_more": int(cursor == 0)}
        return await self.request("main", "", cursor, response)

    async def get_sub_comments(self, video_id, parent_id, cursor):
        assert video_id == VIDEO_ID and cursor in (0, 20)
        number = int(parent_id[1:])
        part = int(cursor == 20)
        row = raw_comment(f"r{number:02d}_{part}", number // 2, 100 + number * 2 + part,
                          alias=bool(number % 2))
        response = {"status_code": 0, "comments": [row], "cursor": 20 if part == 0 else 40,
                    "has_more": int(part == 0)}
        return await self.request("reply", parent_id, cursor, response)

    async def get_user_info(self, sec_uid):
        owner = sec_uid.removeprefix("p").removesuffix("_sec")
        response = {"status_code": 0, "user": {"uid": f"p{owner}_uid", "sec_uid": sec_uid,
                    "nickname": f"用户 {owner}", "unique_id": f"public.{owner}"}}
        return await self.request("profile", sec_uid, 0, response)


async def run_scenario(directory, *, baseline):
    directory.mkdir()
    db = Database(directory / "isolated.sqlite3")
    task = db.create_task(f"https://www.douyin.com/video/{VIDEO_ID}", VIDEO_ID)
    tid = task["id"]
    db.update_task(tid, title="离线基准视频")
    fake = SimulatedClient()
    collector = Collector(db, fake, tid, interval=(3 * SCALE, 3 * SCALE) if baseline else None,
                          concurrency=1 if baseline else 3)
    started = time.perf_counter()
    await collector.run()
    elapsed = time.perf_counter() - started
    task = db.get_task(tid)
    assert task["status"] == "completed"
    assert task["collection_complete"] and task["verification_complete"]
    assert (task["main_count"], task["reply_count"], task["unknown_authors"]) == (12, 24, 0)

    # Compare the stored content as well as the public search/export results.
    rows = [{key: value for key, value in row.items() if key != "task_id"}
            for row in db.read_comments(tid)]
    items = select_comments(rows, Filters())
    output = export_txt(items)
    (directory / "comments.txt").write_bytes(output)
    assert len(rows) == len(items) == 36
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)
    assert all(row["resolved_state"] == "verified" for row in rows)
    assert {item["douyin_id"] for item in items} == {f"public.{i}" for i in range(6)}
    assert len(output.decode("utf-8-sig").strip().split("\r\n\r\n")) == 36

    ordered_cursors = defaultdict(list)
    for family, identity, cursor in fake.calls:
        if family != "profile":
            ordered_cursors[family, identity].append(cursor)
    assert len(ordered_cursors) == 13
    assert all(cursors == [0, 20] for cursors in ordered_cursors.values())
    assert Counter(family for family, _, _ in fake.calls) == {"main": 2, "reply": 24, "profile": 6}
    assert 1 <= fake.peak_active <= 3
    assert fake.active == 0
    return {"rows": rows, "items": items, "export": output,
            "measurement": {"wall_seconds": elapsed, "request_count": len(fake.calls),
                            "peak_active_http": fake.peak_active, "row_count": len(rows),
                            "profile_reuses": collector.metrics["profile_reuses"]}}


@pytest.mark.asyncio
async def test_scaled_scheduling_is_faster_with_identical_comments_and_export(tmp_path):
    baseline = await run_scenario(tmp_path / "sequential_baseline", baseline=True)
    optimized = await run_scenario(tmp_path / "optimized", baseline=False)
    assert baseline["rows"] == optimized["rows"]
    assert baseline["items"] == optimized["items"]
    assert baseline["export"] == optimized["export"]
    assert baseline["measurement"]["peak_active_http"] == 1
    assert optimized["measurement"]["peak_active_http"] > 1
    reduction = 1 - optimized["measurement"]["wall_seconds"] / baseline["measurement"]["wall_seconds"]
    report = {"method": "scaled simulation with real request gate; no external network",
              "scale_seconds_per_simulated_second": SCALE,
              "simulated_network_seconds_per_request": NETWORK_DELAY / SCALE,
              "baseline_fixed_delay_simulated_seconds": 3,
              "gate_interval_simulated_seconds_both_runs": 1,
              "baseline": baseline["measurement"], "optimized": optimized["measurement"],
              "wall_time_reduction_fraction": reduction,
              "identical_database_rows_search_order_count_and_export_bytes": True}
    report_path = tmp_path / "benchmark.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nScaled offline benchmark: {json.dumps(report, ensure_ascii=False)}\nReport: {report_path}")
    assert reduction >= 0.40, f"Scheduling improvement was {reduction:.1%}; see {report_path}"
