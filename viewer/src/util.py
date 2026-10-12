"""Strict JSON, bounded paths and deterministic content identity."""

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import PurePosixPath

from viewer.errors import ViewerError


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def parse_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate object key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('Non-finite JSON value')
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, float) and not math.isfinite(item): raise ValueError('Non-finite JSON value')
            if isinstance(item, str): item.encode('utf-8')
            elif isinstance(item, dict): pending.extend(item.keys()); pending.extend(item.values())
            elif isinstance(item, list): pending.extend(item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ViewerError('invalid_json', 'Invalid JSON document.') from None


def safe_path(value: str, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ViewerError('unsafe_path', 'Unsafe artifact path.')
    try:
        encoded = value.encode('utf-8')
    except UnicodeError:
        raise ViewerError('unsafe_path', 'Unsafe artifact path.') from None
    path = PurePosixPath(value)
    if len(encoded) > max_bytes or path.is_absolute() or '..' in value.split('/') or path.as_posix() != value:
        raise ViewerError('unsafe_path', 'Unsafe artifact path.')
    return value


def utc_timestamp(value: str) -> datetime:
    try:
        if not isinstance(value, str) or not value.endswith('Z'):
            raise ValueError('UTC required')
        return datetime.fromisoformat(value)
    except ValueError:
        raise ViewerError('invalid_timestamp', 'Expected a UTC timestamp.') from None


def utc_now(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace('+00:00', 'Z') if timestamp is not None else datetime.now(UTC).isoformat().replace('+00:00', 'Z')
