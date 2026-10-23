"""Resumable workers with bounded independent work and ordered pagination."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# Running with -m otherwise creates two exception-class identities when the
# adapter imports this module by its package name.
if __name__ == "__main__":
    sys.modules.setdefault("backend.crawler", sys.modules[__name__])


class StopRequested(Exception):
    pass


class ControlledStop(StopRequested):
    """A locally authored control reason safe to show in the workbench."""
    def __init__(self, reason, *, kind=None):
        super().__init__()
        self.public_reason = reason
        self.kind = kind


class PageFailure(Exception):
    pass


class ManualActionRequired(Exception):
    pass


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_comment(raw: dict, video_id: str, parent_id: str = "", *, preview: bool = False) -> dict:
    """Do not confuse internal uid/legacy short_id with an asserted public Douyin ID."""
    comment_id = _string(raw.get("cid") or raw.get("comment_id"))
    if not comment_id:
        raise PageFailure("平台返回缺少评论 ID 的记录，本页未确认完成。")
    user = raw.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    uid = _string(user.get("uid"))
    sec_uid = _string(user.get("sec_uid"))
    uid = "" if uid == "0" else uid
    sec_uid = "" if sec_uid == "0" else sec_uid
    public_id = _string(user.get("unique_id")).strip()
    public_id = "" if public_id == "0" else public_id
    stamp = raw.get("create_time")
    try:
        number = float(stamp)
        stamp = int(number) if math.isfinite(number) and 0 < number < 253402300800 else None
    except (ValueError, TypeError, OverflowError):
        stamp = None
    try:
        # A missing count is not evidence that there are no replies. Query that
        # thread once; an explicit zero needs no additional request.
        reply_count = max(0, int(raw.get("reply_comment_total", 1)))
    except (ValueError, TypeError):
        reply_count = 1
    if raw.get("reply_comment"):
        reply_count = max(reply_count, 1)
    present = {field for field, source in (
        ('text', 'text'), ('created_at', 'create_time'), ('ip_label', 'ip_label'),
        ('reply_count', 'reply_comment_total')) if source in raw}
    present.update(field for field, source in (
        ('uid', 'uid'), ('sec_uid', 'sec_uid'), ('nickname', 'nickname'),
        ('douyin_id', 'unique_id')) if source in user)
    if raw.get('reply_comment'):
        present.add('reply_count')
    # A null/non-text value is unavailable, not an explicit empty edit.
    if not isinstance(raw.get('text'), str):
        present.discard('text')
    return {
        "comment_id": comment_id,
        "video_id": _string(video_id),
        "parent_id": _string(parent_id),
        "text": _string(raw.get("text")),
        "created_at": stamp,
        "ip_label": _string(raw.get("ip_label")),
        "nickname": _string(user.get("nickname")),
        "douyin_id": public_id,
        "uid": uid,
        "sec_uid": sec_uid,
        "author_key": uid or sec_uid or f"comment:{comment_id}",
        "identity_state": "verified" if public_id else "unknown",
        "reply_count": reply_count,
        "_present_fields": tuple(sorted(present)),
        "_source": "preview" if preview else "full",
    }


def normalize_profile(profile: dict, author: dict) -> dict | None:
    """Only a matching profile can establish the legacy, publicly displayed short_id."""
    if not isinstance(profile, dict):
        return None
    user = profile.get("user")
    if not isinstance(user, dict):
        return None
    uid, sec_uid = _string(user.get("uid")), _string(user.get("sec_uid"))
    uid, sec_uid = ('' if value == '0' else value for value in (uid, sec_uid))
    for key, actual in (("uid", uid), ("sec_uid", sec_uid)):
        expected = _string(author.get(key))
        if expected and actual and expected != actual:
            return None
    confirmed = bool((uid and uid == author.get("uid")) or
                     (sec_uid and sec_uid == author.get("sec_uid")))
    if not confirmed:
        return None
    public_id = _string(user.get("unique_id")).strip()
    if public_id in ("", "0"):
        legacy = _string(user.get("short_id")).strip()
        public_id = legacy if legacy not in ("", "0") else ""
    return {
        "uid": uid or _string(author.get("uid")),
        "sec_uid": sec_uid or _string(author.get("sec_uid")),
        "nickname": _string(user.get("nickname")) or _string(author.get("nickname")),
        "douyin_id": public_id,
        "identity_state": "verified" if public_id else "unknown",
    }


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


async def worker(args: argparse.Namespace) -> int:
    from contextlib import ExitStack
    from backend.runtime_control import ProcessLease, OwnershipError, owner_alive
    directory = Path(args.db).resolve().parent
    owner_token = getattr(args, 'service_token', None)
    try:
        with ExitStack() as leases:
            if owner_token:
                if not owner_alive(directory, owner_token):
                    raise OwnershipError('所属服务已退出，未启动采集。')
            else:
                # Standalone CLI runs must not compete with a running service.
                leases.enter_context(ProcessLease(directory, 'service'))
            leases.enter_context(ProcessLease(directory, 'worker'))
            if owner_token and not owner_alive(directory, owner_token):
                raise OwnershipError('所属服务已退出，未启动采集。')
            return await _worker_owned(args, (lambda: owner_alive(directory, owner_token)) if owner_token else None)
    except OwnershipError as exc:
        print(str(exc), flush=True)
        return 1


async def _worker_owned(args: argparse.Namespace, owner_check) -> int:
    worker_started = time.perf_counter()
    # Imports stay outside import-time pure normalization / collection tests.
    from backend.db import Database
    from backend.engine import run_engine

    db = Database(str(Path(args.db).resolve()))
    from backend.runtime_control import StopSignal, POLL_SECONDS
    stop = StopSignal(lambda: db.should_stop(args.task) if args.task else
                      db.session_get().get('status') == 'logged_out', owner=owner_check)
    if getattr(args, 'stop_pipe', False):
        import threading
        def watch_pipe():
            try:
                sys.stdin.buffer.read(1)
            finally:
                stop.request()
        threading.Thread(target=watch_pipe, name='service-stop', daemon=True).start()
    verifying = args.verify_only

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop.request)

    def waiting(wait: bool, message: str | None = None) -> None:
        nonlocal verifying
        db.session_set("waiting" if wait else "ready", message)
        if args.task:
            current = db.get_task(args.task)
            verifying = verifying or current['status'] == 'verifying'
            if wait:
                db.transition_task(args.task, 'waiting', message=message, verify_only=verifying)
            else:
                db.transition_task(args.task, 'resume', verify_only=verifying)

    try:
        db.session_set("waiting")
        pending = asyncio.create_task(run_engine(db, args, stop, waiting))
        try:
            while not pending.done():
                if stop():
                    raise StopRequested()
                await asyncio.wait({pending}, timeout=POLL_SECONDS)
            await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        return 0
    except StopRequested as exc:
        if args.task:
            db.transition_task(args.task, 'worker_pause', message=getattr(exc, 'public_reason', None))
        else:
            db.session_set("logged_out")
        return 0
    except Exception as exc:
        # Do not expose upstream exception bodies, browser command lines or login tokens.
        message = "采集进程未能启动或浏览器已关闭。请检查安装、Node.js 和浏览器后重试。"
        if isinstance(exc, (PageFailure, ManualActionRequired)):
            message = str(exc)
        db.session_set("error", message)
        if args.task:
            db.transition_task(args.task, 'worker_failed', message=message)
        return 1
    finally:
        if stop.parent_lost or (owner_check is not None and not owner_check()):
            db.session_set('logged_out', '所属服务已退出，采集已停止并保留断点。')


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Douyin comment collection worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--task")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--max-seconds", type=float, default=0)
    parser.add_argument("--stop-on-challenge", action="store_true")
    parser.add_argument("--service-token")
    parser.add_argument("--stop-pipe", action="store_true")
    return asyncio.run(worker(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
