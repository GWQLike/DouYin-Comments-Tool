"""Installation check; imports only the Douyin engine, opens no browser."""
from backend.upstream import module, ENGINE_VERSION
from backend.browser_session import DedicatedBrowser


def main():
    engine = module('media_platform.douyin.core').DouYinCrawler
    client = module('media_platform.douyin.client').DouYinClient
    assert callable(engine.start) and callable(client.prepare_request)
    assert DedicatedBrowser.chrome_path().is_file()
    print('MediaCrawler 内核与专用 Chrome 检查通过：' + ENGINE_VERSION)


if __name__ == '__main__':
    main()
