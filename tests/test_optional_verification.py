import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import Database
from backend.engine import WorkbenchRuntime, run_engine
from backend.search import Filters, select_comments
from test_search_api import FakeManager, row
from test_likes_api import video
from test_engine_integration import Fixture, SimulatedTransport, args_for, create, exported, fixture_signing
from test_crawler import comment, page
from test_likes_collector import profile
import httpx


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
def test_option_defaults_patch_conflict_refresh_and_verify(tmp_path, kind):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        endpoint = '/api/tasks' if kind == 'comments' else f'/api/{kind}/tasks'
        body = {'url': 'https://www.douyin.com/video/123456789'} if kind == 'comments' else {'account': 'target.user'}
        task = client.post(endpoint, json=body).json()
        tid = task['id']
        assert task['verify_authors'] is False
        assert client.patch(f'/api/tasks/{tid}', json={'verify_authors': 'false'}).status_code == 400
        assert client.patch(f'/api/tasks/{tid}', json={'verify_authors': True}).json()['verify_authors'] is True
        assert client.post(f'/api/tasks/{tid}/refresh').json()['verify_authors'] is True
        client.post(f'/api/tasks/{tid}/start')
        assert client.patch(f'/api/tasks/{tid}', json={'verify_authors': False}).status_code == 409
        assert client.post(f'/api/tasks/{tid}/verify').status_code == 409
        client.post(f'/api/tasks/{tid}/stop')
        assert client.patch(f'/api/tasks/{tid}', json={'verify_authors': False}).json()['verify_authors'] is False
        db = client.app.state.db
        if kind == 'comments':
            db.save_page(tid, '', [row(douyin_id='', identity_state='unknown')], 20, False)
        else:
            db.save_likes_page(tid, [video(author_douyin_id='')], 20, False, checkpoint_key=kind)
        result = client.post(f'/api/tasks/{tid}/verify')
        assert result.status_code == 200 and result.json()['status'] == 'verifying'
        assert result.json()['verify_authors'] is False


def test_existing_task_migration_preserves_opt_in_and_new_task_opts_out(tmp_path):
    from test_migrations import legacy_database
    path = tmp_path / 'legacy.db'
    legacy_database(path)
    tid = 'comments'
    with sqlite3.connect(path) as con:
        con.execute('ALTER TABLE tasks DROP COLUMN verify_authors')
        con.execute("UPDATE checkpoints SET cursor='123' WHERE task_id=? AND thread_id=''", (tid,))
    db = Database(path)
    assert db.get_task(tid)['verify_authors'] is True
    assert db.get_checkpoint(tid)['cursor'] == 123
    assert db.get_task(tid)['main_count'] == 1
    new = db.create_task('new')
    assert new['verify_authors'] is False
    assert Database(path).get_task(new['id'])['verify_authors'] is False


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
async def test_optional_then_verify_only_matches_full_export_without_recollecting(tmp_path, kind):
    db = Database(tmp_path / 'results.db')
    outputs = []
    for enabled in (True, False):
        task = create(db, kind, verify_authors=enabled)
        fixture = Fixture(kind)
        args = args_for(tmp_path / str(enabled), task)
        runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
            transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
        await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
        actual = db.get_task(task['id'])
        assert actual['collection_complete'] and actual['status'] == 'completed'
        profiles = [r.url.params['sec_user_id'] for r in fixture.requests if r.url.path.endswith('/profile/other/')]
        assert ('target_sec' in profiles) == (kind != 'comments')
        assert ('author_sec' in profiles) == enabled
        if not enabled:
            assert actual['unknown_authors'] == 1 and not actual['verification_complete']
            assert '未提供' in exported(db, task['id'], kind).decode('utf-8-sig')
            if kind == 'comments':
                assert select_comments(db.read_comments(task['id']), Filters(account='author.one')) == []
            checkpoint = db.get_checkpoint(task['id'], '' if kind == 'comments' else kind)
            fixture.requests.clear()
            args.verify_only = True
            runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
                transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
            await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
            assert len(fixture.requests) == 1
            assert fixture.requests[0].url.params['sec_user_id'] == 'author_sec'
            assert db.get_checkpoint(task['id'], '' if kind == 'comments' else kind) == checkpoint
            assert db.get_task(task['id'])['verify_authors'] is False
            assert db.get_task(task['id'])['verification_complete']
        outputs.append(exported(db, task['id'], kind))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('kind', ['likes', 'recommendations'])
async def test_verify_only_keeps_limit_and_private_conclusions(tmp_path, kind):
    db = Database(tmp_path / 'data.db')
    task = create(db, kind, max_videos=1, verify_authors=False)
    db.save_likes_page(task['id'], [video(author_key='author_uid', uid='author_uid', sec_uid='author_sec', author_douyin_id='')], 10, False, checkpoint_key=kind)
    db.update_task(task['id'], visibility='private', status='private')
    fixture = Fixture(kind)
    args = args_for(tmp_path, task)
    args.verify_only = True
    runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
        transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
    await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    actual = db.get_task(task['id'])
    assert actual['status'] == 'private' and not actual['collection_complete']
    assert actual['video_count'] == actual['max_videos'] == 1 and actual['unknown_authors'] == 0
    assert db.get_checkpoint(task['id'], kind)['cursor'] == 10
    assert len(fixture.requests) == 1


async def test_interrupted_verification_keeps_saved_identity_and_resumes_remaining(tmp_path):
    class TwoAuthors(Fixture):
        def __call__(self, request):
            if request.url.path.endswith('/profile/other/') and request.url.params['sec_user_id'] == 'second_sec':
                self.requests.append(request)
                return httpx.Response(200, json=profile('second.public', 'second_sec', 'second_uid'))
            return super().__call__(request)
    db = Database(tmp_path / 'data.db')
    task = create(db, 'comments', verify_authors=False)
    fixture = TwoAuthors('comments')
    fixture.main = {0: page([comment('first', user={'uid':'author_uid', 'sec_uid':'author_sec'}),
        comment('second', user={'uid':'second_uid', 'sec_uid':'second_sec'})], 20)}
    args = args_for(tmp_path, task)
    async def run():
        runtime = WorkbenchRuntime(db, args, lambda: False, lambda *args: None,
            transport_factory=lambda *args: SimulatedTransport(*args, fixture=fixture))
        await run_engine(db, args, runtime.stop, runtime.waiting, runtime=runtime)
    await run()
    assert db.get_task(task['id'])['unknown_authors'] == 2
    args.verify_only, args.max_requests = True, 1
    fixture.requests.clear()
    await run()
    state = db.get_task(task['id'])
    assert state['status'] == 'paused' and state['unknown_authors'] == 1
    assert state['collection_complete']
    first_sec = fixture.requests[0].url.params['sec_user_id']
    fixture.requests.clear()
    args.max_requests = 0
    await run()
    assert db.get_task(task['id'])['verification_complete']
    assert len(fixture.requests) == 1 and fixture.requests[0].url.params['sec_user_id'] != first_sec
