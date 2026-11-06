"""Real interrupted producer history at the untrusted viewer archive boundary."""

import os
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest
from helpers import _write_archive, service_for
from starlette.testclient import TestClient
from test_api import create_user, grant
from viewer.config import Config, Limits
from viewer.errors import ViewerError
from viewer.presentation.inference import build_inference
from viewer.util import JsonObject, JsonValue, digest, json_bytes, parse_json
from viewer.validation.archive import validate_archive
from viewer.web.api import create_app


def _object(value: JsonValue) -> JsonObject:
    """Require a fixture object while retaining its identity for deliberate mutation."""
    assert isinstance(value, dict)
    return value


def _objects(value: JsonValue) -> list[JsonObject]:
    """Require a fixture array of mutable JSON objects."""
    assert isinstance(value, list)
    return [_object(item) for item in value]


def _text(value: JsonValue) -> str:
    """Require a fixture string before using it as an archive member path."""
    assert isinstance(value, str)
    return value


def _archive_files(path: Path) -> dict[str, bytes]:
    """Read regular members from the real producer archive without extracting paths."""
    files = {}
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                with stream:
                    files[member.name] = stream.read()
    return files


def _receipt(files: dict[str, bytes], path: str) -> JsonObject:
    """Bind a modified fixture member to its exact byte size and digest."""
    return {'path': path, 'size_bytes': len(files[path]), 'sha256': digest(files[path])}


@pytest.fixture(scope='module')
def interrupted_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one real SIGKILL/resume archive using the sibling producer environment."""
    supplied = os.environ.get('PRSIM_INTERRUPTED_ARCHIVE')
    if supplied:
        return Path(supplied)
    benchmark = Path(__file__).parents[2] / 'benchmark'
    interpreter = benchmark / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('requires the sibling benchmark module venv')
    exported = tmp_path_factory.mktemp('interrupted-producer')
    result = subprocess.run(
        [
            str(interpreter),
            '-m',
            'pytest',
            '-q',
            'tests/test_artifact_workflow.py::test_sigkill_recovers_only_state_owned_native_facts[native-3]',
        ],
        cwd=benchmark,
        env={
            **os.environ,
            'PYTHONHASHSEED': '42',
            'PRSIM_ARTIFACT_FIXTURE_DIR': str(exported),
        },
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return exported / 'producer.tar.gz'


def test_interrupted_history_publishes_and_refreshes(interrupted_archive: Path, tmp_path: Path) -> None:
    """The same selected task and partial all-attempt costs survive persistent refresh."""
    token = tmp_path / 'token'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock')
    imported = validate_archive(interrupted_archive, tmp_path / 'validated', Limits())
    with TestClient(create_app(config)) as client:
        service = service_for(client)
        create_user(client)
        headers = grant(client, 'publish')
        content = interrupted_archive.read_bytes()
        headers.update(
            {
                'Content-Type': 'application/gzip',
                'Idempotency-Key': str(uuid.uuid4()),
                'X-Archive-SHA256': digest(content),
                'X-Report-SHA256': imported.manifest.report_sha256,
            }
        )
        uploaded = client.put('/api/v1/runs/' + imported.report.run.id, content=content, headers=headers)
        assert uploaded.status_code == 201, uploaded.text
        receipt = uploaded.json()
        rid = receipt['run_id']
        before = client.get('/api/v1/runs/' + rid).json()
        tasks = client.get('/api/v1/runs/' + rid + '/tasks').json()
        assert imported.report.costs.all_attempts.native_reported.status == 'partial'
        assert imported.report.costs.all_attempts.native_reported.amount is None
        assert imported.report.costs.all_attempts.native_reported.coverage.total_attempts == 2
        service.refresh()
        assert client.get('/api/v1/runs/' + rid).json() == before
        assert client.get('/api/v1/runs/' + rid + '/tasks').json() == tasks


@pytest.mark.parametrize(
    'damage',
    [
        'owner',
        'boolean_version',
        'float_version',
        'unknown_version',
        'missing_marker',
        'missing_start',
        'missing_seal',
        'current',
        'unmarked',
        'downgrade',
        'native_conflict',
    ],
)
def test_interruption_cannot_hide_damaged_evidence(interrupted_archive: Path, tmp_path: Path, damage: str) -> None:
    """Recompute outer hashes so rejection proves the inner evidence contract."""
    files = _archive_files(interrupted_archive)
    state = _object(parse_json(files['_meta/state.json']))
    selected = _objects(state['tasks'])[0]
    owner = _objects(selected['history'])[0]
    marker_path = _text(_object(owner['interruption'])['path'])
    if damage in {'owner', 'boolean_version', 'float_version', 'unknown_version'}:
        marker = _object(parse_json(files[marker_path]))
        if damage == 'owner':
            marker['attempt_id'] = _object(selected['attempt'])['id']
        else:
            marker['schema_version'] = {'boolean_version': True, 'float_version': 1.0, 'unknown_version': 2}[damage]
        files[marker_path] = json_bytes(marker)
        owner['interruption'] = {
            'path': marker_path,
            'size_bytes': len(files[marker_path]),
            'sha256': digest(files[marker_path]),
        }
    elif damage == 'missing_marker':
        del files[marker_path]
    elif damage == 'missing_start':
        del files[_text(_object(owner['start'])['path'])]
    elif damage == 'missing_seal':
        del files[_text(_object(selected['seal'])['path'])]
    elif damage == 'current':
        selected['interruption'] = owner['interruption']
    elif damage == 'unmarked':
        owner.pop('interruption')
    elif damage == 'downgrade':
        state['schema_version'] = 2
    else:
        files[marker_path.replace('interruption.json', 'seal.json')] = b'{}'
    files['_meta/state.json'] = json_bytes(state)
    report = _object(parse_json(files['report.json']))
    _object(report['state'])['sha256'] = digest(files['_meta/state.json'])
    files['report.json'] = json_bytes(report)
    manifest = _object(parse_json(files['manifest.json']))
    manifest['report_sha256'] = digest(files['report.json'])
    manifest['files'] = [
        {'path': name, 'size_bytes': len(content), 'sha256': digest(content)}
        for name, content in sorted(files.items())
        if name != 'manifest.json'
    ]
    files['manifest.json'] = json_bytes(manifest)
    path = tmp_path / 'damaged.tar.gz'
    _write_archive(path, files, None)
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'rejected', Limits())


def test_sealed_interrupted_history_checks_diagnostics_without_config(
    tmp_path: Path,
) -> None:
    """Every retained receipt remains binding when effective config capture was unavailable."""
    benchmark = Path(__file__).parents[2] / 'benchmark'
    interpreter = benchmark / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('requires the sibling benchmark module venv')
    generated = tmp_path / 'producer'
    result = subprocess.run(
        [
            str(interpreter),
            '-m',
            'pytest',
            '-q',
            '--basetemp',
            str(generated),
            'tests/test_score_resume.py::test_quota_crash_after_seal_resumes_with_new_native_attempt[True]',
        ],
        cwd=benchmark,
        env={**os.environ, 'PYTHONHASHSEED': '42'},
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source = next(generated.glob('*/recovered.tar.gz'))
    files = _archive_files(source)
    _remove_historical_effective_capture(files)
    state = _object(parse_json(files['_meta/state.json']))
    owner = _objects(_objects(state['tasks'])[0]['history'])[0]
    seal_path = _text(_object(owner['seal'])['path'])
    for damaged in (False, True):
        if damaged:
            files[seal_path.replace('seal.json', 'runtime.log')] += b'changed diagnostic'
        manifest = _object(parse_json(files['manifest.json']))
        manifest['report_sha256'] = digest(files['report.json'])
        manifest['files'] = [_receipt(files, path) for path in sorted(files) if path != 'manifest.json']
        files['manifest.json'] = json_bytes(manifest)
        path = tmp_path / f'sealed-{damaged}.tar.gz'
        _write_archive(path, files, None)
        if damaged:
            with pytest.raises(ViewerError):
                validate_archive(path, tmp_path / 'rejected-seal', Limits())
        else:
            validate_archive(path, tmp_path / 'accepted-seal', Limits())


def _remove_historical_effective_capture(files: dict[str, bytes]) -> None:
    """Remove one historical config capture and rebind state, receipts and inference."""
    state = _object(parse_json(files['_meta/state.json']))
    owner = _objects(_objects(state['tasks'])[0]['history'])[0]
    seal_path = _text(_object(owner['seal'])['path'])
    seal = _object(parse_json(files[seal_path]))
    artifacts = _object(_object(seal['task'])['artifacts'])
    effective = _object(artifacts['effective_config'])['path']
    artifacts['effective_config'] = {
        'status': 'unavailable',
        'path': None,
        'message': 'capture unavailable',
    }
    seal['artifact_receipts'] = [row for row in _objects(seal['artifact_receipts']) if row['path'] != effective]
    files[seal_path] = json_bytes(seal)
    owner['seal'] = _receipt(files, seal_path)
    marker_path = _text(_object(owner['interruption'])['path'])
    marker = _object(parse_json(files[marker_path]))
    marker['seal'] = owner['seal']
    files[marker_path] = json_bytes(marker)
    owner['interruption'] = _receipt(files, marker_path)
    files['_meta/state.json'] = json_bytes(state)
    report = _object(parse_json(files['report.json']))
    _object(report['state'])['sha256'] = digest(files['_meta/state.json'])
    remaining = [
        {
            'attempt_id': item['attempt_id'],
            'source_ref': item['source_ref'],
            'config': parse_json(files[_text(item['source_ref'])]),
        }
        for item in _objects(_object(_object(report['inference'])['effective'])['attempts'])
        if item['attempt_id'] != _object(owner['attempt'])['id']
    ]
    report['inference'] = build_inference(_object(parse_json(files['config_identity.json'])), remaining)
    files['report.json'] = json_bytes(report)
