"""HTTP contracts on a real ASGI app and in-memory S3 mock."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import archive_fixture
from starlette.testclient import TestClient

from viewer.config import Config
from viewer.persistence.storage import MemoryStore
from viewer.util import digest
from viewer.web.api import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Yield an isolated API client backed by in-memory storage."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock')
    with TestClient(create_app(config, MemoryStore())) as value:
        yield value


def admin() -> dict[str, str]:
    """Return authorization headers for the fixture administrator."""
    return {'Authorization': 'Bearer ' + 'A' * 32}


def create_user(client: TestClient, login: str = 'alice') -> None:
    """Create a regular user through the administrator API."""
    assert (
        client.post('/api/v1/admin/users', json={'login': login, 'password': 'secret'}, headers=admin()).status_code
        == 201
    )


def grant(client: TestClient, operation: str, login: str = 'alice', rid: str | None = None) -> dict[str, str]:
    """Issue an operation-specific token and return its authorization header."""
    body = {'login': login, 'password': 'secret'}
    if rid:
        body['run_id'] = rid
    r = client.post('/api/v1/auth/' + operation + '-token', json=body)
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def upload(
    client: TestClient, tmp_path: Path, headers: dict[str, str] | None = None
) -> tuple[dict, bytes, dict[str, str], dict]:
    """Publish a fixture archive and return its report, bytes, headers and receipt."""
    path = tmp_path / (str(uuid.uuid4()) + '.tar.gz')
    report, manifest = archive_fixture(path)
    data = path.read_bytes()
    headers = headers or grant(client, 'publish')
    headers.update(
        {
            'Content-Type': 'application/gzip',
            'Idempotency-Key': str(uuid.uuid4()),
            'X-Archive-SHA256': digest(data),
            'X-Report-SHA256': manifest['report_sha256'],
        }
    )
    result = client.put('/api/v1/runs/' + report['run']['id'], content=data, headers=headers)
    assert result.status_code == 201, result.text
    return report, data, headers, result.json()


def test_all_anonymous_reads_and_safe_projection(client: TestClient, tmp_path: Path) -> None:
    """Verify all anonymous reads and safe projection."""
    create_user(client)
    report, data, headers, receipt = upload(client, tmp_path)
    rid = report['run']['id']
    base = '/api/v1/runs/' + rid
    assert client.get('/healthz').json() == {'status': 'ok'}
    baskets = client.get('/api/v1/baskets').json()
    bh = report['basket']['sha256']
    assert baskets['items'][0]['hash'] == bh
    assert client.get('/api/v1/baskets/' + bh + '/runs').json()['items'][0]['run_id'] == rid
    row = client.get(base).json()
    assert row['score'] == 1
    tasks = client.get(base + '/tasks').json()['items']
    task_id = tasks[0]['task_id']
    task = client.get(base + '/tasks/' + task_id).json()
    assert task['condition'] == report['tasks'][0]['agent_task']
    file = client.get(base + '/tasks/' + task_id + '/files/' + task['files'][0]['file_id']).json()
    assert 'print(' in file['human_diff']['text']
    assert client.put(base + '/pinned', json={'pinned': True}).status_code == 401
    assert client.put(base + '/pinned', json={'pinned': True}, headers=admin()).json() == {
        'run_id': rid,
        'pinned': True,
    }
    for response in [row, tasks, task, file]:
        assert 'agent_log' not in str(response) and 'diagnostics' not in str(response)
    assert client.get(base + '/tasks/' + task_id + '/files/../../runtime.json').status_code == 404


def test_owner_archive_metadata_tokens_and_conflict(client: TestClient, tmp_path: Path) -> None:
    """Verify owner archive metadata tokens and conflict."""
    create_user(client)
    create_user(client, 'bob')
    report, data, headers, receipt = upload(client, tmp_path)
    rid = report['run']['id']
    base = '/api/v1/runs/' + rid
    assert client.get(base + '/archive').status_code == 401
    assert client.get(base + '/archive', headers=admin()).content == data
    assert (
        client.post(
            '/api/v1/auth/download-token', json={'login': 'bob', 'password': 'secret', 'run_id': rid}
        ).status_code
        == 403
    )
    token = grant(client, 'download', rid=rid)
    r = client.get(base + '/archive', headers=token)
    assert r.content == data
    assert client.get(base + '/archive', headers=token).status_code == 401
    token = grant(client, 'metadata', rid=rid)
    assert (
        client.patch(
            base + '/metadata',
            json={'display_name': 'custom', 'tags': ['model: A', 'effort: high', 'setup:x']},
            headers=token,
        ).status_code
        == 200
    )
    assert client.get(base).json()['tag_groups'] == {'model': [], 'setup': ['model: A', 'effort: high', 'setup:x']}
    assert client.patch(base + '/metadata', json={'tags': []}, headers=token).status_code == 401
    retry_headers = grant(client, 'publish')
    retry_headers.update({k: v for k, v in headers.items() if k != 'Authorization'})
    assert client.put(base, content=data, headers=retry_headers).json() == receipt
    assert client.get(base + '/archive', headers=grant(client, 'download', rid=rid)).content == data
    headers = grant(client, 'publish')
    headers.update({k: v for k, v in retry_headers.items() if k != 'Authorization'})
    headers['Idempotency-Key'] = str(uuid.uuid4())
    assert client.put(base, content=data, headers=headers).status_code == 409
    assert client.put(base + '?overwrite=1', content=data, headers=headers).status_code == 200


def test_admin_full_lifecycle_and_validation(client: TestClient, tmp_path: Path) -> None:
    """Verify admin full lifecycle and validation."""
    assert client.get('/api/v1/admin/users').status_code == 401
    create_user(client)
    report, _, _, _ = upload(client, tmp_path)
    rid = report['run']['id']
    bh = report['basket']['sha256']
    assert client.get('/api/v1/admin/users', headers=admin()).json()['items'] == [{'login': 'alice', 'enabled': True}]
    assert client.patch('/api/v1/admin/users/alice', json={'enabled': False}, headers=admin()).status_code == 200
    assert client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).status_code == 401
    assert client.patch('/api/v1/admin/users/alice', json={'enabled': True}, headers=admin()).status_code == 200
    assert (
        client.put('/api/v1/admin/users/alice/password', json={'password': 'new'}, headers=admin()).status_code == 200
    )
    assert client.put('/api/v1/admin/baskets/order', json={'items': [bh]}, headers=admin()).status_code == 200
    assert client.put('/api/v1/admin/baskets/order', json={'items': []}, headers=admin()).status_code == 422
    assert client.post('/api/v1/admin/refresh', json={}, headers=admin()).json()['runs'] == 1
    assert client.delete('/api/v1/admin/runs/' + rid, headers=admin()).status_code == 200
    assert client.get('/api/v1/baskets').json()['items'] == []


def test_query_selection_filters_pages_and_errors(client: TestClient, tmp_path: Path) -> None:
    """Verify query selection filters pages and errors."""
    create_user(client)
    report, _, _, _ = upload(client, tmp_path)
    base = '/api/v1/baskets/' + report['basket']['sha256'] + '/runs'
    assert client.get(base + '?statuses=').json()['items'][0]['score'] is None
    assert client.get(base + '?user=bob').json()['total'] == 0
    assert client.get(base + '?q=baseline&limit=1').json()['total'] == 1
    assert client.get(base + '?offset=1').json()['items'] == []
    assert client.get(base + '?limit=201').status_code == 422
    assert client.get(base + '?statuses=WARN').status_code == 422
    rid = report['run']['id']
    assert client.put('/api/v1/runs/' + rid + '/pinned', json={'pinned': 1}, headers=admin()).status_code == 422
    assert client.put('/api/v1/runs/' + rid + '/pinned', json={'pinned': True, 'tags': []}).status_code == 422
    assert client.post('/api/v1/auth/publish-token', content=b'{"password":"x","password":"y"}').status_code == 422


def test_grant_admitted_upload_outlives_ttl(tmp_path: Path) -> None:
    """Verify grant admitted upload outlives ttl."""
    now = [1000.0]
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock')
    with TestClient(create_app(config, MemoryStore(), clock=lambda: now[0])) as client:
        create_user(client)
        headers = grant(client, 'publish')
        path = tmp_path / 'run.tar.gz'
        report, manifest = archive_fixture(path)
        data = path.read_bytes()
        headers.update(
            {
                'Content-Type': 'application/gzip',
                'Idempotency-Key': 'long-upload',
                'X-Archive-SHA256': digest(data),
                'X-Report-SHA256': manifest['report_sha256'],
            }
        )

        def chunks() -> Iterator[bytes]:
            """Advance the clock beyond the upload deadline between body chunks."""
            yield data[:100]
            now[0] += 61
            yield data[100:]

        assert client.put('/api/v1/runs/' + report['run']['id'], content=chunks(), headers=headers).status_code == 201
        expired = grant(client, 'publish')
        now[0] += 60
        expired.update({k: v for k, v in headers.items() if k != 'Authorization'})
        assert client.put('/api/v1/runs/' + report['run']['id'], content=data, headers=expired).status_code == 401


def test_upload_failure_preserves_grant_and_safe_errors(client: TestClient, tmp_path: Path) -> None:
    """Verify upload failure preserves grant and safe errors."""
    create_user(client)
    headers = grant(client, 'publish')
    path = tmp_path / 'run.tar.gz'
    report, manifest = archive_fixture(path)
    data = path.read_bytes()
    headers.update(
        {
            'Content-Type': 'application/gzip',
            'Idempotency-Key': 'validation-retry',
            'X-Archive-SHA256': '0' * 64,
            'X-Report-SHA256': manifest['report_sha256'],
        }
    )
    base = '/api/v1/runs/' + report['run']['id']
    assert client.put(base, content=data, headers=headers).status_code == 422
    headers['X-Archive-SHA256'] = digest(data)
    assert client.put(base, content=data, headers=headers).status_code == 201
    assert (
        client.post(
            '/api/v1/auth/download-token', json={'login': 'alice', 'password': 'secret', 'run_id': []}
        ).status_code
        == 422
    )


def test_missing_and_disallowed_api_responses_are_json(client: TestClient) -> None:
    """Verify missing and disallowed api responses are json."""
    response = client.get('/api/v1/compare')
    assert response.status_code == 404 and response.json()['error']['code'] == 'not_found'
    response = client.delete('/api/v1/baskets')
    assert response.status_code == 405 and response.json()['error']['code'] == 'method_not_allowed'
    assert client.get('/healthz').headers['content-security-policy'].startswith('default-src \'self\'')
