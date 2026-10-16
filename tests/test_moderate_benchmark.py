"""Virtual network clock; real MediaCrawler client, pagination, SQLite and TXT.

The baseline uses the previous fixed 2–4 second pacing behavior. Both sides get
identical latency, seeded random intervals, data and author verification. Reported
seconds are simulated network/scheduling time, not measured Douyin performance.
"""
from collections import Counter
import json
import random

import httpx
import pytest

from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.stable_policy import StableRequestPolicy
from test_engine_integration import SimulatedTransport, args_for, create, exported, fixture_signing
from test_crawler import comment, page
from test_likes_collector import profile, video, likes, users


class FixedPolicy(StableRequestPolicy):
    NORMAL_RANGES = ((2, 4), (2, 4), (2, 4))


class LargeFixture:
    def __init__(self, kind, size):
        self.kind, self.size, self.calls = kind, size, Counter()
        self.replies = {}
        self.main = []
        ordinal = 0
        if kind == 'comments':
            roots = size // 2
            for i in range(roots):
                count = 21 if i == 0 else 1 if i <= size - roots - 21 else 0
                self.main.append(self.comment(ordinal, replies=count))
                parent = str(ordinal)
                ordinal += 1
                self.replies[parent] = [self.comment(ordinal + j) for j in range(count)]
                ordinal += count
            assert ordinal == size

    def author(self, i):
        owner = i % 1000
        return {'uid': f'u{owner}', 'sec_uid': f's{owner}', 'nickname': f'作者{owner}'}

    def comment(self, i, replies=0):
        return comment(str(i), user=self.author(i), text=f'謝謝分享 {i}', stamp=1720000000 + i, replies=replies)

    def __call__(self, request):
        path, params = request.url.path, request.url.params
        self.calls[path] += 1
        if path.endswith('/aweme/detail/'):
            payload = {'status_code': 0, 'aweme_detail': {'desc': '模拟数据'}}
        elif path.endswith('/comment/list/') or path.endswith('/comment/list/reply/'):
            rows = self.replies[params['comment_id']] if 'comment_id' in params else self.main
            cursor = int(params['cursor'])
            payload = page(rows[cursor:cursor+20], cursor+20, int(cursor+20 < len(rows)))
        elif path.endswith('/profile/other/'):
            sec = params['sec_user_id']
            if sec == 'target_sec':
                payload = profile(show_favorite_list=True)
            else:
                owner = int(sec[1:])
                payload = profile(f'public.{owner}', sec, f'u{owner}')
        elif path.endswith('/discover/search/'):
            payload = users([profile()['user']])
        elif path.endswith('/recommend/feed/'):
            cursor, count = int(params['max_cursor']), int(params['count'])
            rows = []
            for i in range(cursor, min(cursor+count, self.size)):
                item = video(str(i), author=self.author(i))
                item['friend_recommend_info'] = {'label_user_list': [{'user': {'sec_uid': 'target_sec'}, 'recommend_time': 1720001000 - i}]}
                rows.append(item)
            payload = likes(rows, cursor+count, int(cursor+count < self.size))
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=payload)


class TimedTransport(SimulatedTransport):
    def __init__(self, *args, fixture, baseline):
        super().__init__(*args, fixture=fixture)
        generator = random.Random(405)
        policy_type = FixedPolicy if baseline else StableRequestPolicy
        self.policy = policy_type(self.profile.parent / 'control.json', wall_clock=self.clock,
            clock=self.clock, sleep=self.clock.sleep, uniform=generator.uniform, check_stop=self.check_stop)

    async def http_request(self, *args, **kwargs):
        result = await super().http_request(*args, **kwargs)
        await self.clock.sleep(.3)
        return result


async def scenario(directory, kind, size, baseline, enabled):
    db = Database(directory / 'data.db')
    task = create(db, kind, verify_authors=enabled, max_videos=size if kind != 'comments' else 0)
    fixture = LargeFixture(kind, size)
    args = args_for(directory, task)
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: TimedTransport(*args, fixture=fixture, baseline=baseline))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    state = db.get_task(task['id'])
    assert state['collection_complete'] and state['status'] == 'completed'
    assert state['main_count'] + state['reply_count'] + state['video_count'] == size
    assert runtime.transport.metrics['http_peak_active'] == 1
    result = {'seconds': round(runtime.transport.clock.now - 10000, 3),
              'requests': sum(fixture.calls.values()),
              'profiles': sum(count for path, count in fixture.calls.items() if path.endswith('/profile/other/'))}
    return exported(db, task['id'], kind), result, (db, task, args)


@pytest.mark.parametrize('kind,size', [('comments', 500), ('comments', 1000), ('comments', 5000), ('recommendations', 1000)])
async def test_moderate_matches_fixed_pacing_and_saves_at_least_thirty_percent(tmp_path, kind, size):
    before, fixed, _ = await scenario(tmp_path / 'fixed', kind, size, True, True)
    after, moderate, _ = await scenario(tmp_path / 'moderate', kind, size, False, True)
    assert before == after
    assert fixed['requests'] == moderate['requests'] >= 80
    reduction = 1 - moderate['seconds'] / fixed['seconds']
    assert reduction >= .3
    report = {'kind': kind, 'size': size, 'fixed': fixed, 'moderate': moderate,
              'reduction': reduction, 'identical_txt_bytes': True, 'time_basis': 'virtual network and scheduler clock'}
    if kind == 'recommendations':
        partial, off, (db, task, args) = await scenario(tmp_path / 'off', kind, size, False, False)
        assert off['profiles'] == 1  # Only the target account, not the 1000 result authors.
        assert '作者抖音号：未提供' in partial.decode('utf-8-sig')
        fixture = LargeFixture(kind, size)
        args.verify_only = True
        runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
            transport_factory=lambda *args: TimedTransport(*args, fixture=fixture, baseline=False))
        await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
        assert exported(db, task['id'], kind) == before
        assert sum(fixture.calls.values()) == 1000
        assert all(path.endswith('/profile/other/') for path in fixture.calls)
        report['verification_off'] = off
        report['saved_author_requests'] = moderate['requests'] - off['requests']
    (tmp_path / 'moderate-benchmark.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('MODERATE_BENCHMARK ' + json.dumps(report, ensure_ascii=False))
