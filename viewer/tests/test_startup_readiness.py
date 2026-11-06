"""A listening process reports startup progress before catalogue readiness."""

import threading
from collections.abc import Iterator
from pathlib import Path

from starlette.testclient import TestClient
from viewer.config import Config
from viewer.persistence.storage import MemoryStore
from viewer.util import json_bytes
from viewer.web.api import create_app


def test_deferred_startup_exposes_liveness_without_serving_unchecked_catalogue(tmp_path: Path) -> None:
    """Serve liveness while startup validation keeps result APIs unavailable."""
    entered, release = threading.Event(), threading.Event()

    class SlowStore(MemoryStore):
        """Hold publication enumeration until the test releases startup."""

        def list(self, prefix: str) -> Iterator[str]:
            """Pause the startup scan while allowing liveness probes."""
            if prefix == 'runs/':
                entered.set()
                assert release.wait(10)
            yield from super().list(prefix)

    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    app = create_app(
        Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock'),
        SlowStore(),
        deferred_startup=True,
    )
    with TestClient(app) as client:
        try:
            assert entered.wait(5)
            assert client.get('/healthz').json() == {'status': 'ok'}
            response = client.get('/readyz')
            assert response.status_code == 503
            assert response.json()['phase'] == 'catalogue'
            assert client.get('/api/v1/baskets').status_code == 503
        finally:
            release.set()
        worker = app.state.service.maintenance
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.state['status'] == 'succeeded', 5)
        assert client.get('/readyz').status_code == 200
        assert client.get('/api/v1/baskets').status_code == 200


def test_failed_startup_can_be_retried_by_administrator(tmp_path: Path) -> None:
    """Allow an authenticated refresh to retry failed startup validation."""
    store = MemoryStore()
    store.put('settings/basket-order.json', json_bytes('invalid'))
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    app = create_app(
        Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock'),
        store,
        deferred_startup=True,
    )
    with TestClient(app) as client:
        worker = app.state.service.maintenance
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.state['status'] == 'failed', 5)
        assert client.get('/readyz').json()['error_code'] == 'invalid_basket_order'
        assert client.post('/api/v1/admin/refresh', json={}).status_code == 401
        store.put('settings/basket-order.json', json_bytes([]))
        response = client.post('/api/v1/admin/refresh', json={}, headers={'Authorization': 'Bearer ' + 'A' * 32})
        assert response.status_code == 200
        request_id = response.json()['refresh_job']['request_id']
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.refresh_state['completed_id'] == request_id, 5)
        assert client.get('/readyz').status_code == 200
