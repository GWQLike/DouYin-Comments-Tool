"""Task extensions registered with MediaCrawler; no production cursor loops here."""
from __future__ import annotations
import asyncio
import random
import time
from contextlib import contextmanager, nullcontext
from typing import Any, Callable, Awaitable
from backend.crawler import StopRequested, PageFailure, ManualActionRequired, _string, normalize_profile
from backend.responses import LoginRequired, RequestFailure, RequestRejected, explicit_list_private, validate_result
from backend.likes import account_key, public_number, profile_visibility

import re
from backend.recommendations import recommendation_time

class CommentTask:
    def __init__(self, db: Any, client: Any, task_id: str, *,
                 interval: tuple[float, float] | None = None,
                 concurrency: int = 1,
                 stop: Callable[[], bool] | None = None):
        self.db, self.client, self.task_id = db, client, task_id
        self.interval = interval
        self.stop = stop or (lambda: False)
        self.concurrency = max(1, min(3, concurrency))
        self._profile_requests: dict[str, asyncio.Task] = {}
        self.metrics = {'author_seconds': 0.0, 'profile_queries': 0, 'profile_reuses': 0,
                        'database_write_seconds': 0.0, 'stages': {}}

    @contextmanager
    def phase(self, name):
        before = self.db.get_task(self.task_id)
        transport = getattr(self.client, 'metrics', {})
        policy = getattr(getattr(self.client, 'policy', None), 'metrics', {})
        requests, http, wait = transport.get('http_requests', 0), transport.get('http_seconds', 0), policy.get('schedule_wait_seconds', 0)
        writes, began = self.metrics['database_write_seconds'], time.perf_counter()
        try:
            yield
        finally:
            after = self.db.get_task(self.task_id)
            self.metrics['stages'][name] = {
                'elapsed_seconds': round(time.perf_counter() - began, 3),
                'requests': transport.get('http_requests', 0) - requests,
                'http_seconds': round(transport.get('http_seconds', 0) - http, 3),
                'wait_seconds': round(policy.get('schedule_wait_seconds', 0) - wait, 3),
                'database_write_seconds': round(self.metrics['database_write_seconds'] - writes, 3),
                'new_records': sum(after[key] - before[key] for key in ('main_count', 'reply_count', 'video_count')),
                'authors_resolved': max(0, before['unknown_authors'] - after['unknown_authors']) if name == 'authors' else 0,
            }

    async def verify_saved_authors(self):
        with self.phase('authors'):
            await self.hydrate_authors()
        task = self.db.get_task(self.task_id)
        self.db.update_task(self.task_id, verification_complete=not task['unknown_authors'],
            status='private' if task['visibility'] == 'private' else 'completed' if task['collection_complete'] else 'partial')

    def fail_task(self, status, message=None, **fields):
        if getattr(self, '_verifying', False):
            message = self.db.get_task(self.task_id)['error'] or message
            fields.pop('collection_complete', None)
        self.db.update_task(self.task_id, status=status, error=message, **fields)

    def write(self, operation, *args, **kwargs):
        began = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            self.metrics['database_write_seconds'] += time.perf_counter() - began

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
            policy = getattr(self.client, 'policy', None)
            generation = policy.generation if policy else None
            starts = policy.metrics['request_starts'] if policy else 0
            try:
                scope = getattr(self.client, 'response_scope', nullcontext)
                with scope():
                    # Poll for stop even while a network request is pending.
                    pending = asyncio.create_task(call())
                    try:
                        while not pending.done():
                            self.check_stop()
                            await asyncio.wait({pending}, timeout=0.2)
                        self.check_stop()
                        return validate_result(pending.result())
                    finally:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            except StopRequested:
                raise
            except ManualActionRequired as exc:
                if not hasattr(self.client, "wait_for_manual"):
                    raise
                if isinstance(exc, LoginRequired) and policy and not policy.paused:
                    self.client.require_manual(challenge=False, reason='登录失效')
                await self.client.wait_for_manual()
                error = ManualActionRequired("人工处理后接口仍未恢复，请稍后继续。")
            except RequestFailure as exc:
                if not exc.retryable:
                    raise RequestRejected(str(exc), kind=exc.outcome.kind,
                                          resource_missing=exc.outcome.resource_missing) from None
                error = exc
            except Exception as exc:
                # A structurally invalid successful HTTP response is still a
                # failed page. Back off once; transport failures already did so.
                if policy and policy.generation == generation and policy.metrics['request_starts'] > starts:
                    policy.failure()
                # The upstream exception may contain response bodies/cookies. Never persist it.
                error = exc if isinstance(exc, PageFailure) else PageFailure("请求连续失败，请检查网络或稍后继续。")
        if isinstance(error, ManualActionRequired):
            raise error
        if isinstance(error, RequestFailure):
            # Transport retry exhaustion must not become a new paginator retry.
            # Cursor/empty-page decisions remain the paginator's responsibility.
            raise RequestRejected(str(error), kind=error.outcome.kind,
                                  resource_missing=error.outcome.resource_missing) from None
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
        from backend.engine_storage import CommentPages
        self.client.video_url = f'https://www.douyin.com/video/{video_id}'
        token = self.client._referer.set(self.client.video_url)
        try:
            await self.engine.get_comments(video_id, page_hooks=CommentPages(self, video_id, parent_id), parent_id=parent_id)
        finally:
            self.client._referer.reset(token)

    async def hydrate_authors(self) -> None:
        from backend.verification import hydrate
        await hydrate(self)

    async def run(self, verify_only: bool = False) -> None:
        errors: list[str] = []
        self._verifying = verify_only
        try:
            self.check_stop()
            task = self.db.get_task(self.task_id)
            if verify_only:
                await self.verify_saved_authors()
                return
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
                    except (PageFailure, RequestRejected):
                        pass  # Missing title is independent of comment coverage.
                try:
                    with self.phase('main_comments'):
                        await self.collect_thread(video_id)
                except (PageFailure, RequestRejected) as exc:
                    errors.append(str(exc))
                async def collect_reply(thread):
                    try:
                        await self.collect_thread(video_id, _string(thread["comment_id"]))
                    except (PageFailure, RequestRejected) as exc:
                        errors.append(str(exc))
                with self.phase('replies'):
                    await self.parallel(self.db.pending_threads(self.task_id), collect_reply)
                complete = self.db.comments_complete(self.task_id)
                self.db.update_task(self.task_id, collection_complete=bool(complete), error=errors[0] if errors else None)
            if task['verify_authors']:
                with self.phase('authors'):
                    await self.hydrate_authors()
            verified = not self.db.get_task(self.task_id)['unknown_authors']
            complete = bool(self.db.get_task(self.task_id).get("collection_complete"))
            self.db.update_task(self.task_id, verification_complete=verified,
                                status="completed" if complete else "partial",
                                error=errors[0] if errors else None)
        except StopRequested as exc:
            self.fail_task("paused", getattr(exc, "public_reason", None))
        except ManualActionRequired as exc:
            self.fail_task("waiting_login", str(exc))
            self.db.session_set("waiting", str(exc))
        except (PageFailure, RequestRejected) as exc:
            self.fail_task("partial", str(exc))


class LikesTask(CommentTask):
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
        return explicit_list_private(response, self.list_kind)

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
        self.write(self.db.save_likes_page, self.task_id, rows, cursor, done, checkpoint_key=self.list_kind)
        self._video_ids.update(new_ids)
        if limit and len(self._video_ids) >= limit and not done:
            raise PageFailure(f"已达到本次 {limit} 条采集上限，已停止；当前结果不是完整列表。")

    async def verify_target(self, account: str, candidate: dict) -> dict | None:
        sec_uid = _string(candidate.get("sec_uid"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sec_uid):
            return None
        try:
            profile = await self.request(lambda: self.client.get_user_info(sec_uid))
        except RequestRejected as exc:
            # Only an explicit missing profile invalidates this discovery clue.
            # Login, challenge, throttling and transport failures must still stop.
            if exc.resource_missing:
                return None
            raise
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
            raise PageFailure("原目标主页已失效或当前抖音号与任务不一致，已停止；请核对抖音号后新建任务。")
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
        from backend.engine_storage import ActivityPages
        if not self.db.get_checkpoint(self.task_id, self.list_kind)['done']:
            self.request_count()
        await self.engine.collect_activity(lambda cursor: self.fetch_list_page(sec_uid, cursor), ActivityPages(self, visibility))

    async def hydrate_authors(self) -> None:
        from backend.verification import hydrate
        await hydrate(self)

    async def run(self, verify_only: bool = False) -> None:
        self._verifying = verify_only
        try:
            self.check_stop()
            task = self.db.get_task(self.task_id)
            if verify_only:
                await self.verify_saved_authors()
                return
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
                    with self.phase('list'):
                        await self.collect_likes(target["sec_uid"], target["visibility"])
                except (PageFailure, RequestRejected) as exc:
                    error = str(exc)
                if self.db.get_task(self.task_id)["status"] == "private":
                    return
                complete = bool(self.db.get_checkpoint(self.task_id, self.list_kind)["done"])
                self.db.update_task(self.task_id, collection_complete=complete, error=error)
            if task['verify_authors']:
                with self.phase('authors'):
                    await self.hydrate_authors()
            complete = self.db.get_task(self.task_id)["collection_complete"]
            self.db.update_task(self.task_id, status="completed" if complete else "partial", error=error,
                                verification_complete=not self.db.liked_authors(self.task_id))
        except StopRequested as exc:
            self.fail_task("paused", getattr(exc, "public_reason", None))
        except ManualActionRequired as exc:
            self.fail_task("waiting_login", str(exc))
            self.db.session_set("waiting", str(exc))
        except (PageFailure, RequestRejected) as exc:
            self.fail_task("partial", str(exc), collection_complete=False)


class RecommendationsTask(LikesTask):
    list_kind = 'recommendations'
    list_label = '推荐'

    def target_visibility(self, user: dict) -> str:
        # show_favorite_list controls likes only. No independently verified
        # recommendation permission flag is currently available on profiles.
        return 'unknown'

    def action_time(self, raw: dict) -> int | None:
        return recommendation_time(raw, getattr(self, 'target_identity', {}))

    async def fetch_list_page(self, sec_uid: str, cursor: int) -> dict:
        return await self.client.get_user_recommendations(sec_uid, cursor, count=self.request_count())
