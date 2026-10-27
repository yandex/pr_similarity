"""Read immutable historical publications through the current viewer contracts."""

import io
import json
import tarfile
from pathlib import Path

import pytest
import test_api
from helpers import archive_fixture, service_for
from starlette.testclient import TestClient
from test_api import create_user

from viewer.config import Limits
from viewer.errors import ViewerError
from viewer.models import ArchiveManifest, Report, Run, State
from viewer.models.legacy import LegacyApiEquivalent, LegacyMetric
from viewer.util import digest, json_bytes
from viewer.validation.archive import validate_archive
from viewer.validation.schema import validate_document

client = test_api.client

LEGACY_ARCHIVE = Path(__file__).parent / 'fixtures/legacy/producer-v1-basket2.tar.gz'


def test_historical_producer_archive_preserves_typed_evidence(client: TestClient, tmp_path: Path) -> None:
    """Keep legacy source identities, calculator evidence and downloaded bytes unchanged."""
    original = LEGACY_ARCHIVE.read_bytes()
    imported = validate_archive(LEGACY_ARCHIVE, tmp_path / 'legacy', Limits())
    assert imported.report.schema_version == imported.manifest.schema_version == 1
    assert imported.basket.meta.schema_version == 2
    assert isinstance(imported.report.metric, LegacyMetric)
    assert isinstance(imported.report.tasks[0].telemetry.api_equivalent, LegacyApiEquivalent)
    assert imported.report.last_reprice is not None and imported.report.last_rescore is not None
    for name, kind, model in (
        ('report.json', 'report', Report),
        ('run.json', 'run', Run),
        ('_meta/state.json', 'state', State),
        ('manifest.json', 'archive', ArchiveManifest),
    ):
        raw = json.loads((imported.directory / name).read_bytes())
        validate_document(kind, raw)
        assert json_bytes(model.model_validate(raw).to_wire()) == json_bytes(raw)
    assert any('calculator' in path.parts for path in imported.directory.rglob('*.py'))
    create_user(client)
    service = service_for(client)
    receipt = service.publish(imported, 'alice', 'historical-producer')
    rid = receipt['run_id']
    service.refresh()
    assert client.get('/api/v1/runs/' + rid).status_code == 200
    assert client.get('/api/v1/runs/' + rid + '/config').json()['supported']
    tasks = client.get('/api/v1/runs/' + rid + '/tasks').json()['items']
    assert tasks and client.get('/api/v1/runs/' + rid + '/tasks/' + tasks[0]['task_id']).status_code == 200
    assert receipt['archive_sha256'] == digest(original)
    assert service.store.get(service.runs[rid]['prefix'] + 'archive.tar.gz', len(original)) == original
    assert LEGACY_ARCHIVE.read_bytes() == original


@pytest.mark.parametrize('legacy', [False, True])
def test_archive_version_must_match_report(tmp_path: Path, legacy: bool) -> None:
    """A valid standalone manifest version cannot switch the selected report contract."""
    source = LEGACY_ARCHIVE if legacy else tmp_path / 'current.tar.gz'
    if not legacy:
        archive_fixture(source)
    target = tmp_path / 'mixed.tar.gz'
    with tarfile.open(source, 'r:gz') as archive, tarfile.open(target, 'w:gz') as changed:
        for member in archive:
            stream = archive.extractfile(member)
            assert stream is not None
            content = stream.read()
            if member.name == 'manifest.json':
                value = json.loads(content)
                value['schema_version'] = 2 if legacy else 1
                content = json_bytes(value)
            member.size = len(content)
            changed.addfile(member, io.BytesIO(content))
    with pytest.raises(ViewerError) as caught:
        validate_archive(target, tmp_path / 'mixed', Limits())
    assert caught.value.code == 'invalid_state'


def test_current_archive_rejects_rehashed_legacy_native_start(tmp_path: Path) -> None:
    """Recomputing every owning receipt cannot import v1 native evidence into a v2 run."""

    def mutate(report: dict, _basket: dict, files: dict[str, bytes]) -> None:
        """Alter one native version and reseal its state reference and report digest."""
        state = json.loads(files['_meta/state.json'])
        receipt = state['tasks'][0]['start']
        start = json.loads(files[receipt['path']])
        start['schema_version'] = 1
        files[receipt['path']] = json_bytes(start)
        receipt.update(size_bytes=len(files[receipt['path']]), sha256=digest(files[receipt['path']]))
        files['_meta/state.json'] = json_bytes(state)
        report['state']['sha256'] = digest(files['_meta/state.json'])

    path = tmp_path / 'mixed-native.tar.gz'
    archive_fixture(path, change=mutate)
    with pytest.raises(ViewerError) as caught:
        validate_archive(path, tmp_path / 'native', Limits())
    assert caught.value.code == 'invalid_state'


def _update_receipt(value: object, path: str, files: dict[str, bytes]) -> None:
    """Recompute a selected fixture receipt wherever it appears in one document."""
    if isinstance(value, dict):
        if value.get('path') == path and set(value) == {'path', 'size_bytes', 'sha256'}:
            value.update(size_bytes=len(files[path]), sha256=digest(files[path]))
        else:
            for nested in value.values():
                _update_receipt(nested, path, files)
    elif isinstance(value, list):
        for nested in value:
            _update_receipt(nested, path, files)


@pytest.mark.parametrize('kind', ['rescore', 'reprice'])
def test_legacy_run_rejects_rehashed_current_operation(tmp_path: Path, kind: str) -> None:
    """Current-format operation ownership remains foreign even when every receipt matches."""
    files = {}
    with tarfile.open(LEGACY_ARCHIVE, 'r:gz') as archive:
        for member in archive:
            stream = archive.extractfile(member)
            assert stream is not None
            files[member.name] = stream.read()
    state = json.loads(files['_meta/state.json'])
    selection = state['selected_operations'][kind]
    result_path, receipt_path = selection['result']['path'], selection['receipt']['path']
    result = json.loads(files[result_path])
    result['schema_version'] = 2
    files[result_path] = json_bytes(result)
    receipt = json.loads(files[receipt_path])
    _update_receipt(receipt, result_path, files)
    files[receipt_path] = json_bytes(receipt)
    for path in (result_path, receipt_path):
        _update_receipt(state, path, files)
    files['_meta/state.json'] = json_bytes(state)
    report = json.loads(files['report.json'])
    report['state']['sha256'] = digest(files['_meta/state.json'])
    files['report.json'] = json_bytes(report)
    manifest = json.loads(files['manifest.json'])
    manifest['report_sha256'] = digest(files['report.json'])
    for path in (result_path, receipt_path, '_meta/state.json', 'report.json'):
        _update_receipt(manifest, path, files)
    files['manifest.json'] = json_bytes(manifest)
    target = tmp_path / 'mixed-operation.tar.gz'
    with tarfile.open(target, 'w:gz') as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    with pytest.raises(ViewerError) as caught:
        validate_archive(target, tmp_path / 'mixed-operation', Limits())
    assert caught.value.code == 'invalid_state'
    assert caught.value.message == 'Uploaded operation evidence is inconsistent.'
