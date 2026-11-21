"""Run the production CLI with the historical collectors unavailable."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.db import Database
from support.engine import create, exported


ROOT = Path(__file__).resolve().parents[1]

WORKER = '''
import importlib.abc
from pathlib import Path
import runpy
import sys
from unittest.mock import patch


def is_legacy(name):
    name = name.removeprefix('tests.')
    return name.split('.')[0] == 'baselines' or name in {
        'backend.crawler', 'backend.likes', 'backend.recommendations',
        'support.legacy_clients', 'test_crawler', 'test_likes_collector',
        'test_recommendations_collector',
    }


blocked = []


class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if is_legacy(fullname):
            blocked.append(fullname)
            raise ImportError('Historical collector unavailable: ' + fullname)


sys.meta_path.insert(0, BlockLegacy())
assert not any(is_legacy(name) for name in sys.modules)
sys.path.insert(0, str(Path.cwd() / 'tests'))

from backend.upstream import ENGINE_VERSION, module, verify_patch
from support.engine import Fixture, SimulatedTransport

kind, verify, *cli_args = sys.argv[1:]
fixture = Fixture(kind)
if kind != 'comments':
    fixture.pages[100]['aweme_list'][1]['author'].update(uid='other_uid', sec_uid='other_sec')

assert ENGINE_VERSION == '5d547f45862ca2d4b404e5a51775879d579b29d4+workbench.2'
verify_patch()
vendor = Path.cwd() / 'vendor' / 'MediaCrawler' / 'media_platform' / 'douyin'
engine, client, paginator = (module('media_platform.douyin.' + name)
                            for name in ('core', 'client', 'pagination'))
for name, imported in [('core', engine), ('client', client), ('pagination', paginator)]:
    assert Path(imported.__file__).resolve() == vendor / (name + '.py')
paging_method = 'get_aweme_all_comments' if kind == 'comments' else 'get_activity_all_videos'
calls = []
transports = []


def track(name, original):
    async def invoke(*args, **kwargs):
        calls.append(name)
        return await original(*args, **kwargs)
    return invoke


def open_simulation(*args):
    transport = SimulatedTransport(*args, fixture=fixture)
    transports.append(transport)
    return transport


sys.argv = ['backend.worker', *cli_args]
with (
    patch('backend.douyin_adapter.DouyinAdapter', open_simulation),
    patch('media_platform.douyin.help.get_a_bogus_from_js', return_value='fixture-signature'),
    patch.object(engine, 'async_playwright', side_effect=AssertionError('Unexpected browser launch')) as browser,
    patch('httpx.AsyncHTTPTransport.handle_async_request',
          side_effect=AssertionError('Unexpected external HTTP request')) as network,
    patch.object(engine.DouYinCrawler, 'start', track('start', engine.DouYinCrawler.start)),
    patch.object(client.DouYinClient, paging_method, track('client', getattr(client.DouYinClient, paging_method))),
    patch.object(paginator, 'collect_pages', track('pages', paginator.collect_pages)),
):
    try:
        runpy.run_module('backend.worker', run_name='__main__', alter_sys=True)
    except SystemExit as exc:
        assert exc.code == 0, exc.code
    else:
        raise AssertionError('CLI did not exit')

browser.assert_not_called()
network.assert_not_called()
assert not blocked, blocked
assert not any(is_legacy(name) for name in sys.modules)
assert calls == ['start'] + ['client', 'pages'] * (2 if kind == 'comments' else 1), calls
transport, = transports
assert transport.closed and transport.http_client.is_closed
targets = [] if kind == 'comments' else ['target_sec']
profiles = [request.url.params['sec_user_id'] for request in fixture.requests
            if request.url.path.endswith('/profile/other/')]
assert profiles == targets + (['author_sec'] if verify == 'True' else []), profiles
'''


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
@pytest.mark.parametrize('verify', [False, True], ids=['without-verification', 'with-verification'])
def test_worker_collects_and_exports_with_legacy_imports_blocked(tmp_path, kind, verify):
    db = Database(tmp_path / 'worker.sqlite3')
    tid = create(db, kind, list_order='activity', verify_authors=verify)['id']
    result = subprocess.run(
        [sys.executable, '-c', WORKER, kind, str(verify), '--db', db.path,
         '--profile', str(tmp_path / 'profile'), '--task', tid],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=45,
        env={**os.environ, 'PYTHONUTF8': '1'},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    task = db.get_task(tid)
    assert task['status'] == 'completed' and task['collection_complete']
    assert task['verify_authors'] == task['verification_complete'] == verify
    assert task['unknown_authors'] == (0 if verify else 1)
    number = 'author.one' if verify else '未提供'
    if kind == 'comments':
        assert (task['main_count'], task['reply_count']) == (2, 2)
        assert [(row['comment_id'], row['parent_id']) for row in db.read_comments(tid)] == [
            ('m2', ''), ('r2', 'm1'), ('r1', 'm1'), ('m1', ''),
        ]
        checkpoint = {'cursor': 40, 'done': True, 'restarts': 0}
        assert db.get_checkpoint(tid) == db.get_checkpoint(tid, 'm1') == checkpoint
        expected = (
            '用户名：测试用户\r\n抖音号：public.one\r\n评论内容：謝謝分享\r\n'
            '时间：2024-07-03 17:50:00\r\nIP 属地：广东\r\n\r\n'
            '用户名：测试用户\r\n抖音号：public.one\r\n评论内容：测试评论\r\n'
            '时间：2024-07-03 17:46:40\r\nIP 属地：广东\r\n\r\n'
            '用户名：测试用户\r\n抖音号：public.one\r\n评论内容：楼中楼\r\n'
            '时间：2024-07-03 17:46:40\r\nIP 属地：广东\r\n\r\n'
            f'用户名：原始作者\r\n抖音号：{number}\r\n评论内容：测试评论\r\n'
            '时间：2024-07-03 17:46:40\r\nIP 属地：广东\r\n'
        )
    else:
        assert task['video_count'] == 2 and task['visibility'] == 'public'
        assert [(row['video_id'], row['action_at']) for row in db.read_liked_videos(tid)] == (
            [('1', 1720001000), ('2', 1720000900)] if kind == 'recommendations'
            else [('1', None), ('2', None)]
        )
        assert db.get_checkpoint(tid, kind) == {'cursor': 200, 'done': True, 'restarts': 0}
        expected = (
            f'视频标题：测试视频标题\\n原始文本\r\n作者名称：原始作者\r\n作者抖音号：{number}\r\n'
            '发布时间：2024-07-03 17:46:40\r\n视频链接：https://www.douyin.com/video/1\r\n\r\n'
            '视频标题：测试视频标题\\n原始文本\r\n作者名称：视频作者\r\n作者抖音号：author.one\r\n'
            '发布时间：2024-07-03 17:46:40\r\n视频链接：https://www.douyin.com/video/2\r\n'
        )
    assert exported(db, tid, kind) == expected.encode('utf-8-sig')
