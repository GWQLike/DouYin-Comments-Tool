"""Collect only the accessible likes of an exactly verified public Douyin account.

Network wrappers use the existing MediaCrawler session/signature implementation.
An empty response, absent permission field, or 403 never proves a private list.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from typing import Any

from backend.crawler import (
    Collector, ManualActionRequired, PageFailure, StopRequested,
    _string, normalize_profile, validate_response,
)


def account_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", _string(value).strip()).casefold()


def public_number(user: dict) -> str:
    value = _string(user.get("unique_id")).strip()
    if value in ("", "0"):
        value = _string(user.get("short_id")).strip()
    return "" if value == "0" else value


def profile_visibility(user: dict) -> str:
    # The profile schema defines this as an optional boolean. In particular,
    # None/0/"false" and guessed favorite_permission codes are not evidence.
    flag = user.get("show_favorite_list")
    if flag is False:
        return "private"
    if flag is True:
        return "public"
    return "unknown"


def normalize_liked_video(raw: dict) -> dict:
    video_id = _string(raw.get("aweme_id"))
    if not re.fullmatch(r"[0-9]{1,32}", video_id):
        raise PageFailure("视频列表中存在无效视频 ID，本页未确认完成。")
    author = raw.get("author")
    author = author if isinstance(author, dict) else {}
    uid, sec_uid = (_string(author.get(key)).strip() for key in ("uid", "sec_uid"))
    uid, sec_uid = ("" if value == "0" else value for value in (uid, sec_uid))
    number = _string(author.get("unique_id")).strip()
    number = "" if number == "0" else number
    try:
        stamp = float(raw.get("create_time"))
        stamp = int(stamp) if math.isfinite(stamp) and 0 < stamp < 253402300800 else None
    except (TypeError, ValueError, OverflowError):
        stamp = None
    return {
        "video_id": video_id, "title": _string(raw.get("desc")),
        "author_name": _string(author.get("nickname")), "author_douyin_id": number,
        "created_at": stamp, "video_url": f"https://www.douyin.com/video/{video_id}",
        "uid": uid, "sec_uid": sec_uid, "author_key": uid or sec_uid or f"video:{video_id}",
    }


class LikesCollector(Collector):
    list_kind = "likes"
    list_label = "喜欢"

    def list_options(self) -> dict:
        if not hasattr(self, '_list_options'):
            # Task conditions are immutable; load quota membership only once.
            self._list_options = self.db.get_task(self.task_id)
            self._video_ids = self.db.liked_video_ids(self.task_id) if self._list_options['max_videos'] else set()
        return self._list_options

    def target_visibility(self, user: dict) -> str:
        return profile_visibility(user)

    async def fetch_list_page(self, sec_uid: str, cursor: int) -> dict:
        count = self.request_count()
        if count == 18:
            return await self.client.get_user_likes(sec_uid, cursor)
        return await self.client.get_user_likes(sec_uid, cursor, count=count)

    def request_count(self) -> int:
        task = self.list_options()
        remaining = task['max_videos'] - len(self._video_ids) if task['max_videos'] else 18
        if remaining <= 0:
            raise PageFailure(f"已达到本次 {task['max_videos']} 条采集上限，已停止；当前结果不是完整列表。")
        return min(18, remaining)

    def list_is_private(self, response: Any) -> bool:
        return False

    def action_time(self, raw: dict) -> int | None:
        # The favorite response has no verified per-item like timestamp.
        # create_time is publication time; the page cursor is not an item time.
        return None

    def filter_list_rows(self, rows: list[dict]) -> list[dict]:
        task = self.list_options()
        start, end = task['start_time'], task['end_time']
        if start is None and end is None:
            return rows
        if any(row.get('action_at') is None for row in rows):
            action = '点赞' if self.list_kind == 'likes' else '推荐'
            raise PageFailure(f"平台未提供可核验的{action}时间，无法按所选时间范围采集。已保留进度；可新建不限制时间的任务。")
        return [row for row in rows if (start is None or row['action_at'] >= start)
                and (end is None or row['action_at'] <= end)]

    def save_list_page(self, rows: list, cursor: int, done: bool) -> None:
        task = self.list_options()
        limit = task['max_videos']
        new_ids = set()
        if limit:
            kept, clipped = [], False
            for row in rows:
                is_new = row['video_id'] not in self._video_ids and row['video_id'] not in new_ids
                if is_new and len(self._video_ids) + len(new_ids) >= limit:
                    clipped = True
                    continue
                if is_new:
                    new_ids.add(row['video_id'])
                kept.append(row)
            if clipped:
                cursor = self.db.get_checkpoint(self.task_id, self.list_kind)['cursor']
                done = False
            rows = kept
        self.db.save_likes_page(self.task_id, rows, cursor, done, checkpoint_key=self.list_kind)
        self._video_ids.update(new_ids)
        if limit and len(self._video_ids) >= limit and not done:
            raise PageFailure(f"已达到本次 {limit} 条采集上限，已停止；当前结果不是完整列表。")

    def restart_list_checkpoint(self) -> None:
        # A changed first page cannot be appended to an activity-ordered batch:
        # newly liked/recommended videos would appear after older saved rows.
        if self.db.get_task(self.task_id)['list_order'] == 'activity':
            action = '点赞' if self.list_kind == 'likes' else '推荐'
            raise PageFailure(f"无法按原分页位置继续，已保留结果和断点；为保持{action}顺序，请新建批次重新采集。")
        self.db.reset_checkpoint(self.task_id, self.list_kind)

    async def verify_target(self, account: str, candidate: dict) -> dict | None:
        sec_uid = _string(candidate.get("sec_uid"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sec_uid):
            return None
        profile = await self.request(lambda: self.client.get_user_info(sec_uid))
        verified = normalize_profile(profile, candidate)
        if not verified or account_key(verified["douyin_id"]) != account_key(account):
            return None
        user = profile["user"]
        return {**verified, "visibility": self.target_visibility(user)}

    async def resolve_target(self, task: dict) -> dict:
        account = task["target_account"]
        if task.get("target_sec_uid"):
            verified = await self.verify_target(account, {"sec_uid": task["target_sec_uid"]})
            if verified:
                return verified
            # Never mix the prior account's saved videos with a newly reassigned
            # public number during resume. A fresh batch can resolve it again.
            raise PageFailure("该主页当前抖音号与任务不一致，已停止；请核对抖音号后新建任务。")
        cached = {}
        for candidate in self.db.cached_account_profiles(account):
            verified = await self.verify_target(account, candidate)
            if verified:
                cached[verified['sec_uid']] = verified
        if len(cached) == 1:
            return next(iter(cached.values()))
        if len(cached) > 1:
            raise PageFailure("历史资料中出现多个同号主页，无法唯一核验目标账号，已停止。")
        offset, search_id, visited = 0, "", {"0"}
        while True:
            response = await self.request(lambda: self.client.search_users(account, offset, search_id))
            users = response.get("user_list")
            if not isinstance(users, list) or any(not isinstance(item, dict) for item in users):
                raise PageFailure("用户搜索未返回可核验的用户列表，暂时无法定位此抖音号。")
            matches = {}
            for item in users:
                user = item.get("user_info")
                if not isinstance(user, dict):
                    continue
                if account_key(public_number(user)) != account_key(account):
                    continue
                verified = await self.verify_target(account, user)
                if verified:
                    matches[verified["sec_uid"]] = verified
            if len(matches) == 1:
                return next(iter(matches.values()))
            if len(matches) > 1:
                raise PageFailure("用户搜索出现多个同号主页，无法唯一核验目标账号，已停止。")
            more = response.get("has_more")
            if more in (0, "0", False):
                raise PageFailure("未能核验到完全匹配的抖音号，请检查输入；这不代表账号不存在。")
            cursor = response.get("cursor")
            if more not in (1, "1", True) or not str(cursor).isdigit() or str(cursor) in visited or not users:
                raise PageFailure("用户搜索分页信息不完整，暂时无法核验此抖音号。")
            offset = int(cursor)
            visited.add(str(cursor))
            log_pb = response.get("log_pb")
            if isinstance(log_pb, dict):
                search_id = _string(log_pb.get("impr_id"))

    async def collect_likes(self, sec_uid: str, visibility: str) -> None:
        checkpoint = self.db.get_checkpoint(self.task_id, self.list_kind)
        if checkpoint["done"]:
            return
        self.request_count()
        # Saved rows describe an earlier observation. They cannot prove the
        # list is still public after an interruption or privacy-setting change.
        cursor = checkpoint["cursor"]
        resumed, restarted, stalls = bool(cursor), False, 0
        visited = {str(cursor)}
        while True:
            self.check_stop()

            async def fetch_page():
                response = await self.fetch_list_page(sec_uid, cursor)
                if self.list_is_private(response):
                    return {"private": True}
                response = validate_response(response)
                if response.get("has_more") not in (0, 1, "0", "1", False, True):
                    raise PageFailure(f"{self.list_label}列表未返回有效分页结束标志，已保留断点。")
                done = response["has_more"] in (0, "0", False)
                raw = response.get("aweme_list")
                # Null/missing is intentionally different from an explicit [];
                # restricted responses can use null even with has_more=0.
                if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
                    raise PageFailure(f"{self.list_label}列表未返回有效视频列表，无法判断是否公开，已保留断点。")
                next_cursor = response.get("max_cursor", cursor)
                if not str(next_cursor).isdigit():
                    raise PageFailure(f"{self.list_label}列表分页游标无效，已保留断点。")
                return {"rows": [{**normalize_liked_video(row), 'action_at': self.action_time(row)} for row in raw], "done": done,
                        "next_cursor": int(next_cursor), "has_cursor": "max_cursor" in response}

            try:
                page = await self.request(fetch_page)
            except PageFailure:
                if resumed and not restarted:
                    self.restart_list_checkpoint()
                    cursor, restarted, stalls, visited = 0, True, 0, {"0"}
                    continue
                raise
            if page.get("private"):
                self.db.update_task(self.task_id, visibility="private", status="private",
                                    collection_complete=False, error=f"该账号未公开{self.list_label}列表")
                return
            rows, done, next_cursor = page["rows"], page["done"], page["next_cursor"]
            raw_count = len(rows)
            if rows:
                visibility = "public"
                self.db.update_task(self.task_id, visibility="public")
            rows = self.filter_list_rows(rows)
            if done:
                if visibility != "public":
                    raise PageFailure(f"未获得视频且平台未明确说明{self.list_label}列表是否公开，暂时无法确认；请稍后重试。")
                self.save_list_page(rows, next_cursor, True)
                return
            if not raw_count or not page["has_cursor"] or str(next_cursor) in visited:
                self.save_list_page(rows, cursor, False)
                stalls += 1
                if stalls >= 3:
                    if resumed and not restarted:
                        self.restart_list_checkpoint()
                        cursor, restarted, stalls, visited = 0, True, 0, {"0"}
                        continue
                    raise PageFailure(f"{self.list_label}列表连续三次空页或游标停滞，尚未采集完成，可稍后继续。")
                continue
            self.save_list_page(rows, next_cursor, False)
            cursor, stalls = next_cursor, 0
            visited.add(str(cursor))

    async def hydrate_authors(self) -> None:
        self.db.update_task(self.task_id, status="verifying")

        async def hydrate(author):
            self.check_stop()
            if not author.get("sec_uid"):
                return
            try:
                profile = await self.author_profile(author["sec_uid"])
            except PageFailure:
                return
            verified = normalize_profile(profile, author)
            if verified:
                self.db.save_liked_author(self.task_id, author["author_key"],
                                          verified["nickname"], verified["douyin_id"])

        started = time.perf_counter()
        try:
            await self.parallel(self.db.liked_authors(self.task_id), hydrate)
        finally:
            self.metrics['author_seconds'] += time.perf_counter() - started

    async def run(self, verify_only: bool = False) -> None:
        try:
            self.check_stop()
            task = self.db.get_task(self.task_id)
            self.db.update_task(self.task_id, status="running", error=None)
            target = await self.resolve_target(task)
            self.target_identity = target
            self.db.update_task(self.task_id, target_sec_uid=target["sec_uid"],
                                target_nickname=target["nickname"], visibility=target["visibility"],
                                title=f"{target['nickname'] or task['target_account']}的{self.list_label}列表")
            if target["visibility"] == "private":
                self.db.update_task(self.task_id, status="private", collection_complete=False,
                                    error=f"该账号未公开{self.list_label}列表")
                return
            error = None
            if not verify_only:
                try:
                    await self.collect_likes(target["sec_uid"], target["visibility"])
                except PageFailure as exc:
                    error = str(exc)
                if self.db.get_task(self.task_id)["status"] == "private":
                    return
                complete = bool(self.db.get_checkpoint(self.task_id, self.list_kind)["done"])
                self.db.update_task(self.task_id, collection_complete=complete)
            await self.hydrate_authors()
            complete = self.db.get_task(self.task_id)["collection_complete"]
            self.db.update_task(self.task_id, status="completed" if complete else "partial", error=error,
                                verification_complete=not self.db.liked_authors(self.task_id))
        except StopRequested:
            self.db.update_task(self.task_id, status="paused")
        except ManualActionRequired as exc:
            self.db.update_task(self.task_id, status="waiting_login", error=str(exc))
            self.db.session_set("waiting", str(exc))
        except PageFailure as exc:
            self.db.update_task(self.task_id, status="partial", error=str(exc), collection_complete=False)
