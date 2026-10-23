"""The API process owns Chrome; collection workers only attach over CDP."""
from pathlib import Path
import socket
import time
import atexit
import json

from backend.upstream import module
from backend.crawler import PageFailure


class DedicatedBrowser:
    def __init__(self, data_dir):
        self.profile = Path(data_dir).resolve() / 'chrome-profile'
        self.record = Path(data_dir).resolve() / 'browser-endpoint.json'
        self.launcher = None
        self.endpoint = None
        atexit.register(self.close)

    @staticmethod
    def chrome_path():
        launcher = module('tools.browser_launcher').BrowserLauncher()
        candidates = [Path(p) for p in launcher.detect_browser_paths() if Path(p).name.lower() in ('chrome.exe', 'google-chrome', 'chrome') or 'Google Chrome.app' in p]
        if not candidates:
            raise PageFailure('未找到 Google Chrome，请安装 Chrome 后重新打开登录窗口。')
        return candidates[0]

    def ensure(self):
        if self.is_alive():
            return self.endpoint
        if self.launcher is None and self.record.exists():
            # A previous service may have crashed while Chrome survived. Do not
            # adopt or terminate a process based on a saved PID or port alone.
            try:
                port = json.loads(self.record.read_text(encoding='utf-8'))['port']
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError()
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        raise PageFailure('上次服务的专用 Chrome 仍在运行，请关闭该专用窗口后重试；不会结束其他浏览器。')
                except OSError:
                    pass
            except (OSError, ValueError, KeyError, TypeError):
                raise PageFailure('专用浏览器归属记录无法读取，请检查本地 browser-endpoint.json；尚未启动浏览器。') from None
        self.launcher = module('tools.browser_launcher').BrowserLauncher()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        try:
            # Save the port before launch so an abrupt service exit cannot
            # leave an unrecorded Chrome between Popen and the next write.
            self.record.write_text(json.dumps({'port': port}), encoding='utf-8')
            self.launcher.launch_dedicated(self.chrome_path(), self.profile, port)
        except BaseException:
            self.close()
            raise
        for _ in range(100):
            if self.launcher.browser_process.poll() is not None:
                break
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    self.endpoint = f'http://127.0.0.1:{port}'
                    return self.endpoint
            except OSError:
                time.sleep(.1)
        self.close()
        raise PageFailure('专用 Chrome 未能启动，已保留断点；请关闭该工具遗留的专用窗口后重试。')

    def is_alive(self):
        return bool(self.launcher and self.launcher.browser_process
                    and self.launcher.browser_process.poll() is None)

    def close(self):
        if self.is_alive():
            process = self.launcher.browser_process
            self.launcher.cleanup()
            if process.poll() is None:
                # Upstream cleanup clears its handle even on failure. Retain
                # our ownership evidence and block a duplicate launch.
                self.launcher.browser_process = process
                raise PageFailure('专用 Chrome 尚未退出，请关闭该专用窗口后重试。')
        if self.launcher:
            self.record.unlink(missing_ok=True)
        self.launcher, self.endpoint = None, None
