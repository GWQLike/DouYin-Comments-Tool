"""The API process owns Chrome; collection workers only attach over CDP."""
from pathlib import Path
import socket
import time
import atexit

from backend.upstream import module
from backend.crawler import PageFailure


class DedicatedBrowser:
    def __init__(self, data_dir):
        self.profile = Path(data_dir).resolve() / 'chrome-profile'
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
        if self.launcher and self.launcher.browser_process.poll() is None:
            return self.endpoint
        self.launcher = module('tools.browser_launcher').BrowserLauncher()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.launcher.launch_dedicated(self.chrome_path(), self.profile, port)
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

    def close(self):
        if self.launcher and self.launcher.browser_process and self.launcher.browser_process.poll() is None:
            self.launcher.cleanup()
        self.launcher, self.endpoint = None, None
