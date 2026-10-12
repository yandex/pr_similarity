"""Strict artifacts, content digests and durable single-writer operations."""

import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from benchmark.errors import BenchmarkError


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp."""
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def canonical_bytes(value: object) -> bytes:
    """Encode identity without whitespace, unstable key order or nonfinite floats."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(value: object) -> str:
    """Hash canonical JSON."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def bytes_sha256(value: bytes) -> str:
    """Hash exact artifact bytes."""
    return hashlib.sha256(value).hexdigest()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate JSON keys while preserving decoded values."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkError('invalid_json', 'JSON contains a duplicate key; repair the artifact.')
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Reject nonfinite JSON numeric constants."""
    raise BenchmarkError('invalid_json', 'JSON contains a nonfinite number; repair the artifact.')


def parse_json(content: bytes) -> dict:
    """Decode strict frozen bytes without echoing malformed contents."""
    try:
        value = json.loads(content, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise BenchmarkError('invalid_json', 'Cannot read the JSON artifact; check its path and format.') from exc
    if not isinstance(value, dict):
        raise BenchmarkError('invalid_json', 'JSON artifact must contain an object.')
    return value


def read_json(path: Path) -> dict:
    """Read and validate an artifact once."""
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError('invalid_json', 'Cannot read the JSON artifact; check its path and permissions.') from exc
    return parse_json(content)


def atomic_write(path: Path, value: bytes) -> None:
    """Fsync a private temporary file, replace atomically, then fsync its directory."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise BenchmarkError(
            'artifact_write_failed', 'Cannot save an artifact; check free space and permissions.', exit_code=1
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def document_bytes(value: object) -> bytes:
    """Encode readable JSON without changing native rule order or list values."""
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def write_json(path: Path, value: dict, *, preserve_order: bool = False) -> None:
    """Atomically write every generated document with uniform readable formatting."""
    atomic_write(path, document_bytes(value))


@contextmanager
def file_lock(path: Path, *, timeout: float | None = None, cancel: threading.Event | None = None) -> Iterator[None]:
    """Keep a stable inode; run writers fail fast, caches may wait with cancellation."""
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise BenchmarkError('lock_failed', 'Cannot acquire the directory lock; check path and permissions.') from exc
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise BenchmarkError('operation_cancelled', 'Cache preparation was cancelled.', exit_code=130)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if deadline is None:
                    raise BenchmarkError(
                        'run_locked', 'Another process is using this directory; wait for it to finish.'
                    ) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BenchmarkError(
                        'cache_locked', 'Another process is still preparing this cache.', exit_code=1
                    ) from exc
                delay = min(remaining, 0.1)
                if cancel is not None:
                    cancel.wait(delay)
                else:
                    time.sleep(delay)
        yield
    finally:
        if fd is not None:
            os.close(fd)


@dataclass
class RunLease:
    """Represent the lifetime and directory of an acquired run writer lock."""

    directory: Path
    active: bool = False


@contextmanager
def run_lock(run_dir: Path) -> Iterator[RunLease]:
    """Coordinate all writers and expose a lease to nested store commits."""
    with file_lock(run_dir / '.lock'):
        lease = RunLease(run_dir.resolve(), active=True)
        try:
            yield lease
        finally:
            lease.active = False
