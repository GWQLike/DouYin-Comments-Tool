"""Comment collection before the MediaCrawler task extensions."""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable

from backend.errors import StopRequested, PageFailure, ManualActionRequired
from backend.normalization import _string, normalize_comment


def validate_response(response: Any) -> dict:
    if not isinstance(response, dict):
        raise PageFailure("平台返回了非预期数据，无法确认采集完成。")
    if response.get("verify_check") or response.get("verify_info"):
        raise ManualActionRequired("平台要求登录或人工验证，请在专用浏览器处理。")
    # User-search can return HTTP/status_code 0 with no users while a challenge
    # is encoded only in search_nil_info. This is not an empty search result.
    nil_info = response.get("search_nil_info")
    if isinstance(nil_info, dict) and any(
        str(nil_info.get(key, "")).lower() in {"verify_check", "verify", "captcha", "login"}
        for key in ("search_nil_type", "search_nil_item")
    ):
        raise ManualActionRequired("账号搜索需要人工验证，请在专用浏览器完成验证后继续。")
    code = response.get("status_code", 0)
    if code not in (0, "0", None):
        message = str(response.get("status_msg") or response.get("message") or "")
        if str(code) in {"8", "1001", "1002", "1003", "1004", "20001"} or any(
            term in message.lower() for term in ("登录", "验证", "captcha", "verify", "login")
        ):
            raise ManualActionRequired("平台要求登录或人工验证，请在专用浏览器处理。")
        raise PageFailure(f"平台请求未成功（状态码 {str(code)[:20]}），已保留断点。")
    return response


class Collector:
    def __init__(self, db: Any, client: Any, task_id: str, *,
                 interval: tuple[float, float] | None = None,
                 concurrency: int = 3,
                 stop: Callable[[], bool] | None = None):
        self.db, self.client, self.task_id = db, client, task_id
        self.interval = interval
        self.stop = stop or (lambda: False)
        self.concurrency = max(1, min(3, concurrency))
        self._profile_requests: dict[str, asyncio.Task] = {}
        self.metrics = {'author_seconds': 0.0, 'profile_queries': 0, 'profile_reuses': 0}

    def check_stop(self) -> None:
        if self.stop() or self.db.should_stop(self.task_id):
            raise StopRequested()

    async def delay(self) -> None:
        # Production requests are paced once, at the adapter's HTTP boundary.
        # An explicit interval remains available for tests and baseline runs.
        if self.interval is None:
            self.check_stop()
            return
        remaining = random.uniform(*self.interval)
        while remaining > 0:
            self.check_stop()
            period = min(remaining, 0.2)
            await asyncio.sleep(period)
            remaining -= period
        self.check_stop()

    async def request(self, call: Callable[[], Awaitable[Any]]) -> dict:
        error: Exception | None = None
        for _ in range(3):
            await self.delay()
            try:
                # Poll for stop even while a network request is pending.
                pending = asyncio.create_task(call())
                try:
                    while not pending.done():
                        self.check_stop()
                        await asyncio.wait({pending}, timeout=0.2)
                    self.check_stop()
                    return validate_response(pending.result())
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            except StopRequested:
                raise
            except ManualActionRequired:
                if not hasattr(self.client, "wait_for_manual"):
                    raise
                await self.client.wait_for_manual()
                error = ManualActionRequired("人工处理后接口仍未恢复，请稍后继续。")
            except Exception as exc:
                # The upstream exception may contain response bodies/cookies. Never persist it.
                error = exc if isinstance(exc, PageFailure) else PageFailure("请求连续失败，请检查网络或稍后继续。")
        if isinstance(error, ManualActionRequired):
            raise error
        raise PageFailure(str(error)) from None

    async def parallel(self, items, operation) -> None:
        """Only a fixed number of workers exist, even for very large lists."""
        iterator = iter(items)

        async def consume():
            while True:
                self.check_stop()
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                await operation(item)

        workers = [asyncio.create_task(consume()) for _ in range(self.concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def author_profile(self, sec_uid: str) -> dict:
        """Reuse raw profile data, never another author's identity conclusion."""
        self.check_stop()
        pending = self._profile_requests.get(sec_uid)
        if pending is None:
            self.metrics['profile_queries'] += 1
            pending = asyncio.create_task(self.request(lambda: self.client.get_user_info(sec_uid)))
            self._profile_requests[sec_uid] = pending
        else:
            self.metrics['profile_reuses'] += 1
        try:
            return await pending
        except BaseException:
            if self._profile_requests.get(sec_uid) is pending:
                self._profile_requests.pop(sec_uid, None)
            raise

    async def collect_thread(self, video_id: str, parent_id: str = "") -> None:
        cp = self.db.get_checkpoint(self.task_id, parent_id)
        if cp.get("done"):
            return
        cursor = cp.get("cursor", 0)
        resumed = cursor not in (0, "0", None, "")
        restarted = False
        stalls = 0
        visited = {str(cursor)}
        while True:
            self.check_stop()

            async def fetch_page():
                if parent_id:
                    response = await self.client.get_sub_comments(video_id, parent_id, cursor)
                else:
                    response = await self.client.get_aweme_comments(video_id, cursor)
                response = validate_response(response)
                if "has_more" not in response or response["has_more"] not in (0, 1, "0", "1", False, True):
                    raise PageFailure("平台未返回有效分页结束标志，已保留断点。")
                done = response["has_more"] in (0, "0", False)
                raw = response.get("comments")
                if raw is None and done:
                    raw = []
                if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
                    raise PageFailure("平台评论列表格式异常，已保留断点。")
                next_cursor = response.get("cursor", cursor)
                if not str(next_cursor).isdigit():
                    raise PageFailure("平台返回的分页游标无效，已保留断点。")
                rows = []
                for item in raw:
                    normalized = normalize_comment(item, video_id, parent_id)
                    rows.append(normalized)
                    previews = item.get("reply_comment")
                    if previews is None:
                        continue
                    if not isinstance(previews, list) or any(not isinstance(reply, dict) for reply in previews):
                        raise PageFailure("平台内嵌回复格式异常，已保留断点。")
                    # Keep responses we already have, even if later reply pages
                    # fail. Preview rows never establish a reply-thread checkpoint.
                    rows.extend(normalize_comment(reply, video_id, parent_id or normalized["comment_id"], preview=True)
                                for reply in previews)
                return {"done": done, "rows": rows,
                        "next_cursor": int(next_cursor), "has_cursor": "cursor" in response}

            try:
                page = await self.request(fetch_page)
            except PageFailure:
                if resumed and not restarted:
                    # Expired resume cursors can also be rejected with an error
                    # response. Replay once after ordinary retries are exhausted;
                    # login/verification requests retain their separate exception.
                    cursor, visited, stalls, restarted = 0, {"0"}, 0, True
                    self.db.reset_checkpoint(self.task_id, parent_id)
                    continue
                raise
            done, rows, next_cursor = page["done"], page["rows"], page["next_cursor"]
            if done:
                self.db.save_page(self.task_id, parent_id, rows, next_cursor, True)
                return
            # Never advance past an empty page; never mark an unknown/stalled cursor done.
            stalled = not rows or not page["has_cursor"] or str(next_cursor) in visited
            if stalled:
                self.db.save_page(self.task_id, parent_id, rows, cursor, False)
                stalls += 1
                if stalls >= 3:
                    if resumed and not restarted:
                        # A stale resume token is safely recoverable by replaying this thread.
                        cursor, visited, stalls, restarted = 0, {"0"}, 0, True
                        self.db.reset_checkpoint(self.task_id, parent_id)
                        continue
                    raise PageFailure("连续三次空页或分页游标停滞，采集尚未完成，可稍后继续。")
                continue
            self.db.save_page(self.task_id, parent_id, rows, next_cursor, False)
            cursor = next_cursor
            visited.add(str(cursor))
            stalls = 0

    async def hydrate_authors(self) -> None:
        from backend.verification import hydrate
        await hydrate(self)

    async def run(self, verify_only: bool = False) -> None:
        errors: list[str] = []
        try:
            self.check_stop()
            task = self.db.get_task(self.task_id)
            video_id = _string(task.get("video_id"))
            if not video_id:
                async def resolve():
                    return {"video_id": await self.client.resolve_video_id(task["source_url"])}

                resolved = await self.request(resolve)
                video_id = _string(resolved.get("video_id"))
                if not video_id:
                    raise PageFailure("未能解析视频链接，请确认分享链接仍可访问。")
                self.db.update_task(self.task_id, video_id=video_id)
            if not verify_only:
                self.db.update_task(self.task_id, status="running", error=None)
                if not task.get("title"):
                    try:
                        detail = await self.request(lambda: self.client.get_video_by_id(video_id))
                        if detail.get("desc"):
                            self.db.update_task(self.task_id, title=str(detail["desc"]))
                    except PageFailure:
                        pass  # Missing title is independent of comment coverage.
                try:
                    await self.collect_thread(video_id)
                except PageFailure as exc:
                    errors.append(str(exc))
                async def collect_reply(thread):
                    try:
                        await self.collect_thread(video_id, _string(thread["comment_id"]))
                    except PageFailure as exc:
                        errors.append(str(exc))
                await self.parallel(self.db.list_threads(self.task_id), collect_reply)
                complete = self.db.get_checkpoint(self.task_id).get("done", False) and all(
                    self.db.get_checkpoint(self.task_id, _string(thread["comment_id"])).get("done", False)
                    for thread in self.db.list_threads(self.task_id)
                )
                self.db.update_task(self.task_id, collection_complete=bool(complete))
            await self.hydrate_authors()
            verified = not self.db.get_task(self.task_id)['unknown_authors']
            complete = bool(self.db.get_task(self.task_id).get("collection_complete"))
            self.db.update_task(self.task_id, verification_complete=verified,
                                status="completed" if complete else "partial",
                                error=errors[0] if errors else None)
        except StopRequested:
            self.db.update_task(self.task_id, status="paused")
        except ManualActionRequired as exc:
            self.db.update_task(self.task_id, status="waiting_login", error=str(exc))
            self.db.session_set("waiting", str(exc))
        except PageFailure as exc:
            self.db.update_task(self.task_id, status="partial", error=str(exc))
