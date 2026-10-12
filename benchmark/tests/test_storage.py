"""Durability, malformed input and single-writer behaviour."""

import json
from pathlib import Path

import pytest
from benchmark import storage
from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError
from benchmark.storage import (
    atomic_write,
    canonical_bytes,
    read_json,
    run_lock,
    sha256,
    write_json,
)


@pytest.mark.parametrize('text', ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '[]', 'null'])
def test_malformed_json_is_rejected(tmp_path: Path, text: str) -> None:
    """Verify malformed json is rejected."""
    path = tmp_path / 'report.json'
    path.write_text(text)
    with pytest.raises(BenchmarkError):
        read_json(path)


def test_unicode_identity_is_canonical() -> None:
    """Verify unicode identity is canonical."""
    assert canonical_bytes({'b': 2, 'a': 'Привет'}) == canonical_bytes({'a': 'Привет', 'b': 2})
    assert sha256({'a': 1}) != sha256({'a': True})
    with pytest.raises((ValueError, BenchmarkError)):
        canonical_bytes({'a': float('inf')})


def test_atomic_failure_keeps_old_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify atomic failure keeps old document."""
    path = tmp_path / 'report.json'
    atomic_write(path, b'{"revision":1}')

    def fail(*_args: object) -> None:
        """Inject a filesystem replace error while preserving the old document."""
        raise OSError('simulated replace failure')

    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(BenchmarkError):
        atomic_write(path, b'{"revision":2}')
    assert read_json(path)['revision'] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.json']


def test_lock_conflict_and_independent_runs(tmp_path: Path) -> None:
    """Verify lock conflict and independent runs."""
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    with run_lock(a):
        with pytest.raises(BenchmarkError) as caught, run_lock(a):
            raise AssertionError('must not enter second writer')
        assert caught.value.code == 'run_locked'
        with run_lock(b):
            pass
    with run_lock(a):
        pass


def test_schema_validation_does_not_show_payload_value() -> None:
    """Verify schema validation does not show payload value."""
    with pytest.raises(BenchmarkError) as caught:
        validate_document('config', {'schema_version': 'SECRET_BAD_VALUE'})
    assert 'SECRET_BAD_VALUE' not in str(caught.value)


def test_artifact_read_write_preserves_exact_bytes(tmp_path: Path) -> None:
    """Verify artifact read write preserves exact bytes."""
    path = tmp_path / 'nested' / 'receipt.json'
    data = {'revision': 1, 'text': 'Пример'}
    encoded = canonical_bytes(data)
    atomic_write(path, encoded)
    assert path.read_bytes() == encoded
    assert read_json(path) == data
    assert json.loads(encoded) == data


def test_lock_does_not_reclassify_body_failure(tmp_path: Path) -> None:
    """Verify lock does not reclassify body failure."""
    with pytest.raises(OSError, match='body failure'), run_lock(tmp_path):
        raise OSError('body failure')
    with run_lock(tmp_path):
        pass


def test_native_snapshot_preserves_mapping_rule_order(tmp_path: Path) -> None:
    """Verify native snapshot preserves mapping rule order."""
    native = {'permission': {'bash': {'rm *': 'deny', '*': 'allow'}}}
    path = tmp_path / 'config.json'
    write_json(path, native, preserve_order=True)
    restored = read_json(path)
    assert list(restored['permission']['bash']) == ['rm *', '*']


def test_document_writer_is_pretty_without_changing_semantic_identity(tmp_path: Path) -> None:
    """Every generated document is readable while identity ignores formatting."""
    data = {'permission': {'bash': {'rm *': 'deny', '*': 'allow'}}, 'text': 'Пример'}
    path = tmp_path / 'receipt.json'
    before = sha256(data)
    write_json(path, data)
    expected = (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
    assert path.read_bytes() == expected
    assert sha256(read_json(path)) == before
    assert storage.document_bytes(data) == expected
    assert list(read_json(path)['permission']['bash']) == ['rm *', '*']
