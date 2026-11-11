"""Administrative metadata grants preserve ownership and one-use admission."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_local_api
from starlette.testclient import TestClient
from test_local_api import get_local_service, local_upload
from test_local_service import require_record
from viewer.errors import ViewerError
from viewer.util import digest

local_client = test_local_api.local_client


def admin_grant(client: TestClient) -> dict[str, str]:
    """Issue the shared administrative mutation credential through its HTTP route."""
    response = client.post('/api/v1/auth/admin-overwrite-token', json={'login': 'operator', 'password': 'secret'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_admin_metadata_preserves_publication_and_queues_backup(local_client: TestClient, tmp_path: Path) -> None:
    """Only metadata changes when an administrator edits another publisher's run."""
    service = get_local_service(local_client)
    assert service.backups.close(5)
    uploaded, report, _, _ = local_upload(local_client, tmp_path)
    assert uploaded.status_code == 201, uploaded.text
    rid = report['run']['id']
    service.set_pinned(rid, True)
    before = require_record(service, rid)
    owner = service.db.owner(rid)
    grant = admin_grant(local_client)
    labels = ['second', 'first', 'second']
    changed = local_client.patch(
        '/api/v1/runs/' + rid + '/metadata',
        json={'display_name': 'Corrected name', 'tags': labels},
        headers=grant,
    )
    assert changed.status_code == 200, changed.text
    after = require_record(service, rid)
    assert after.owner == before.owner == 'alice'
    assert (after.generation, after.archive_sha256) == (before.generation, before.archive_sha256)
    assert service.db.owner(rid) == owner
    assert after.metadata == {**before.metadata, 'display_name': 'Corrected name', 'tags': labels}
    assert after.metadata['pinned'] is True
    assert after.metadata_revision == before.metadata_revision + 1
    assert after.backup_state == 'pending'
    with service.lock:
        job = service.db.connection.execute(
            'SELECT kind,state FROM local_backup_jobs WHERE run_id=? AND metadata_revision=?',
            (rid, after.metadata_revision),
        ).fetchone()
    assert job is not None and (job['kind'], job['state']) == ('metadata', 'pending')
    repeated = local_client.patch('/api/v1/runs/' + rid + '/metadata', json={'tags': []}, headers=grant)
    assert repeated.status_code == 401, repeated.text


def test_admin_metadata_fields_are_independent(local_client: TestClient, tmp_path: Path) -> None:
    """Partial updates retain omitted fields and accept an explicitly empty tag list."""
    uploaded, report, _, _ = local_upload(local_client, tmp_path)
    assert uploaded.status_code == 201
    service = get_local_service(local_client)
    rid = report['run']['id']
    before = require_record(service, rid)
    path = '/api/v1/runs/' + rid + '/metadata'
    renamed = local_client.patch(path, json={'display_name': 'Renamed'}, headers=admin_grant(local_client))
    assert renamed.status_code == 200, renamed.text
    assert require_record(service, rid).metadata['tags'] == before.metadata['tags']
    cleared = local_client.patch(path, json={'tags': []}, headers=admin_grant(local_client))
    assert cleared.status_code == 200, cleared.text
    assert require_record(service, rid).metadata['display_name'] == 'Renamed'
    assert require_record(service, rid).metadata['tags'] == []


@pytest.mark.parametrize('revocation', ['expired', 'role', 'disabled', 'password'])
def test_admin_metadata_rejects_revoked_grant(local_client: TestClient, tmp_path: Path, revocation: str) -> None:
    """Both expiry and current account authorization apply to metadata admission."""
    uploaded, report, _, _ = local_upload(local_client, tmp_path)
    assert uploaded.status_code == 201
    service = get_local_service(local_client)
    rid = report['run']['id']
    before = require_record(service, rid)
    headers = admin_grant(local_client)
    db = service.db
    if revocation == 'expired':
        with db.lock, db.connection:
            db.connection.execute(
                'UPDATE grants SET expires=? WHERE token_hash=?',
                (db.clock() - 1, digest(headers['Authorization'][7:].encode())),
            )
    elif revocation == 'password':
        db.set_password('operator', 'new-secret')
    else:
        db.create_user('keeper', 'secret', role='admin')
        if revocation == 'role':
            db.set_role('operator', 'user')
        else:
            db.update_user('operator', enabled=False)
    response = local_client.patch('/api/v1/runs/' + rid + '/metadata', json={'tags': []}, headers=headers)
    assert response.status_code == 401, response.text
    after = require_record(service, rid)
    assert (after.metadata_revision, after.metadata) == (before.metadata_revision, before.metadata)


@pytest.mark.parametrize('patch', [{'pinned': True}, {'owner': 'operator'}, {'tags': [1]}, {'display_name': ''}])
def test_admin_metadata_invalid_payload_does_not_consume(local_client: TestClient, tmp_path: Path, patch: dict) -> None:
    """Invalid data cannot spend a valid credential or mutate protected fields."""
    uploaded, report, _, _ = local_upload(local_client, tmp_path)
    assert uploaded.status_code == 201
    path = '/api/v1/runs/' + report['run']['id'] + '/metadata'
    headers = admin_grant(local_client)
    invalid = local_client.patch(path, json=patch, headers=headers)
    assert invalid.status_code == 422, invalid.text
    valid = local_client.patch(path, json={'display_name': 'Valid'}, headers=headers)
    assert valid.status_code == 200, valid.text


def test_admin_owner_grant_remains_bound_to_its_run(local_client: TestClient, tmp_path: Path) -> None:
    """An administrator's ordinary metadata credential does not gain global scope."""
    uploaded, foreign, _, _ = local_upload(local_client, tmp_path)
    assert uploaded.status_code == 201
    published = local_client.post(
        '/api/v1/auth/publish-token', json={'login': 'operator', 'password': 'secret'}
    ).json()['token']
    own_response, own, _, _ = local_upload(local_client, tmp_path, grant=published)
    assert own_response.status_code == 201, own_response.text
    token = local_client.post(
        '/api/v1/auth/metadata-token',
        json={'login': 'operator', 'password': 'secret', 'run_id': own['run']['id']},
    ).json()['token']
    headers = {'Authorization': 'Bearer ' + token}
    denied = local_client.patch(
        '/api/v1/runs/' + foreign['run']['id'] + '/metadata', json={'tags': []}, headers=headers
    )
    assert denied.status_code == 401, denied.text
    allowed = local_client.patch('/api/v1/runs/' + own['run']['id'] + '/metadata', json={'tags': []}, headers=headers)
    assert allowed.status_code == 200, allowed.text


@pytest.mark.parametrize('first', ['metadata', 'overwrite'])
def test_admin_grant_is_one_use_across_mutation_kinds(local_client: TestClient, tmp_path: Path, first: str) -> None:
    """A completed mutation cannot turn its consumed token into another operation."""
    initial, report, publication, _ = local_upload(local_client, tmp_path)
    other, other_report, other_publication, _ = local_upload(local_client, tmp_path)
    assert initial.status_code == other.status_code == 201
    headers = admin_grant(local_client)
    token = headers['Authorization'][7:]
    path = '/api/v1/runs/' + report['run']['id'] + '/metadata'
    if first == 'metadata':
        response = local_client.patch(path, json={'tags': []}, headers=headers)
        assert response.status_code == 200, response.text
        denied, _, _, _ = local_upload(
            local_client,
            tmp_path,
            run_id=other_report['run']['id'],
            grant=token,
            overwrite=True,
            expected=other_publication['X-Archive-SHA256'],
        )
    else:
        response, _, _, _ = local_upload(
            local_client,
            tmp_path,
            run_id=report['run']['id'],
            grant=token,
            overwrite=True,
            expected=publication['X-Archive-SHA256'],
        )
        assert response.status_code == 200, response.text
        denied = local_client.patch(path, json={'tags': []}, headers=headers)
    assert denied.status_code == 401, denied.text


def test_admin_metadata_concurrent_reuse_admits_one(local_client: TestClient, tmp_path: Path) -> None:
    """Concurrent requests for different runs cannot reuse one administrative grant."""
    first, first_report, _, _ = local_upload(local_client, tmp_path)
    second, second_report, _, _ = local_upload(local_client, tmp_path)
    assert first.status_code == second.status_code == 201
    headers = admin_grant(local_client)
    service = get_local_service(local_client)
    ids = [first_report['run']['id'], second_report['run']['id']]
    revisions = {rid: require_record(service, rid).metadata_revision for rid in ids}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                local_client.patch,
                '/api/v1/runs/' + rid + '/metadata',
                json={'display_name': 'Once'},
                headers=headers,
            )
            for rid in ids
        ]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(response.status_code for response in results) == [200, 401]
    assert sum(require_record(service, rid).metadata_revision - revisions[rid] for rid in ids) == 1


def test_admin_metadata_rechecks_role_after_waiting_for_run(local_client: TestClient, tmp_path: Path) -> None:
    """A grant admitted earlier does not retain privilege while waiting for another mutation."""
    uploaded, report, _, _ = local_upload(local_client, tmp_path)
    assert uploaded.status_code == 201
    service = get_local_service(local_client)
    rid = report['run']['id']
    before = require_record(service, rid)
    service.db.create_user('keeper', 'secret', role='admin')
    started = threading.Event()

    def update() -> dict:
        """Attempt the mutation after signaling entry to the worker."""
        started.set()
        return service.patch_metadata(rid, 'operator', {'tags': []}, admin=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service._run_lock(rid):
            future = pool.submit(update)
            assert started.wait(5)
            service.db.set_role('operator', 'user')
        with pytest.raises(ViewerError) as rejected:
            future.result(timeout=5)
    assert rejected.value.code == 'invalid_grant'
    after = require_record(service, rid)
    assert (after.metadata_revision, after.metadata) == (before.metadata_revision, before.metadata)
