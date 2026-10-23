"""Identity evidence is checked per result, never per representative group."""
import asyncio
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from backend import migrations
from backend.crawler import normalize_comment
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.likes import normalize_liked_video
from backend.search import Filters, select_comments
from backend.engine_tasks import CommentTask, LikesTask, RecommendationsTask
from test_crawler import comment
from test_engine_integration import SimulatedTransport, args_for, fixture_signing
from test_likes_collector import video
from test_migrations import backups, snapshot, assert_existing_data_preserved
from test_comment_preservation import historical_v2


KINDS = ['comments', 'likes', 'recommendations']


def seed(tmp_path, kind, users, *, complete=False):
    db = Database(tmp_path / 'results.db')
    task = db.create_task('fixture', 'video', kind=kind, max_videos=len(users))
    tid = task['id']
    if kind == 'comments':
        db.save_page(tid, '', [normalize_comment(comment(str(i+1), user=user), 'video')
                               for i, user in enumerate(users)], 40, complete)
    else:
        db.save_likes_page(tid, [normalize_liked_video(video(str(i+1), author=user))
                                for i, user in enumerate(users)], 40, complete, checkpoint_key=kind)
    db.update_task(tid, status='completed' if complete else 'partial', collection_complete=complete,
                   error=None if complete else '保留的采集错误')
    return db, tid


async def verify(tmp_path, db, tid, responses, **limits):
    calls = []
    def fixture(request):
        assert request.url.path.endswith('/profile/other/')
        sec = request.url.params['sec_user_id']
        calls.append(sec)
        result = responses[sec]
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
    args = args_for(tmp_path, db.get_task(tid))
    args.verify_only = True
    for key, value in limits.items():
        setattr(args, key, value)
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    return calls


def public_rows(db, tid, kind):
    if kind == 'comments':
        return [row['douyin_id'] for row in select_comments(db.read_comments(tid), Filters())]
    return [row['author_douyin_id'] for row in db.read_liked_videos(tid)]


@pytest.mark.parametrize('kind', KINDS)
async def test_same_author_key_different_profiles_are_verified_independently(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'same', 'sec_uid': 'a'}, {'uid': 'same', 'sec_uid': 'b'}])
    assert db.get_task(tid)['unknown_authors'] == 2
    checkpoint = db.get_checkpoint(tid, '' if kind == 'comments' else kind)
    calls = await verify(tmp_path, db, tid, {
        'a': {'user': {'uid': 'same', 'sec_uid': 'a', 'unique_id': 'public.a'}},
        'b': {'user': {'uid': 'same', 'sec_uid': 'b', 'unique_id': 'public.b'}}})
    assert calls == ['a', 'b']
    assert public_rows(db, tid, kind) == ['public.b', 'public.a']
    state = db.get_task(tid)
    assert state['verification_complete'] and not state['collection_complete']
    assert state['error'] == '保留的采集错误' and state['verify_authors'] is False
    assert db.get_checkpoint(tid, '' if kind == 'comments' else kind) == checkpoint
    assert all(r['status'] == 'confirmed' and r['attempts'] == 1 and r['last_attempt_at']
               for r in db.verification_records(tid))
    assert await verify(tmp_path, db, tid, {}) == []


@pytest.mark.parametrize('kind', KINDS)
async def test_shared_response_never_shares_identity_conclusion(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'right', 'sec_uid': 'shared'},
                                   {'uid': 'wrong', 'sec_uid': 'shared'}, {'sec_uid': 'shared'}])
    calls = await verify(tmp_path, db, tid, {'shared': {'user': {
        'uid': 'right', 'sec_uid': 'shared', 'unique_id': 'right.public'}}})
    assert calls == ['shared']
    records = db.verification_records(tid)
    assert [r['status'] for r in records] == ['confirmed', 'conflict', 'confirmed']
    assert records[1]['failure_kind'] == 'identity_conflict'
    assert db.get_task(tid)['unknown_authors'] == 1
    assert not db.get_task(tid)['verification_complete']
    assert public_rows(db, tid, kind) == ['right.public', '', 'right.public']
    assert [r['attempts'] for r in records] == [1, 1, 1]


@pytest.mark.parametrize('kind', KINDS)
async def test_missing_conditions_and_unprovided_identity_are_distinct(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'no-query'}, {'uid': 'u2', 'sec_uid': 's2'},
                                   {'uid': 'u3', 'sec_uid': 's3'}])
    calls = await verify(tmp_path, db, tid, {'s2': {'user': {}}, 's3': {'user': {'uid': 'u3', 'sec_uid': 's3'}}})
    assert calls == ['s2', 's3']
    records = db.verification_records(tid)
    assert [(r['status'], r['failure_kind'], r['attempts']) for r in records] == [
        ('no_query', 'missing_sec_uid', 0), ('not_provided', 'identity_missing', 1),
        ('not_provided', 'public_number_missing', 1)]
    assert records[0]['last_attempt_at'] is None
    assert all(r['last_attempt_at'] for r in records[1:])
    assert public_rows(db, tid, kind) == ['', '', '']
    assert db.get_task(tid)['unknown_authors'] == 3


@pytest.mark.parametrize('kind', KINDS)
async def test_partial_failure_retry_preserves_success_and_collection_error(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'good', 'sec_uid': 'a'}, {'uid': 'later', 'sec_uid': 'b'}])
    checkpoint = db.get_checkpoint(tid, '' if kind == 'comments' else kind)
    await verify(tmp_path, db, tid, {'a': {'user': {'uid': 'good', 'sec_uid': 'a', 'unique_id': 'good.public'}},
                                    'b': httpx.Response(403, json={})})
    records = db.verification_records(tid)
    assert records[0]['status'] == 'confirmed'
    assert (records[1]['status'], records[1]['failure_kind'], records[1]['attempts']) == ('request_failed', 'permanent', 1)
    db = Database(db.path)
    calls = await verify(tmp_path, db, tid, {'b': {'user': {'uid': 'later', 'sec_uid': 'b', 'unique_id': 'later.public'}}})
    assert calls == ['b']
    assert [r['attempts'] for r in db.verification_records(tid)] == [1, 2]
    assert db.get_task(tid)['verification_complete']
    assert db.get_task(tid)['error'] == '保留的采集错误'
    assert db.get_checkpoint(tid, '' if kind == 'comments' else kind) == checkpoint


@pytest.mark.parametrize('kind', KINDS)
async def test_all_requests_fail_without_losing_completed_content(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'u', 'sec_uid': 's'}], complete=True)
    assert await verify(tmp_path, db, tid, {'s': httpx.Response(503)}) == ['s', 's', 's']
    record, = db.verification_records(tid)
    assert (record['status'], record['failure_kind'], record['attempts']) == ('request_failed', 'temporary', 1)
    state = db.get_task(tid)
    assert state['collection_complete'] and not state['verification_complete']


@pytest.mark.parametrize('kind', KINDS)
async def test_budget_pause_preserves_original_collection_error_and_resume(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'u1', 'sec_uid': 's1'}, {'uid': 'u2', 'sec_uid': 's2'}])
    responses = {f's{i}': {'user': {'uid': f'u{i}', 'sec_uid': f's{i}', 'unique_id': f'public.{i}'}} for i in (1, 2)}
    assert await verify(tmp_path, db, tid, responses, max_requests=1) == ['s1']
    state = db.get_task(tid)
    assert state['status'] == 'paused' and state['error'] == '保留的采集错误'
    assert db.verification_records(tid)[0]['status'] == 'confirmed'
    assert await verify(tmp_path, db, tid, responses) == ['s2']
    assert db.get_task(tid)['verification_complete']


@pytest.mark.parametrize('kind', KINDS)
def test_empty_results_need_no_verification(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [])
    assert db.get_task(tid)['verification_complete']
    assert not db.get_task(tid)['collection_complete']
    assert db.list_tasks()[0]['verification_complete']


def test_late_profile_cannot_update_a_replaced_video_identity(tmp_path):
    db, tid = seed(tmp_path, 'likes', [{'uid': 'old', 'sec_uid': 'old-sec'}])
    record, = db.verification_records(tid)
    db.begin_verification(tid, record)
    db.save_likes_page(tid, [normalize_liked_video(video('1', author={'uid': 'new', 'sec_uid': 'new-sec'}))], 60, False)
    db.finish_verification(tid, record, 'confirmed', '', {'uid': 'old', 'sec_uid': 'old-sec', 'douyin_id': 'wrong', 'nickname': '旧作者'})
    assert public_rows(db, tid, 'likes') == ['']
    assert db.verification_records(tid)[0]['status'] == 'pending'


@pytest.mark.parametrize('kind', KINDS)
async def test_cancelled_profile_keeps_error_and_pending_evidence(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'u', 'sec_uid': 's'}])
    entered = asyncio.Event()
    async def query(_):
        entered.set()
        await asyncio.Future()
    cls = {'comments': CommentTask, 'likes': LikesTask, 'recommendations': RecommendationsTask}[kind]
    task = cls(db, SimpleNamespace(get_user_info=query), tid)
    pending = asyncio.create_task(task.run(verify_only=True))
    await asyncio.wait_for(entered.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    record, = db.verification_records(tid)
    assert record['status'] == 'request_failed' and record['failure_kind'] == 'cancelled'
    assert record['attempts'] == 1 and record['last_attempt_at']
    assert db.get_task(tid)['error'] == '保留的采集错误'


@pytest.mark.parametrize('kind', KINDS)
async def test_first_human_verification_preserves_collection_error(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'u', 'sec_uid': 's'}])
    calls = await verify(tmp_path, db, tid, {'s': {'verify_check': True}}, stop_on_challenge=True)
    assert calls == ['s']
    assert db.get_task(tid)['status'] == 'paused'
    assert db.get_task(tid)['error'] == '保留的采集错误'
    assert db.verification_records(tid)[0]['status'] == 'request_failed'
    assert db.verification_records(tid)[0]['failure_kind'] == 'verification'


@pytest.mark.parametrize('kind', KINDS)
async def test_newly_verified_profile_remains_a_discovery_hint(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'u', 'sec_uid': 's'}])
    await verify(tmp_path, db, tid, {'s': {'user': {'uid': 'u', 'sec_uid': 's', 'unique_id': 'public.author'}}})
    assert db.cached_account_profiles('PUBLIC.AUTHOR') == [{'uid': 'u', 'sec_uid': 's'}]
    assert db.cached_account_profiles('unrelated.author') == []


@pytest.mark.parametrize('kind', KINDS)
async def test_stop_before_verification_starts_retains_collection_error(tmp_path, kind):
    db, tid = seed(tmp_path, kind, [{'uid': 'u', 'sec_uid': 's'}])
    db.update_task(tid, stop_requested=True)
    cls = {'comments': CommentTask, 'likes': LikesTask, 'recommendations': RecommendationsTask}[kind]
    await cls(db, SimpleNamespace(), tid).run(verify_only=True)
    assert db.get_task(tid)['status'] == 'paused'
    assert db.get_task(tid)['error'] == '保留的采集错误'
    assert db.verification_records(tid)[0]['attempts'] == 0


@pytest.mark.parametrize('verify_only', [False, True])
@pytest.mark.parametrize('stopping', [False, True])
async def test_worker_login_during_verification_preserves_collection_error(tmp_path, monkeypatch, verify_only, stopping):
    from backend.crawler import worker, StopRequested
    db, tid = seed(tmp_path, 'comments', [{'uid': 'u', 'sec_uid': 's'}])
    args = SimpleNamespace(db=db.path, profile=str(tmp_path / 'profile'), task=tid, verify_only=verify_only)
    async def run(db, args, stop, waiting):
        # Covers both automatic verification after collection and verify-only.
        db.update_task(tid, status='verifying')
        waiting(True, '重新登录')
        assert db.get_task(tid)['error'] == '保留的采集错误'
        waiting(False)
        assert db.get_task(tid)['status'] == 'verifying'
        assert db.get_task(tid)['error'] == '保留的采集错误'
        raise StopRequested() if stopping else RuntimeError('fixture browser exit')
    monkeypatch.setattr('backend.engine.run_engine', run)
    monkeypatch.setattr('backend.crawler.signal.signal', lambda *args: None)
    assert await worker(args) == (0 if stopping else 1)
    assert db.get_task(tid)['error'] == '保留的采集错误'
    assert db.get_checkpoint(tid)['cursor'] == 40


@pytest.mark.parametrize('launch_fails', [False, True])
def test_manager_keeps_collection_error_on_verification_start_and_exit(tmp_path, monkeypatch, launch_fails):
    from backend.app import WorkerManager
    from backend.runtime_control import ProcessLease
    from fastapi import HTTPException
    db, tid = seed(tmp_path, 'comments', [{'uid': 'u', 'sec_uid': 's'}])
    manager = WorkerManager(db, tmp_path)
    manager.browser.ensure = Mock(return_value='http://127.0.0.1:9222')
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr('backend.app.subprocess.Popen',
        Mock(side_effect=OSError('fixture spawn failure')) if launch_fails else Mock(return_value=process))
    with ProcessLease(tmp_path, 'service') as owner:
        owner.identify()
        manager.owner = owner
        if launch_fails:
            with pytest.raises(HTTPException) as error:
                manager.start(tid, verify=True)
            assert error.value.status_code == 500
        else:
            manager.start(tid, verify=True)
            assert db.get_task(tid)['error'] == '保留的采集错误'
            process.poll.return_value = 1
            assert not manager.check()
    assert db.get_task(tid)['error'] == '保留的采集错误'
    assert db.get_checkpoint(tid)['cursor'] == 40


def test_protection_pause_has_its_own_verification_failure_category(tmp_path):
    from backend.stable_policy import StableRequestPolicy, ProtectionPause
    from backend.verification import failure_kind
    policy = StableRequestPolicy(tmp_path / 'control.json')
    policy.failure(rate_limited=True)
    with pytest.raises(ProtectionPause) as error:
        policy.failure(rate_limited=True)
    assert failure_kind(error.value) == 'protection'


def test_count_and_queue_ignore_orphaned_verification_rows(tmp_path):
    db, tid = seed(tmp_path, 'comments', [{'uid': 'u', 'sec_uid': 's'}])
    with db.connection() as con:
        con.execute('DELETE FROM comments WHERE task_id=?', (tid,))
    assert db.verification_records(tid) == []
    assert db.get_task(tid)['verification_complete']
    assert db.list_tasks()[0]['unknown_authors'] == 0


def test_repeated_page_preserves_migrated_linked_identity(tmp_path):
    path = tmp_path / 'legacy.db'
    version_three(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute("UPDATE comments SET uid='',sec_uid='',douyin_id='' WHERE comment_id='c1'")
        con.commit()
    db = Database(path)
    before = db.verification_records('comments')[0]
    db.save_page('comments', '', [normalize_comment({'cid': 'c1'}, '100')], 40, False)
    after = db.verification_records('comments')[0]
    assert after == before
    assert db.read_comments('comments')[0]['public_number'] == 'fixture.author'


def version_three(path):
    historical_v2(path)
    with closing(sqlite3.connect(path)) as con:
        migrations.MIGRATIONS[2].apply(con)
        con.execute('PRAGMA user_version=3')
        con.commit()


def test_migration_preserves_old_data_and_classifies_per_result(tmp_path):
    path = tmp_path / 'legacy.db'
    version_three(path)
    before = snapshot(path)
    db = Database(path)
    backup, = backups(path)
    assert snapshot(backup) == before
    assert_existing_data_preserved(backup, path)
    records = db.verification_records('comments')
    assert [r['status'] for r in records] == ['confirmed', 'pending']
    assert all(r['attempts'] == 0 and r['last_attempt_at'] is None for r in records)
    assert db.get_checkpoint('comments')['cursor'] == 40
    Database(path)
    assert backups(path) == [backup]
    restored = tmp_path / 'restored.db'
    with closing(sqlite3.connect(backup)) as source, closing(sqlite3.connect(restored)) as target:
        source.backup(target)
    assert snapshot(restored) == before
    assert Database(restored).verification_records('comments') == records


def test_migration_does_not_trust_legacy_video_group_with_conflicting_ids(tmp_path):
    path = tmp_path / 'ambiguous.db'
    version_three(path)
    with closing(sqlite3.connect(path)) as con:
        con.execute('''INSERT INTO liked_videos(task_id,video_id,author_key,uid,sec_uid,author_douyin_id,video_url)
            VALUES('likes','v-conflict','fixture.uid','fixture.uid','other.sec','fixture.author',
                   'https://www.douyin.com/video/103')''')
        con.commit()
    before = snapshot(path)
    db = Database(path)
    assert snapshot(backups(path)[0]) == before
    records = db.verification_records('likes')
    assert len(records) == 2
    assert all(r['status'] == 'conflict' and r['source_conflict'] == 1 for r in records)
    assert db.get_task('likes')['unknown_authors'] == 2
    assert public_rows(db, 'likes', 'likes') == ['', '']


def test_version_four_failure_rolls_back_records_and_keeps_backup(tmp_path, monkeypatch):
    path = tmp_path / 'broken.db'
    version_three(path)
    before = snapshot(path)
    real = migrations.MIGRATIONS[3].apply
    def broken(con):
        real(con)
        raise RuntimeError('fixture failure after creating verification records')
    monkeypatch.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:3] + (migrations.Migration(4, broken),))
    with pytest.raises(migrations.DatabaseMigrationError):
        Database(path)
    assert snapshot(path) == snapshot(backups(path)[0]) == before
