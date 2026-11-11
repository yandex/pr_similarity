"""Grant validation belongs to first admission, before its atomic consumption."""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_local_api
from starlette.requests import Request
from starlette.testclient import TestClient
from test_local_api import get_local_service, local_upload
from test_local_service import require_record
from viewer.bundles import BundleDescriptor, DownloadedBundle
from viewer.config import Limits
from viewer.errors import ViewerError
from viewer.validation.archive import ImportedRun
from viewer.web import api

local_client = test_local_api.local_client


def publication_grant(client: TestClient, *, admin: bool) -> str:
    """Issue a credential through the owner or administrator HTTP endpoint."""
    operation = 'admin-overwrite' if admin else 'publish'
    login = 'operator' if admin else 'alice'
    response = client.post('/api/v1/auth/' + operation + '-token', json={'login': login, 'password': 'secret'})
    assert response.status_code == 200, response.text
    return response.json()['token']


@pytest.mark.parametrize('admin', [False, True])
def test_expired_grant_rejected_after_waiting_for_cold_restore(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admin: bool
) -> None:
    """A real cold read can delay admission past TTL without spending the expired grant."""
    service = get_local_service(local_client)
    assert service.backups.close(5)
    response, report, headers, _ = local_upload(local_client, tmp_path)
    assert response.status_code == 201
    rid = report['run']['id']
    service.backups.stop.clear()
    assert service.backups.run_once()
    service.backups.stop.set()
    before = require_record(service, rid)
    assert before.backup_state == 'backed_up'
    now = [service.db.clock() + service.config.cache_idle_ttl_seconds + 1]
    monkeypatch.setattr(service.db, 'clock', lambda: now[0])
    assert service.cleanup_cache() == 1
    token = publication_grant(local_client, admin=admin)
    entered, release, authorized = threading.Event(), threading.Event(), threading.Event()
    download, actor = service.bundles.download_bundle, api._Application.publication_actor

    def slow_download(descriptor: BundleDescriptor, destination: Path) -> DownloadedBundle:
        """Keep a real restore holding the run lock until the publication has authenticated."""
        entered.set()
        assert release.wait(15)
        return download(descriptor, destination)

    def checked_actor(
        application: api._Application, request: Request, key: str, *, status: bool = False
    ) -> tuple[str, bool]:
        """Signal the initial HTTP credential check before admission waits for the run."""
        result = actor(application, request, key, status=status)
        authorized.set()
        return result

    monkeypatch.setattr(service.bundles, 'download_bundle', slow_download)
    monkeypatch.setattr(api._Application, 'publication_actor', checked_actor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cold = pool.submit(local_client.get, '/api/v1/runs/' + rid)
        assert entered.wait(5)
        overwrite = pool.submit(
            local_upload,
            local_client,
            tmp_path,
            run_id=rid,
            grant=token,
            overwrite=True,
            expected=headers['X-Archive-SHA256'],
        )
        try:
            assert authorized.wait(5)
            now[0] += max(service.config.grant_ttl_seconds, service.config.admin_overwrite_token_ttl_seconds) + 1
            with pytest.raises(ViewerError) as expired:
                service.db.authorize(token, 'admin-overwrite' if admin else 'publish')
            assert expired.value.code == 'invalid_grant'
        finally:
            release.set()
        cold_response = cold.result(timeout=10)
        rejected, _, rejected_headers, _ = overwrite.result(timeout=10)
    assert rejected.status_code == 401, rejected.text
    assert rejected.json()['error']['code'] == 'invalid_grant'
    assert cold_response.status_code == 200, cold_response.text
    after = require_record(service, rid)
    assert (after.generation, after.archive_sha256) == (before.generation, before.archive_sha256)
    assert service.repository.operation('operator' if admin else 'alice', rejected_headers['Idempotency-Key']) is None
    assert service.busy_runs() == set()
    assert service.files.reserved == 0


@pytest.mark.parametrize('admin', [False, True])
def test_admitted_upload_and_terminal_replay_survive_grant_expiry(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admin: bool
) -> None:
    """Expiry after admission cannot abort its upload or invalidate an exact terminal replay."""
    service = get_local_service(local_client)
    response, report, initial_headers, _ = local_upload(local_client, tmp_path)
    assert response.status_code == 201
    rid = report['run']['id']
    token = publication_grant(local_client, admin=admin)
    now = [service.db.clock()]
    monkeypatch.setattr(service.db, 'clock', lambda: now[0])
    validate = api.validate_archive

    def expire_during_validation(
        path: Path,
        destination: Path,
        limits: Limits,
        *,
        deadline: float | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> ImportedRun:
        """Advance beyond grant lifetime only after HTTP admission has reserved the run."""
        assert rid in service.busy_runs()
        now[0] += max(service.config.grant_ttl_seconds, service.config.admin_overwrite_token_ttl_seconds) + 1
        return validate(path, destination, limits, deadline=deadline, on_phase=on_phase)

    monkeypatch.setattr(api, 'validate_archive', expire_during_validation)
    completed, _, headers, _ = local_upload(
        local_client,
        tmp_path,
        run_id=rid,
        grant=token,
        overwrite=True,
        expected=initial_headers['X-Archive-SHA256'],
    )
    assert completed.status_code == 200, completed.text
    assert require_record(service, rid).generation == 2
    replay = local_client.put(
        '/api/v1/runs/' + rid,
        content=b'',
        headers=headers,
        params={'overwrite': '1', 'expected_publication_sha256': initial_headers['X-Archive-SHA256']},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == completed.json()
    assert require_record(service, rid).generation == 2


@pytest.mark.parametrize(
    ('grant_login', 'operation', 'bound', 'admin'),
    [
        ('operator', 'publish', False, False),
        ('alice', 'metadata', False, False),
        ('alice', 'publish', True, False),
        ('operator', 'publish', False, True),
        ('alice', 'admin-overwrite', False, True),
    ],
)
def test_admission_checks_grant_actor_purpose_and_run_scope(
    local_client: TestClient, tmp_path: Path, grant_login: str, operation: str, bound: bool, admin: bool
) -> None:
    """The durable admission boundary rejects valid grants for a different authorization context."""
    service = get_local_service(local_client)
    response, report, headers, _ = local_upload(local_client, tmp_path)
    assert response.status_code == 201
    rid = report['run']['id']
    token = service.db.issue(grant_login, 'secret', operation, rid if bound else None)['token']
    actor = 'operator' if admin else 'alice'
    key = str(uuid.uuid4())
    with pytest.raises(ViewerError) as rejected:
        service.admit_publication(
            actor,
            key,
            rid,
            headers['X-Archive-SHA256'],
            headers['X-Report-SHA256'],
            overwrite=True,
            admin=admin,
            expected_publication_sha256=headers['X-Archive-SHA256'],
            grant_token=token,
            reserve_upload=True,
        )
    assert rejected.value.code == 'invalid_grant'
    assert service.repository.operation(actor, key) is None
    assert service.db.authorize(token, operation, rid if bound else None) == grant_login
    assert service.busy_runs() == set()
    assert service.files.reserved == 0
    assert require_record(service, rid).generation == 1
