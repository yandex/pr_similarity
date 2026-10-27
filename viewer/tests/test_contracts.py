"""Malformed uploads fail safely even when their manifest is rebuilt."""

import gzip
import io
import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import archive_fixture

from viewer.config import Config, Limits
from viewer.errors import ViewerError
from viewer.models import JsonObject, JsonValue
from viewer.models.evidence import ArtifactReceipt
from viewer.models.report import ReportAgentInput
from viewer.util import digest, parse_json, safe_path
from viewer.validation.agent_input import validate_agent_input
from viewer.validation.archive import validate_archive


@pytest.mark.parametrize(
    'raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}', b'{"a":-1e999}', b'\xff']
)
def test_strict_json(raw: bytes) -> None:
    """Verify strict json."""
    with pytest.raises(ViewerError):
        parse_json(raw)


@pytest.mark.parametrize('path', ['../foo', '/foo', 'a/../b', 'a\\b', 'a//b', 'a/./b', 'a\x00b'])
def test_path_normalization_is_not_implicit(path: str) -> None:
    """Verify path normalization is not implicit."""
    with pytest.raises(ViewerError):
        safe_path(path)


def test_hash_damage_detected(tmp_path: Path) -> None:
    """Verify hash damage detected."""
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path)
    changed = tmp_path / 'changed.tar.gz'
    with tarfile.open(path, 'r:gz') as source, tarfile.open(changed, 'w:gz') as target:
        for member in source:
            stream = source.extractfile(member)
            assert stream is not None
            data = stream.read()
            if member.name == 'report.json':
                data = data.replace(b'baseline', b'badagent')
            member.size = len(data)
            target.addfile(member, io.BytesIO(data))
    with pytest.raises(ViewerError, match='inconsistent'):
        validate_archive(changed, tmp_path / 'data', Limits())


def test_gzip_trailing_bomb_is_counted(tmp_path: Path) -> None:
    """Verify gzip trailing bomb is counted."""
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path)
    with path.open('ab') as stream:
        stream.write(gzip.compress(b'X' * (2 << 20)))
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'data', replace(Limits(), unpacked_bytes=1 << 20))


def test_duplicate_json_rejected_with_valid_inventory(tmp_path: Path) -> None:
    """Verify duplicate json rejected with valid inventory."""
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path, change=lambda report, basket, files: files.update({'config_identity.json': b'{"x":1,"x":2}'}))
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'data', Limits())


def test_config_rejects_unknown_fields_and_credentials_in_url(tmp_path: Path) -> None:
    """Verify config rejects unknown fields and credentials in url."""
    p = tmp_path / 'config.json'
    p.write_text(json.dumps({'storage': 'mock', 'bogus': 1}))
    with pytest.raises(ViewerError):
        Config.load(p)
    with pytest.raises(ViewerError):
        Config(storage='s3', bucket='bucket', endpoint='https://a:b@host')
    with pytest.raises(ViewerError):
        Limits(files=-1)


@pytest.mark.parametrize('endpoint', [None, '', False, 0])
def test_config_json_keeps_legacy_optional_values(tmp_path: Path, endpoint: object) -> None:
    """Decoding keeps the accepted optional values without string coercion."""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'storage': 'mock', 'endpoint': endpoint, 'limits': {'files': 7}}))
    config = Config.load(path)
    assert config.endpoint == endpoint and type(config.endpoint) is type(endpoint)
    assert config.limits.files == 7
    assert config.limits.file_bytes == Limits().file_bytes
    assert replace(config, port=8001).port == 8001


@pytest.mark.parametrize(
    ('changes', 'message'),
    [
        ({'unknown': True, 'limits': None}, 'Unknown configuration field.'),
        ({'limits': None, 'host': ''}, 'Invalid or unknown limit field.'),
        ({'limits': {'files': 0}, 'runtime': None}, 'Limits must be positive integers.'),
        ({'runtime': None, 'host': ''}, 'Runtime and token paths must be valid local paths.'),
        ({'host': '', 'port': 0}, 'Invalid bind host.'),
        ({'port': 0, 'session_ttl_seconds': 0}, 'Invalid server port or grant TTL.'),
    ],
)
def test_config_json_preserves_diagnostic_precedence(tmp_path: Path, changes: dict[str, object], message: str) -> None:
    """Multiple invalid fields retain the established public validation order."""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'storage': 'mock', **changes}))
    with pytest.raises(ViewerError) as raised:
        Config.load(path)
    assert raised.value.code == 'invalid_config'
    assert raised.value.message == message


@pytest.mark.parametrize(
    ('field', 'value', 'error_type', 'message'),
    [
        ('setup', False, TypeError, '\'bool\' object is not subscriptable'),
        ('setup', 1.0, TypeError, '\'float\' object is not subscriptable'),
        ('setup', '', TypeError, 'string indices must be integers, not \'str\''),
        ('setup', [], TypeError, 'list indices must be integers or slices, not str'),
        ('setup', {}, KeyError, '\'sha256\''),
        ('setup', {'extension': True}, KeyError, '\'sha256\''),
        ('resources', [False], TypeError, '\'bool\' object is not subscriptable'),
        ('resources', [{}], KeyError, '\'field\''),
        ('resources', [{'field': 'instructions'}], KeyError, '\'index\''),
        ('resources', [{'field': [], 'index': 0}], TypeError, 'unhashable type: \'list\''),
    ],
)
def test_identity_members_retain_original_failure_boundary(
    field: str, value: JsonValue, error_type: type[Exception], message: str
) -> None:
    """Typed identity construction must not turn legacy raw failures into new 422s."""
    files = {'agent/opencode.json': b'{}', 'agent/task_instructions.txt': b''}
    inventory = {
        path: ArtifactReceipt(path=path, size_bytes=len(content), sha256=digest(content))
        for path, content in files.items()
    }
    selected = ReportAgentInput.model_validate(
        {
            'config': inventory['agent/opencode.json'].to_wire(),
            'resources': [inventory['agent/task_instructions.txt'].to_wire()],
        }
    )
    identity: JsonObject = {
        'native_config': {},
        'instructions': {'sha256': digest(b'')},
        'resources': [],
        'setup': None,
    }
    identity[field] = value
    with pytest.raises(error_type) as raised:
        validate_agent_input(selected, identity, inventory, files.__getitem__)
    assert str(raised.value) == message
