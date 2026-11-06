"""Source reads leave catalogue requests responsive and preserve snapshot identity."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from helpers import service_for
from starlette.testclient import TestClient
from test_config_view import publish_config
from viewer.persistence.storage import StoredBody

pytest_plugins = ['test_api']


@pytest.mark.parametrize('changed', [False, True])
def test_changes_does_not_block_catalogue_and_rechecks_snapshot(
    client: TestClient, tmp_path: Path, changed: bool
) -> None:
    """Serve catalogue reads during changes calculation and reject a replaced snapshot."""
    rid = publish_config(client, tmp_path)
    service = service_for(client)
    task = service.full_report(rid).tasks[0]
    started = Event()
    release = Event()
    original = service.store.open
    sources = {
        service.runs[rid]['prefix'] + 'data/' + ref
        for file in task.scoring.files
        for ref in (file.base, file.human, file.ai)
        if ref is not None
    }

    def delayed(key: str) -> StoredBody:
        """Pause source reads until the concurrent catalogue request completes."""
        if key in sources:
            started.set()
            assert release.wait(10)
        return original(key)

    service.store.open = delayed
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.get, '/api/v1/runs/' + rid + '/tasks/' + task.id + '?view=changes')
        try:
            assert started.wait(5)
            assert pool.submit(client.get, '/api/v1/baskets').result(timeout=3).status_code == 200
            if changed:
                with service.lock:
                    service.runs[rid]['marker']['archive_sha256'] = '0' * 64
        finally:
            release.set()
        response = future.result(timeout=5)
        assert response.status_code == (409 if changed else 200)
        if not changed:
            assert set(response.json()) == {'human', 'agent'}
    service.store.open = original
