"""Durability, malformed input and single-writer behaviour."""

import json
import multiprocessing
import threading
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.persistence import storage
from benchmark.persistence.storage import (
    atomic_write,
    canonical_bytes,
    read_json,
    run_lock,
    sha256,
    write_json,
)
from benchmark.validation.contracts import validate_document


def _hold_lock(path: str, channel: Connection) -> None:
    """Hold a real lock in a spawned process until released by the parent."""
    with storage.file_lock(Path(path)):
        channel.send('ready')
        assert channel.poll(10)
        channel.recv()


@pytest.mark.parametrize('terminate', [False, True])
def test_wait_forever_process_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminate: bool) -> None:
    """Wait across processes and preserve the inode even if the owner dies."""
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    path = tmp_path / 'cache.lock'
    owner = context.Process(target=_hold_lock, args=(str(path), child))
    owner.start()
    cancel = threading.Event()
    entered = threading.Event()
    blocked = threading.Event()
    errors = []
    native = storage.fcntl.flock

    def flock(fd: int, operation: int) -> None:
        """Signal contention while preserving the native lock operation."""
        try:
            native(fd, operation)
        except BlockingIOError:
            blocked.set()
            raise

    monkeypatch.setattr(storage.fcntl, 'flock', flock)

    def waiter() -> None:
        """Acquire the lock in a thread and report failures to the parent test."""
        try:
            with storage.file_lock(path, wait_forever=True, cancel=cancel):
                entered.set()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=waiter)
    try:
        assert parent.poll(10)
        assert parent.recv() == 'ready'
        inode = path.stat().st_ino
        worker.start()
        assert blocked.wait(5)
        assert not entered.is_set()
        with storage.file_lock(tmp_path / 'other.lock'):
            pass
        if terminate:
            owner.terminate()
        else:
            parent.send('release')
        owner.join(10)
        worker.join(10)
        assert not worker.is_alive()
        assert not errors
        assert entered.is_set()
        assert path.stat().st_ino == inode
    finally:
        cancel.set()
        if worker.ident is not None:
            worker.join(10)
        if owner.is_alive():
            owner.kill()
        owner.join(10)
        parent.close()
        child.close()


@pytest.mark.parametrize('when', ['before', 'waiting', 'acquired'])
def test_wait_forever_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    """Cancel at each acquisition boundary without entering the protected body."""
    path = tmp_path / 'cache.lock'
    cancel = threading.Event()
    native = storage.fcntl.flock
    calls = []

    def flock(fd: int, operation: int) -> None:
        """Inject cancellation at the selected lock acquisition boundary."""
        calls.append(fd)
        if when == 'waiting':
            cancel.set()
            raise BlockingIOError()
        native(fd, operation)
        if when == 'acquired':
            cancel.set()

    if when == 'before':
        cancel.set()
    with monkeypatch.context() as patch:
        patch.setattr(storage.fcntl, 'flock', flock)
        with pytest.raises(BenchmarkError) as caught:
            with storage.file_lock(path, wait_forever=True, cancel=cancel):
                pytest.fail('cancelled waiter entered body')
        assert caught.value.code == 'operation_cancelled'
        assert caught.value.exit_code == 130
    for fd in calls:
        with pytest.raises(OSError):
            storage.os.fstat(fd)
    with storage.file_lock(path):
        pass


def test_wait_forever_rejects_deadline_before_open(tmp_path: Path) -> None:
    """Reject ambiguous policies without creating a lock file."""
    path = tmp_path / 'invalid.lock'
    with pytest.raises(ValueError):
        with storage.file_lock(path, timeout=1, wait_forever=True):
            pytest.fail('invalid policy entered body')
    assert not path.exists()


def test_bounded_lock_still_times_out(tmp_path: Path) -> None:
    """Installation locks retain their finite timeout and error classification."""
    path = tmp_path / 'bounded.lock'
    with storage.file_lock(path):
        with pytest.raises(BenchmarkError) as caught:
            with storage.file_lock(path, timeout=0):
                pytest.fail('busy lock acquired')
        assert caught.value.code == 'cache_locked'


def test_wait_forever_body_error_and_symlink(tmp_path: Path) -> None:
    """Preserve body exceptions and refuse symlink locks in the new mode."""
    path = tmp_path / 'cache.lock'
    with pytest.raises(OSError, match='body failure'):
        with storage.file_lock(path, wait_forever=True):
            raise OSError('body failure')
    with storage.file_lock(path):
        pass
    link = tmp_path / 'link.lock'
    link.symlink_to(path)
    with pytest.raises(BenchmarkError) as caught:
        with storage.file_lock(link, wait_forever=True):
            pytest.fail('symlink accepted')
    assert caught.value.code == 'lock_failed'


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
    permission = restored['permission']
    assert isinstance(permission, dict)
    bash = permission['bash']
    assert isinstance(bash, dict)
    assert list(bash) == ['rm *', '*']


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
    permission = read_json(path)['permission']
    assert isinstance(permission, dict)
    bash = permission['bash']
    assert isinstance(bash, dict)
    assert list(bash) == ['rm *', '*']
