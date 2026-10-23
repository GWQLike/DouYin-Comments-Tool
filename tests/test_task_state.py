"""Worker/supervisor races and controlled handling of malformed local inputs."""
import subprocess
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.app import WorkerManager, create_app
from backend.db import Database
from backend import search
from test_search_api import FakeManager


class ExitingProcess:
    pid = 123456

    def __init__(self, on_exit, *, force=False):
        self.on_exit, self.force, self.exited = on_exit, force, False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout):
        if self.force and timeout == 4:
            raise subprocess.TimeoutExpired('fixture worker', timeout)
        if not self.exited:
            self.on_exit()
            self.exited = True
        return 0


@pytest.mark.parametrize('status,message', [
    ('completed', None), ('private', '该账号未公开喜欢列表'),
    ('paused', '短时间内再次触发限流，冷却后手动继续'),
    ('partial', '平台响应结构异常'), ('failed', '进程无法启动'),
])
@pytest.mark.parametrize('force', [False, True])
def test_stop_retains_final_conclusion_written_while_waiting(tmp_path, monkeypatch, status, message, force):
    db = Database(tmp_path / 'test.db')
    tid = db.create_task('', kind='likes')['id']
    db.save_likes_page(tid, [], 100, False)
    db.update_task(tid, status='running')
    manager = WorkerManager(db, tmp_path)
    manager.task_id = tid
    manager.process = ExitingProcess(lambda: db.update_task(tid, status=status, error=message,
        collection_complete=status == 'completed', visibility='private' if status == 'private' else 'unknown'), force=force)
    killed = []
    monkeypatch.setattr('backend.app.subprocess.run', lambda command, **kwargs: killed.append(command))
    # POSIX uses terminate instead of taskkill.
    manager.process.terminate = lambda: killed.append(['terminate'])
    manager.stop()
    task = db.get_task(tid)
    assert task['status'] == status and task['error'] == message
    assert task['collection_complete'] == (status == 'completed')
    assert db.get_checkpoint(tid, 'likes')['cursor'] == 100
    assert manager.process is None and manager.task_id is None
    assert bool(killed) == force


@pytest.mark.parametrize('status', ['running', 'verifying', 'waiting_login'])
def test_stop_settles_only_unfinished_worker_and_preserves_error(tmp_path, status):
    db = Database(tmp_path / 'test.db')
    tid = db.create_task('')['id']
    db.update_task(tid, status=status, error='原采集错误')
    manager = WorkerManager(db, tmp_path)
    manager.task_id = tid
    manager.process = ExitingProcess(lambda: None)
    manager.stop()
    task = db.get_task(tid)
    assert task['status'] == 'paused' and task['error'] == '原采集错误'
    assert db.should_stop(tid)


@pytest.mark.parametrize('status', ['completed', 'private', 'paused', 'partial', 'failed'])
def test_already_exited_worker_is_not_reclassified_by_stop(tmp_path, status):
    db = Database(tmp_path / 'test.db')
    tid = db.create_task('')['id']
    db.update_task(tid, status=status, error='已保存的结论')
    before = db.get_task(tid)
    manager = WorkerManager(db, tmp_path)
    manager.task_id = tid
    manager.process = ExitingProcess(lambda: None)
    manager.process.exited = True
    manager.stop()
    assert db.get_task(tid) == before


@pytest.mark.parametrize('status', ['completed', 'private', 'paused'])
def test_late_worker_callbacks_cannot_overwrite_terminal_state(tmp_path, status):
    db = Database(tmp_path / 'test.db')
    tid = db.create_task('')['id']
    db.update_task(tid, status=status, error='已有结论')
    before = db.get_task(tid)
    for event in ('waiting', 'resume', 'pause', 'fail', 'worker_failed', 'worker_pause', 'worker_exit', 'finish'):
        db.transition_task(tid, event, message='迟到错误')
        assert db.get_task(tid) == before


def test_verification_lifecycle_keeps_content_error_and_stop_request(tmp_path):
    db = Database(tmp_path / 'test.db')
    tid = db.create_task('')['id']
    db.update_task(tid, status='partial', error='原采集错误', collection_complete=True, stop_requested=True)
    db.transition_task(tid, 'start', verify_only=True)
    assert db.should_stop(tid)  # An in-process start cannot revoke a user stop.
    for event in ('waiting', 'resume', 'finish'):
        db.transition_task(tid, event, message='登录提示' if event == 'waiting' else None, verify_only=True)
        assert db.get_task(tid)['error'] == '原采集错误'
    assert db.get_task(tid)['status'] == 'completed'
    db.transition_task(tid, 'start', stop_requested=False)
    assert db.get_task(tid)['status'] == 'running' and db.get_task(tid)['error'] is None
    assert not db.should_stop(tid)


@pytest.mark.parametrize('status', ['completed', 'private', 'paused'])
def test_api_stop_on_finished_task_is_idempotent(tmp_path, status):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        tid = db.create_task('')['id']
        db.update_task(tid, status=status, error='已有结论')
        before = db.get_task(tid)
        assert client.post(f'/api/tasks/{tid}/stop').json() == before
        assert not db.should_stop(tid)


@pytest.mark.parametrize('origin', [
    'http://127.0.0.1:bad', 'http://127.0.0.1:99999', 'http://[broken',
    'http://[not-ipv6]:8765', 'null', '', ' http://127.0.0.1:8765',
    'http://user@127.0.0.1:8765', 'http://127.0.0.1:8765/path',
    'http://@127.0.0.1:8765', 'http://:@127.0.0.1:8765',
    'http://127.0.0.1:8765?x=1', 'http://127.0.0.1:8765#fragment',
    'http://127.0.0.1:8765 https://evil.example',
])
def test_malformed_origin_is_403_without_starting_operation(tmp_path, origin):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        response = client.post('/api/session/login', headers={'Origin': origin})
        assert response.status_code == 403
        assert client.app.state.db.session_get()['status'] == 'logged_out'


def test_duplicate_origins_rejected_and_valid_origins_still_allowed(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        response = client.post('/api/tasks', json={'url': 'https://www.douyin.com/video/123'},
            headers=[('Origin', 'http://localhost:8765'), ('Origin', 'https://evil.example')])
        assert response.status_code == 403
        for origin in ('http://localhost:8765', 'http://127.0.0.1:8765', 'http://localhost:5173'):
            assert client.post('/api/tasks', headers={'Origin': origin},
                json={'url': 'https://www.douyin.com/video/123'}).status_code == 200


def test_unsupported_system_date_error_becomes_400(tmp_path, monkeypatch):
    class UnsupportedDate(datetime):
        def timestamp(self):
            raise OSError('fixture platform date error')
    monkeypatch.setattr(search, 'datetime', UnsupportedDate)
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        tid = client.app.state.db.create_task('')['id']
        assert client.get(f'/api/tasks/{tid}/comments', params={'start': '0001-01-01'}).status_code == 400
        assert client.post('/api/recommendations/tasks', json={'account': 'fixture', 'start': '0001-01-01'}).status_code == 400
