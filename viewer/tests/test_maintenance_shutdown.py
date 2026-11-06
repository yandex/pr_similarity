"""Shutdown waits for workers and rejects new tasks without closing live resources."""

import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.maintenance import Maintenance
from viewer.persistence.storage import MemoryStore
from viewer.web.api import create_app


def test_shutdown_timeout_retains_worker_until_it_finishes() -> None:
    """Keep the worker alive after a bounded join times out."""
    entered, release = threading.Event(), threading.Event()

    def recovery() -> None:
        """Hold recovery until the test allows shutdown to finish."""
        entered.set()
        release.wait(5)

    worker = Maintenance(recovery, 1, 1)
    worker.start()
    worker.request_recovery()
    assert entered.wait(2)
    try:
        assert worker.close(timeout=0.01) is False
        with pytest.raises(ViewerError):
            worker.request_refresh()
        assert worker.worker is not None
        assert worker.worker.is_alive()
    finally:
        release.set()
        assert worker.close(timeout=5) is True


def test_refresh_status_reports_failure_and_last_success_separately() -> None:
    """Retain the last successful timestamp after a later refresh fails."""
    fail = []

    def refresh() -> bool:
        """Simulate a successful scan followed by a settings failure."""
        if fail:
            raise ViewerError('invalid_metadata', 'bad settings')
        return True

    worker = Maintenance(lambda: None, 1, 1, refresh)
    worker.start()
    try:
        job = worker.request_refresh()
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.refresh_state['status'] == 'succeeded', 5)
        first = worker.refresh_status()
        assert first['completed_id'] == job['request_id']
        assert first['last_success_at'] and first['finished_at']
        fail.append(True)
        worker.request_refresh()
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.refresh_state['status'] == 'failed', 5)
        second = worker.refresh_status()
        assert second['last_success_at'] == first['last_success_at']
        assert second['error_code'] == 'invalid_metadata'
    finally:
        worker.close()


def test_stopped_app_rejects_callbacks_before_accessing_closed_sqlite(tmp_path: Path) -> None:
    """Queued callbacks see shutdown even when the database has already closed."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    app = create_app(Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock'), MemoryStore())
    assert app.state.service.close()
    app.state.service.db.close()
    client = TestClient(app)
    try:
        response = client.patch(
            '/api/v1/runs/00000000-0000-4000-8000-000000000001/metadata', json={'display_name': 'name'}
        )
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'service_stopping'
        assert client.get('/api/v1/admin/stats').json()['error']['code'] == 'service_stopping'
        assert client.get('/readyz').status_code == 503
    finally:
        client.close()
