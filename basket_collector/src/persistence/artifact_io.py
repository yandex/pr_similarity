"""Strict JSON encoding, semantic digests and durable atomic file writes."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_TIMING_FIELDS = ('started_at', 'updated_at', 'completed_at')


class StorageError(ValueError):
    """An artifact is damaged or belongs to another input/configuration."""


def utc_now() -> str:
    """Return the current UTC time as an RFC 3339 string ending in ``Z``."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _plain(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic document to JSON-compatible data if needed."""
    return value.model_dump(mode='json') if isinstance(value, BaseModel) else value


def canonical_bytes(value: object) -> bytes:
    """Encode data as deterministic, compact UTF-8 JSON bytes."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(value: object) -> str:
    """Hash data using the canonical JSON encoding shared with settings."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stage_sha256(document: BaseModel | dict[str, Any]) -> str:
    """Hash a stage document without timing or checkpoint state.

    The resulting digest identifies semantic stage content across resumes.
    """
    payload = _plain(document)
    payload = dict(payload)
    meta = dict(payload['meta'])
    for key in _TIMING_FIELDS:
        meta.pop(key, None)
    payload['meta'] = meta
    payload.pop('checkpoint', None)
    return sha256(payload)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StorageError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    """Read a strict JSON object from a stage artifact.

    Raises:
        StorageError: The file is unreadable, malformed, or not an object.

    """
    try:
        payload = json.loads(
            path.read_text(encoding='utf-8'),
            object_pairs_hook=_unique_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(StorageError(f'non-finite JSON number: {value}')),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f'cannot read {path}: {exc}') from exc
    if not isinstance(payload, dict):
        raise StorageError(f'{path}: expected JSON object')
    return payload


def durable_directory(path: Path) -> None:
    """Persist newly created directory entries before acknowledging files within them."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _fsync_directory(directory.parent)


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Durably replace a JSON artifact via a same-directory temporary file.

    The file and containing directory are synced before returning.
    """
    durable_directory(path.parent)
    encoded = artifact_bytes(payload)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='wb', prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent, delete=False
        ) as stream:
            temporary_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Sync directory metadata after an atomic rename."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def artifact_bytes(payload: dict[str, Any]) -> bytes:
    """Encode immutable artifacts exactly as the atomic writer persists them."""
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
