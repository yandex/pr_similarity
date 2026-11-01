"""Reject invalid display evidence before a publication can poison durable recovery."""

import json
import uuid
from pathlib import Path

import helpers
import pytest
from helpers import archive_fixture, service_for
from starlette.testclient import TestClient
from test_api import create_user, grant, upload
from test_partial_usage import partial_usage
from viewer.config import Config
from viewer.persistence.storage import MockFileStore
from viewer.util import digest, json_bytes
from viewer.web.api import create_app


def _unsigned_usage_archive(path: Path, run_id: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """Omit only the usage receipt before dependent hashes and state are frozen."""
    original = helpers._reference

    def reference(name: str, files: dict[str, bytes]) -> dict:
        """Keep all hashes coherent while simulating an unsigned declared ledger."""
        if name.endswith('/seal.json'):
            seal = json.loads(files[name])
            seal['artifact_receipts'] = [
                row for row in seal['artifact_receipts'] if not row['path'].endswith('/telemetry/usage.json')
            ]
            files[name] = json_bytes(seal)
        return original(name, files)

    with monkeypatch.context() as local:
        local.setattr(helpers, '_reference', reference)
        return archive_fixture(path, run_id=run_id, configure=partial_usage)


@pytest.mark.parametrize('overwrite', [False, True])
def test_rejected_display_evidence_preserves_publications_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overwrite: bool
) -> None:
    """A failed HTTP upload leaves storage, ownership, grants and recovery usable."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock')
    store = MockFileStore(tmp_path / 'objects')
    with TestClient(create_app(config, store)) as client:
        create_user(client)
        healthy, _, _, _ = upload(client, tmp_path)
        healthy_id = healthy['run']['id']
        rid = healthy_id if overwrite else str(uuid.uuid4())
        path = tmp_path / 'unsigned.tar.gz'
        _, manifest = _unsigned_usage_archive(path, rid, monkeypatch)
        data = path.read_bytes()
        headers = {
            **grant(client, 'publish'),
            'Content-Type': 'application/gzip',
            'Idempotency-Key': 'display-validation',
            'X-Archive-SHA256': digest(data),
            'X-Report-SHA256': manifest['report_sha256'],
        }
        before = {key: store.get(key, config.limits.archive_bytes) for key in store.list('')}
        catalogue = client.get('/api/v1/baskets').json()
        response = client.put(f'/api/v1/runs/{rid}?overwrite={int(overwrite)}', content=data, headers=headers)
        assert response.status_code == 422
        assert response.json()['error']['code'] == 'usage_receipt_missing'
        svc = service_for(client)
        assert svc.db.operations() == {}
        assert {key: store.get(key, config.limits.archive_bytes) for key in store.list('')} == before
        assert client.get('/api/v1/baskets').json() == catalogue
        assert client.get('/api/v1/runs/' + healthy_id).status_code == 200
        assert (svc.db.owner(rid) is not None) is overwrite
        assert not list(svc.staging.iterdir())

    with TestClient(create_app(config, MockFileStore(tmp_path / 'objects'))) as restarted:
        assert restarted.get('/api/v1/baskets').json() == catalogue
        assert restarted.get('/api/v1/runs/' + healthy_id).status_code == 200
        assert service_for(restarted).db.operations() == {}
        valid = tmp_path / 'valid.tar.gz'
        _, manifest = archive_fixture(valid, run_id=rid, configure=partial_usage)
        data = valid.read_bytes()
        headers.update({'X-Archive-SHA256': digest(data), 'X-Report-SHA256': manifest['report_sha256']})
        response = restarted.put(f'/api/v1/runs/{rid}?overwrite={int(overwrite)}', content=data, headers=headers)
        assert response.status_code == (200 if overwrite else 201), response.text
        assert restarted.get('/api/v1/runs/' + rid).json()['cost']['partial_tasks'] == 1
        assert service_for(restarted).db.operations() == {}
