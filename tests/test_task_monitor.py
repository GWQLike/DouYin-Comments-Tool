from fastapi.testclient import TestClient
import pytest

from backend.app import create_app
from test_search_api import FakeManager, row


@pytest.mark.parametrize('kind', ['comments', 'likes', 'recommendations'])
def test_single_task_monitor_matches_history_without_listing_all(tmp_path, monkeypatch, kind):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        db = client.app.state.db
        task = db.create_task('', kind=kind, target_account='sample')
        if kind == 'comments':
            db.save_page(task['id'], '', [row('one'), row('two', parent_id='one')], 1, False)
        expected = db.list_tasks()[0]
        monkeypatch.setattr(db, 'list_tasks', lambda: pytest.fail('monitor must not list historical tasks'))
        response = client.get(f"/api/tasks/{task['id']}")
        assert response.status_code == 200
        assert response.json() == expected
        assert 'stop_requested' not in response.json()
        db.update_task(task['id'], status='paused')
        assert client.get(f"/api/tasks/{task['id']}").json()['status'] == 'paused'


def test_missing_monitor_task_returns_404(tmp_path):
    with TestClient(create_app(tmp_path, manager_factory=FakeManager)) as client:
        assert client.get('/api/tasks/missing').status_code == 404
