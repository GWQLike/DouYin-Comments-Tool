from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.browser_session import DedicatedBrowser
from backend.douyin_adapter import DouyinAdapter
from backend.upstream import module
from backend.db import Database
from backend.app import WorkerManager, create_app
from backend.stable_policy import StableRequestPolicy
from fastapi.testclient import TestClient


async def test_cdp_attaches_same_context_and_never_closes_external_browser():
    context = SimpleNamespace(close=AsyncMock())
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    connect = AsyncMock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    cls = module('tools.cdp_browser').CDPBrowserManager
    for _ in range(2):
        manager = cls(connection_url='http://127.0.0.1:9222')
        assert await manager.launch_and_connect(playwright) is context
        await manager.cleanup(force=True)
    assert connect.await_count == 2
    context.close.assert_not_awaited()
    browser.close.assert_not_awaited()


@pytest.mark.parametrize('url', ['http://192.168.0.1:9222', 'https://127.0.0.1:9222', 'http://127.0.0.1'])
async def test_cdp_rejects_non_loopback_endpoints(url):
    with pytest.raises(ValueError):
        await module('tools.cdp_browser').CDPBrowserManager(connection_url=url).launch_and_connect(None)


def test_browser_owner_reuses_until_explicit_close(tmp_path, monkeypatch):
    launcher_type = module('tools.browser_launcher').BrowserLauncher
    launcher = Mock()
    launcher.browser_process.poll.return_value = None
    monkeypatch.setattr(module('tools.browser_launcher'), 'BrowserLauncher', lambda: launcher)
    monkeypatch.setattr(DedicatedBrowser, 'chrome_path', staticmethod(lambda: tmp_path / 'chrome.exe'))
    connection = Mock(__enter__=Mock(), __exit__=Mock())
    monkeypatch.setattr('backend.browser_session.socket.create_connection', lambda *args, **kwargs: connection)
    owner = DedicatedBrowser(tmp_path)
    first = owner.ensure()
    assert first == owner.ensure() and first.startswith('http://127.0.0.1:')
    launcher.launch_dedicated.assert_called_once()
    assert launcher.launch_dedicated.call_args.args[1] == tmp_path / 'chrome-profile'
    launcher.cleanup.assert_not_called()
    owner.close()
    launcher.cleanup.assert_called_once()


async def test_worker_disconnect_keeps_chrome_profile_open(tmp_path):
    adapter = DouyinAdapter(tmp_path / 'profile', lambda: False, lambda *args: None)
    adapter.context = SimpleNamespace(close=AsyncMock())
    adapter.playwright_manager = SimpleNamespace(__aexit__=AsyncMock())
    await adapter.__aexit__()
    adapter.context.close.assert_not_awaited()
    adapter.playwright_manager.__aexit__.assert_awaited_once()


def test_api_control_is_additive_and_start_blocked_during_cooldown(tmp_path):
    p = StableRequestPolicy(tmp_path / 'collection-control.json')
    p.failure(rate_limited=True)
    from backend.stable_policy import ProtectionPause
    with pytest.raises(ProtectionPause):
        p.failure(rate_limited=True)
    app = create_app(tmp_path)
    app.state.manager.browser.ensure = Mock(side_effect=AssertionError('browser should not start'))
    task = app.state.db.create_task('https://www.douyin.com/video/123', '123')
    with TestClient(app) as client:
        session = client.get('/api/session').json()
        assert session['control_status'] == 'paused' and session['cooldown_until'] > 0
        assert 'status' in session and 'error' in session
        assert client.post(f"/api/tasks/{task['id']}/start").status_code == 409
    app.state.manager.browser.ensure.assert_not_called()
