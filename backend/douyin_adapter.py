"""Transport/session extension used by the actual MediaCrawler engine lifecycle."""
from __future__ import annotations

import asyncio
import copy
import importlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from backend.crawler import ManualActionRequired, PageFailure, StopRequested, ControlledStop, validate_response
from backend.stable_policy import StableRequestPolicy
from backend.upstream import module


def pooled_client_type(upstream, owner):
    """Explicit public parameter/signing hook plus a pooled HTTP boundary."""
    if not hasattr(upstream, 'prepare_request'):
        return upstream
    helpers = importlib.import_module("media_platform.douyin.help")

    async def threaded_sign(uri, query, post_data, user_agent, page=None):
        began = time.monotonic()
        try:
            return await asyncio.get_running_loop().run_in_executor(
                owner.sign_executor, helpers.get_a_bogus_from_js, uri, query, user_agent)
        finally:
            owner.metrics["signature_seconds"] += time.monotonic() - began

    class PooledClient(upstream):
        async def _signed(self, method, uri, values, headers):
            async with owner.policy.slot():
                async def prepare():
                    await owner.ensure_session()
                    async with owner._page_lock:
                        snapshot = copy.deepcopy(values)
                        request_headers = dict(headers or self.headers)
                        # Endpoints may build parameters and headers before a
                        # queued session refresh. Capture cookie-bound fields
                        # together while refresh_cookies cannot change them.
                        for key in ("Cookie", "uifid"):
                            if key in self.headers:
                                request_headers[key] = self.headers[key]
                            else:
                                request_headers.pop(key, None)
                        if uri == "/aweme/v1/web/aweme/detail/":
                            snapshot["uifid"] = (self.cookie_dict.get("UIFID")
                                                 or self.cookie_dict.get("UIFID_TEMP", ""))
                            fingerprint = self.cookie_dict.get("s_v_web_id", "")
                            snapshot["verifyFp"] = snapshot["fp"] = fingerprint
                        referer = owner._referer.get()
                        if referer:
                            request_headers["Referer"] = referer
                        self.signer = threaded_sign
                        await self.prepare_request(uri, snapshot, request_headers, request_method=method)
                    return snapshot, request_headers
                snapshot, request_headers = await owner.policy.prepare_start(prepare)
                return await owner.http_request(method, f"{self._host}{uri}", scheduled=True,
                    headers=request_headers, **{"params" if method == "GET" else "data": snapshot})

        async def get(self, uri, params=None, headers=None):
            return await self._signed("GET", uri, params, headers)

        async def post(self, uri, data, headers=None):
            return await self._signed("POST", uri, data, headers)

        async def request(self, method, url, **kwargs):
            async with owner.policy.slot():
                return await owner.http_request(method, url, **copy.deepcopy(kwargs))

        async def resolve_short_url(self, short_url):
            async with owner.policy.slot():
                response = await owner.http_request("GET", short_url, timeout=10, raw=True)
                if response.status_code in (301, 302, 303, 307, 308):
                    return response.headers.get("Location", "")
                return ""

    return PooledClient


def load_upstream_client():
    return module('media_platform.douyin.client').DouYinClient


def video_id_from_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise PageFailure("请使用有效的抖音视频链接。")
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in ("douyin.com", "iesdouyin.com")):
        raise PageFailure("分享链接跳转到了非抖音域名，已停止解析。")
    if parsed.port not in (None, 80, 443):
        raise PageFailure("抖音视频链接端口无效。")
    match = re.search(r"/(?:video|note)/(\d+)(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "aweme_id"):
        value = query.get(key, [""])[0]
        if value.isdigit():
            return value
    return ""


class DouyinAdapter:
    def __init__(self, profile: Path, stop: Callable[[], bool],
                 waiting: Callable[[bool, str | None], None]):
        self.profile = profile
        self.stop = stop
        self.waiting = waiting
        self.playwright_manager = None
        self.playwright = None
        self.context = None
        self.page = None
        self.client = None
        self.video_url = "https://www.douyin.com/"
        self.http_client = None
        self.sign_executor = None
        self.policy = StableRequestPolicy(profile.parent / 'collection-control.json', check_stop=self.check_stop)
        self.cdp_manager = None
        self.stop_on_challenge = False
        self.max_requests = 0
        self.deadline = None
        self._page_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._referer = ContextVar("douyin_request_referer", default=None)
        self._session_checked = self._cookies_checked = None
        self._logged_in = False
        self._manual_task = None
        self._session_generation = 0
        self._http_active = 0
        self.metrics = {"http_requests": 0, "http_seconds": 0.0, "signature_seconds": 0.0,
                        "session_checks": 0, "session_seconds": 0.0,
                        "cookie_refreshes": 0, "cookie_seconds": 0.0,
                        "http_peak_active": 0, "http_request_categories": {}}

    def performance_summary(self):
        return {**self.metrics, **self.policy.summary()}

    def check_stop(self):
        if self.stop():
            raise StopRequested()
        if self.deadline and time.monotonic() >= self.deadline:
            raise ControlledStop('已达到实测时间上限，断点已保存。')
        if self.page is not None and self.page.is_closed():
            raise PageFailure("专用浏览器已关闭，已保存采集进度，可稍后继续。")

    async def sleep(self, seconds: float):
        remaining = seconds
        while remaining > 0:
            self.check_stop()
            period = min(remaining, 0.2)
            await asyncio.sleep(period)
            remaining -= period
        self.check_stop()

    async def interruptible(self, awaitable):
        pending = asyncio.create_task(awaitable)
        try:
            while not pending.done():
                self.check_stop()
                await asyncio.wait({pending}, timeout=0.2)
            self.check_stop()
            return pending.result()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def __aenter__(self):
        client_type = load_upstream_client()
        from playwright.async_api import async_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            async def start_browser():
                # Retain the context manager before its startup await so even an
                # interrupted driver startup has an owned object to close.
                self.playwright_manager = async_playwright()
                self.playwright = await self.playwright_manager.start()
                self.check_stop()
                endpoint = os.environ.get('DOUYIN_CDP_URL')
                if not endpoint:
                    raise PageFailure('未连接专用 Chrome，请从工作台启动采集。')
                self.cdp_manager = module('tools.cdp_browser').CDPBrowserManager(connection_url=endpoint)
                self.context = await self.cdp_manager.launch_and_connect(self.playwright)
                self.check_stop()

            await self.interruptible(start_browser())
            self.page = next((page for page in self.context.pages if urlsplit(page.url).hostname in ('www.douyin.com', 'douyin.com')), None)
            if self.page is None:
                self.page = next((page for page in self.context.pages if page.url == 'about:blank'), None) or await self.context.new_page()
            self.page.set_default_timeout(5000)
            try:
                if urlsplit(self.page.url).hostname not in ("www.douyin.com", "douyin.com"):
                    await self.interruptible(self.page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000))
            except (StopRequested, PageFailure):
                raise
            except Exception as exc:
                # Keep only Chromium's fixed error category, never URLs/tokens.
                category = re.search(r'net::(ERR_[A-Z_]+)', str(exc))
                reason = f'（{category.group(1)}）' if category else ''
                raise PageFailure(f"无法连接抖音网页{reason}，请检查网络后继续；尚未开始采集。") from None
            user_agent = await self.page.evaluate("() => navigator.userAgent")
            import httpx
            from tools.httpx_util import make_async_client
            self.http_client = make_async_client(proxy=None, follow_redirects=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
            self.sign_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="douyin-sign")
            client_type = pooled_client_type(client_type, self)
            self.client = client_type(
                timeout=25, proxy=None, proxy_ip_pool=None,
                headers={"User-Agent": user_agent, "Cookie": "", "Host": "www.douyin.com",
                         "Origin": "https://www.douyin.com/", "Referer": "https://www.douyin.com/",
                         "Content-Type": "application/json;charset=UTF-8"},
                playwright_page=self.page, cookie_dict={},
            )
            await self.refresh_cookies(force=True)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        if self._manual_task and not self._manual_task.done():
            self._manual_task.cancel()
            await asyncio.gather(self._manual_task, return_exceptions=True)
        if self.http_client:
            with suppress(Exception):
                await self.http_client.aclose()
        if self.sign_executor:
            # Running execjs cannot be interrupted safely, but it never occupies
            # the event loop or retains a network connection after cancellation.
            self.sign_executor.shutdown(wait=False, cancel_futures=True)
        if self.playwright_manager:
            with suppress(Exception):
                await self.playwright_manager.__aexit__(None, None, None)

    async def refresh_cookies(self, *, force=False):
        async with self._page_lock:
            self.check_stop()
            if not force and self._cookies_checked is not None and time.monotonic() - self._cookies_checked < 30:
                return
            began = time.monotonic()
            try:
                await self.client.update_cookies(self.context)
                uifid = self.client.cookie_dict.get("UIFID") or self.client.cookie_dict.get("UIFID_TEMP")
                if uifid:
                    self.client.headers["uifid"] = uifid
                else:
                    self.client.headers.pop("uifid", None)
                self._cookies_checked = time.monotonic()
                self._session_generation += 1
                self.metrics["cookie_refreshes"] += 1
            finally:
                self.metrics["cookie_seconds"] += time.monotonic() - began

    async def logged_in(self, *, force=False) -> bool:
        async with self._page_lock:
            self.check_stop()
            if not force and self._session_checked is not None and time.monotonic() - self._session_checked < 30:
                return self._logged_in
            began = time.monotonic()
            try:
                self._logged_in = bool(await self.client.pong(self.context))
                self._session_checked = time.monotonic()
                self.metrics["session_checks"] += 1
                return self._logged_in
            except Exception:
                self.check_stop()
                return False  # Navigation may temporarily replace the JS context.
            finally:
                self.metrics["session_seconds"] += time.monotonic() - began

    def require_manual(self, *, challenge=True, reason='人工验证'):
        if isinstance(self.policy, StableRequestPolicy):
            self.policy.pause(challenge=challenge, reason=reason)
        else:
            self.policy.pause()
        if challenge and self.stop_on_challenge:
            raise ControlledStop('实测遇到首次人机验证，已立即停止并保留断点。')
        self._session_checked = self._cookies_checked = None

    async def ensure_session(self):
        async with self._session_lock:
            if not await self.logged_in():
                self.require_manual(challenge=False)
                raise ManualActionRequired("登录已失效，请在专用浏览器重新登录。")
            await self.refresh_cookies()

    async def wait_for_login(self):
        if await self.logged_in(force=True):
            await self.refresh_cookies(force=True)
            self.waiting(False, None)
            return
        self.waiting(True, "请在专用抖音浏览器中点击登录并扫码；完成后自动继续。")
        await self.page.bring_to_front()
        # Login is intentionally manual; don't import the upstream slider solver.
        while not await self.logged_in(force=True):
            await self.sleep(1)
        await self.refresh_cookies(force=True)
        self.waiting(False, None)

    async def wait_for_manual(self):
        if self._manual_task is None or (self._manual_task.done() and self.policy.paused):
            self.require_manual()
            self._manual_task = asyncio.create_task(self._manual_prompt())
        await asyncio.shield(self._manual_task)

    async def _manual_prompt(self):
        self.check_stop()
        message = "平台要求人工验证，请在专用浏览器处理后点击右下角「已完成验证，继续采集」。"
        self.waiting(True, message)
        await self.page.bring_to_front()
        # The button is installed again after navigation. It never solves or
        # dismisses a challenge; only an explicit user click resumes requests.
        with suppress(Exception):
            await self.page.evaluate("() => { window.__douyinCommentsResume = false; }")
        while True:
            self.check_stop()
            try:
                resumed = await self.page.evaluate("""() => {
                    if (window.__douyinCommentsResume) return true;
                    if (!document.getElementById('douyin-comments-resume')) {
                        const panel = document.createElement('div');
                        panel.id = 'douyin-comments-resume';
                        panel.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:2147483647;background:#fff;color:#172126;padding:18px;border:2px solid #1b7767;border-radius:12px;box-shadow:0 4px 24px #0003;font:14px sans-serif;max-width:320px';
                        const text = document.createElement('p');
                        text.textContent = '采集已暂停。请先完成页面中的登录或验证码，再点击下方按钮。';
                        const button = document.createElement('button');
                        button.textContent = '已完成验证，继续采集';
                        button.style.cssText = 'padding:10px 16px;border:0;border-radius:6px;background:#1b7767;color:white;cursor:pointer';
                        button.onclick = () => { window.__douyinCommentsResume = true; panel.remove(); };
                        panel.append(text, button);
                        document.body.append(panel);
                    }
                    return false;
                }""")
                if resumed:
                    await self.wait_for_login()
                    self.waiting(False, None)
                    self.policy.resume()
                    return
            except (StopRequested, PageFailure):
                raise
            except Exception:
                pass  # Allow the user to navigate while completing the challenge.
            await self.sleep(0.5)

    async def invoke(self, method: str, *args):
        self.check_stop()
        await self.policy.wait_ready()
        await self.ensure_session()
        try:
            return await getattr(self.client, method)(*args)
        except ManualActionRequired:
            raise
        except (StopRequested, PageFailure):
            raise
        except Exception as exc:
            error = str(exc).lower()
            if any(marker in error for marker in ("captcha", "verification", "验证码")):
                self.require_manual(reason='接口明确要求人工验证')
                raise ManualActionRequired("平台要求登录或人工验证，请在专用浏览器处理。") from None
            self.policy.failure()
            raise PageFailure("平台请求失败，请检查网络或稍后继续。") from None

    async def http_request(self, method, url, *, raw=False, scheduled=False, **kwargs):
        """One HTTP attempt; the engine task extension owns bounded retries."""
        if not scheduled:
            await self.policy.wait_turn()
        self.check_stop()
        generation = self.policy.generation
        began = time.monotonic()
        if (self.max_requests and self.metrics['http_requests'] >= self.max_requests) or (self.deadline and time.monotonic() >= self.deadline):
            raise ControlledStop('已达到实测请求或时间上限，断点已保存。')
        self.metrics["http_requests"] += 1
        # Use a fixed vocabulary; never log URLs, account IDs or query strings.
        category = "short_url" if raw else {
            "/aweme/v1/web/aweme/detail/": "video_detail",
            "/aweme/v1/web/comment/list/": "comments",
            "/aweme/v1/web/comment/list/reply/": "replies",
            "/aweme/v1/web/user/profile/other/": "author_profile",
            "/aweme/v1/web/discover/search/": "user_search",
            "/aweme/v1/web/aweme/favorite/": "likes",
            "/aweme/v1/web/familiar/recommend/feed/": "recommendations",
        }.get(urlsplit(url).path, "other")
        categories = self.metrics["http_request_categories"]
        categories[category] = categories.get(category, 0) + 1
        self._http_active += 1
        self.metrics["http_peak_active"] = max(self.metrics["http_peak_active"], self._http_active)
        try:
            response = await self.interruptible(self.http_client.request(
                method, url, timeout=kwargs.pop("timeout", getattr(self.client, "timeout", 25)), **kwargs))
        except (StopRequested, asyncio.CancelledError):
            raise
        except Exception:
            self.policy.failure()
            raise PageFailure("平台请求失败，请检查网络或稍后继续。") from None
        finally:
            self._http_active -= 1
            self.metrics["http_seconds"] += time.monotonic() - began
        if response.status_code == 429:
            self.policy.failure(rate_limited=True, retry_after=response.headers.get("Retry-After"))
            raise PageFailure("平台请求频率受限，已自动降速并等待后重试。")
        if response.status_code == 401:
            self.require_manual(challenge=False)
            raise ManualActionRequired("登录已失效，请在专用浏览器重新登录。")
        if response.status_code == 403:
            # Only structured, explicit challenge evidence counts as a captcha.
            try:
                validate_response(response.json())
            except ManualActionRequired:
                self.require_manual(reason='HTTP 403 明确验证提示')
                raise
            except (ValueError, TypeError, PageFailure):
                pass
            self.policy.failure()
            raise PageFailure("平台拒绝访问（HTTP 403），原因尚未明确；已降速并保留断点。")
        if response.status_code >= 500:
            self.policy.failure()
            raise PageFailure("平台服务暂时不可用，已自动降速。")
        if response.status_code >= 400:
            self.policy.failure(temporary=False)
            raise PageFailure(f"平台请求未成功（HTTP {response.status_code}），已保留断点。")
        if raw:
            self.policy.success(generation=generation)
            return response
        if response.text in ("", "blocked"):
            self.policy.failure()
            raise PageFailure("平台返回空数据或访问拒绝，尚无明确验证提示；已降速并保留断点。")
        try:
            data = response.json()
        except (ValueError, TypeError):
            self.policy.failure()
            raise PageFailure("平台返回了非预期数据，已保留断点。") from None
        try:
            validate_response(data)
        except ManualActionRequired:
            reason = '搜索验证提示' if data.get('search_nil_info') else '响应包含验证提示'
            self.require_manual(reason=reason)
            raise
        except PageFailure:
            self.policy.failure()
            # List-specific privacy responses must reach the collector intact.
        else:
            self.policy.success(generation=generation)
        return data

    async def invoke_with_referer(self, referer, method, *args):
        # The public header remains useful for browser/debug state; the actual
        # HTTP snapshot always takes its referer from this coroutine's context.
        self.client.headers["Referer"] = referer
        token = self._referer.set(referer)
        try:
            return await self.invoke(method, *args)
        finally:
            self._referer.reset(token)

    async def resolve_video_id(self, source_url: str) -> str:
        url = source_url
        for attempt in range(5):
            result = video_id_from_url(url)
            if result:
                self.video_url = f"https://www.douyin.com/video/{result}"
                return result
            target = await self.invoke("resolve_short_url", url)
            if not target:
                break
            url = urljoin(url, target)
        raise PageFailure("未能解析分享链接，请复制视频的完整链接后重试。")

    async def get_video_by_id(self, video_id: str):
        self.video_url = f"https://www.douyin.com/video/{video_id}"
        return await self.invoke_with_referer(self.video_url, "get_video_by_id", video_id)

    async def get_aweme_comments(self, video_id: str, cursor: int):
        return await self.invoke_with_referer(self.video_url, "get_aweme_comments", video_id, cursor)

    async def get_sub_comments(self, video_id: str, parent_id: str, cursor: int):
        return await self.invoke_with_referer(self.video_url, "get_sub_comments", video_id, parent_id, cursor)

    async def get_user_info(self, sec_uid: str):
        return await self.invoke_with_referer(f"https://www.douyin.com/user/{quote(sec_uid, safe='')}", "get_user_info", sec_uid)

    async def search_users(self, account: str, offset: int = 0, search_id: str = ""):
        # User discovery is a distinct endpoint; the general/video endpoint does
        # not become a user search merely by changing its channel parameter.
        search_url = f"https://www.douyin.com/search/{quote(account, safe='')}?type=user"
        if offset == 0 and self.page.url != search_url:
            # Put manual verification on the actual search page, so the user can
            # see/complete the challenge when the API reports search_nil_info.
            async with self._page_lock:
                await self.interruptible(self.page.goto(search_url, wait_until="domcontentloaded", timeout=60000))
        params = {"keyword": account, "search_channel": "aweme_user_web", "search_source": "switch_tab",
                  "offset": offset, "count": 20, "search_id": search_id, "is_filter_search": 1,
                  "query_correct_type": 1, "need_filter_settings": 1, "list_type": "single",
                  "search_filter_value": '{"douyin_user_fans":[""],"douyin_user_type":[""]}'}
        return await self.invoke_with_referer(search_url, "get", "/aweme/v1/web/discover/search/", params)

    async def get_user_likes(self, sec_uid: str, max_cursor: int = 0, count: int = 18):
        referer = f"https://www.douyin.com/user/{quote(sec_uid, safe='')}?showTab=like"
        params = {"sec_user_id": sec_uid, "max_cursor": max_cursor, "count": max(1, min(count, 18)),
                  "publish_video_strategy_type": 2, "locate_query": "false"}
        return await self.invoke_with_referer(referer, "get", "/aweme/v1/web/aweme/favorite/", params)

    async def get_user_recommendations(self, sec_uid: str, max_cursor: int = 0, count: int = 18):
        referer = f"https://www.douyin.com/user/{quote(sec_uid, safe='')}"
        params = {"sec_user_id": sec_uid, "max_cursor": max_cursor, "min_cursor": 0,
                  "count": max(1, min(count, 18)), "from": 1, "whale_cut_token": ""}
        return await self.invoke_with_referer(referer, "get", "/aweme/v1/web/familiar/recommend/feed/", params)
