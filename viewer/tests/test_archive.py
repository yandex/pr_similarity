"""Real tar bytes at the untrusted upload boundary."""

import io
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import archive_fixture

from viewer.config import Limits
from viewer.errors import ViewerError
from viewer.util import json_bytes, parse_json, require_object
from viewer.validation.archive import validate_archive


def test_accepts_frozen_producer_schema_archive(tmp_path: Path) -> None:
    """Verify accepts frozen producer schema archive."""
    path = tmp_path / 'run.tar.gz'
    report, manifest = archive_fixture(path)
    result = validate_archive(path, tmp_path / 'data', Limits())
    assert result.report.to_wire() == report
    assert result.manifest.to_wire() == manifest
    assert result.archive_sha256


@pytest.mark.parametrize('kind', ['traversal', 'absolute', 'link', 'duplicate', 'sparse'])
def test_rejects_unsafe_members(tmp_path: Path, kind: str) -> None:
    """Verify rejects unsafe members."""
    name = {
        'traversal': '../escape',
        'absolute': '/escape',
        'link': 'link',
        'duplicate': 'report.json',
        'sparse': 'sparse',
    }[kind]
    member = tarfile.TarInfo(name)
    member.size = 1
    if kind == 'link':
        member.type = tarfile.SYMTYPE
        member.linkname = '/etc/passwd'
    if kind == 'sparse':
        member.type = tarfile.GNUTYPE_SPARSE
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path, extra=(member, io.BytesIO(b'x')))
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'data', Limits())
    assert not (tmp_path / 'escape').exists()
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize(
    'limit', ['archive_bytes', 'unpacked_bytes', 'file_bytes', 'json_bytes', 'files', 'path_bytes', 'expansion_ratio']
)
def test_resource_limits(tmp_path: Path, limit: str) -> None:
    """Verify resource limits."""
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path)
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'data', replace(Limits(), **{limit: 1}))
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('kind', ['score', 'revision', 'basket', 'reference', 'summary'])
def test_semantic_validation(tmp_path: Path, kind: str) -> None:
    """Verify semantic validation."""

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Introduce the selected inconsistency into the archive documents or files."""
        if kind == 'score':
            report['tasks'][0]['scoring']['Mf_mlp'] = 0.4
        if kind == 'revision':
            report['schema_version'] = 9
        if kind == 'basket':
            report['basket']['sha256'] = '0' * 64
        if kind == 'reference':
            del files[report['tasks'][0]['scoring']['files'][0]['human']]
        if kind == 'summary':
            report['summary']['scored_tasks'] = 0

    path = tmp_path / 'run.tar.gz'
    archive_fixture(path, change=change)
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'data', Limits())


def test_oversized_pax_header_fails_before_tarfile_allocates_payload(tmp_path: Path) -> None:
    """Verify oversized pax header fails before tarfile allocates payload."""
    member = tarfile.TarInfo('extra')
    member.pax_headers = {'comment': 'x' * (128 << 10)}
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path, extra=(member, io.BytesIO(b'')))
    with pytest.raises(ViewerError) as error:
        validate_archive(path, tmp_path / 'data', Limits())
    assert error.value.status == 413
    assert not (tmp_path / 'data').exists()


def test_public_diff_cannot_alias_logs_or_previous_attempt(tmp_path: Path) -> None:
    """Verify public diff cannot alias logs or previous attempt."""
    for target in [
        'logs_20260930T120000000000Z/33333333-3333-4333-8333-333333333333/agent.log',
        'attempts/old/old/triplets/000001/human',
    ]:

        def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
            """Point the human source reference at a private artifact."""
            row = report['tasks'][0]['scoring']['files'][0]
            files[target] = b'private log'
            row['human'] = target

        path = tmp_path / 'run.tar.gz'
        archive_fixture(path, change=change)
        with pytest.raises(ViewerError) as error:
            validate_archive(path, tmp_path / 'data', Limits())
        assert error.value.code == 'invalid_public_artifact'


def test_triplet_owner_and_state_receipts_are_verified(tmp_path: Path) -> None:
    """Verify triplet owner and state receipts are verified."""

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Bind the triplet manifest to a foreign attempt identifier."""
        key = report['tasks'][0]['artifacts']['triplets_manifest']
        index = require_object(parse_json(files[key]))
        index['attempt_id'] = '00000000-0000-4000-8000-000000000000'
        files[key] = json_bytes(index)

    path = tmp_path / 'run.tar.gz'
    archive_fixture(path, change=change)
    with pytest.raises(ViewerError) as error:
        validate_archive(path, tmp_path / 'data', Limits())
    assert error.value.code == 'invalid_triplets'
