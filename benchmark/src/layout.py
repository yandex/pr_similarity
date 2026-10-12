"""Stable human-readable paths with explicit run, task and attempt ownership."""

import re
import stat
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from benchmark.errors import BenchmarkError
from benchmark.git import safe_path
from benchmark.storage import bytes_sha256


class TaskRegistration(TypedDict):
    """Bind a frozen task to a stable ordinal and readable directory."""

    id: str
    ordinal: int
    key: str
    path: str
    basket_row: dict


def slug(value: str, *, limit: int = 72) -> str:
    """Produce a bounded ASCII label rather than treating user text as a path."""
    ascii_text = unicodedata.normalize('NFKD', value).encode('ascii', errors='ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-')[:limit].rstrip('-') or 'agent'


def run_name(label: str, run_id: str, created_at: str) -> str:
    """Name a new run using UTC time, its display label and a UUID suffix."""
    try:
        identifier = uuid.UUID(run_id)
        stamp = datetime.fromisoformat(created_at.replace('Z', '+00:00')).astimezone(UTC)
    except (ValueError, TypeError) as error:
        raise BenchmarkError('invalid_run_identity', 'Run ID or creation timestamp is invalid.') from error
    return stamp.strftime('%Y%m%dT%H%M%SZ') + '-' + slug(label, limit=48) + '-' + identifier.hex[:8]


def _digest_widths(rows: list[dict]) -> dict[str, int]:
    """Extend colliding short digest suffixes without depending on filesystem order."""
    digests = {row['id']: bytes_sha256(row['id'].encode()) for row in rows}
    if len(digests) != len(rows) or len(set(digests.values())) != len(rows):
        raise BenchmarkError('duplicate_task_identity', 'Task identities or their content digests collide.')
    widths = {}
    for identifier, digest in digests.items():
        width = 8
        while any(other != identifier and value[:width] == digest[:width] for other, value in digests.items()):
            width += 4
        widths[identifier] = width
    return widths


def task_registry(rows: list[dict], repository: dict) -> list[TaskRegistration]:
    """Freeze numbered task paths independently of attempt scheduling and source renames."""
    widths = _digest_widths(rows)
    provider = repository['provider']
    host = '' if provider == 'github' else (urlsplit(repository['server_url']).netloc + '-')
    result: list[TaskRegistration] = []
    for ordinal, row in enumerate(rows, 1):
        change = row['change_request']
        kind = 'pr' if change['kind'] == 'pull_request' else 'mr'
        label = slug(f'{provider}-{host}{change['repository']}-{kind}-{change['number']}')
        suffix = bytes_sha256(row['id'].encode())[: widths[row['id']]]
        key = f'{ordinal:03d}-{label}-{suffix}'
        result.append({'id': row['id'], 'ordinal': ordinal, 'key': key, 'path': 'tasks/' + key, 'basket_row': row})
    return result


def attempt_path(task: TaskRegistration | dict, sequence: int) -> Path:
    """Derive a numbered path only from a registered task and a positive sequence."""
    if type(sequence) is not int or sequence < 1:
        raise BenchmarkError('invalid_attempt_sequence', 'Attempt sequence must be a positive integer.')
    return Path(safe_path(task['path'])) / 'attempts' / f'{sequence:03d}'


def artifact_path(root: Path, reference: str, *, must_exist: bool = False) -> Path:
    """Resolve a safe run reference without traversing any symlink or special file.

    Args:
        root: Owned run or triplet root.
        reference: Relative POSIX artifact path, including future log locations.
        must_exist: Require a complete regular file instead of a future location.

    Returns:
        The validated path within the unchanged root.

    Raises:
        BenchmarkError: If ownership boundaries, filesystem kind or availability fail.

    """
    relative = Path(safe_path(reference))
    target = root / relative
    try:
        if root.is_symlink():
            raise ValueError('root symlink')
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('artifact ancestor symlink')
            if current.exists():
                mode = current.lstat().st_mode
                if current == target:
                    if not stat.S_ISREG(mode):
                        raise ValueError('artifact is not a regular file')
                elif not stat.S_ISDIR(mode):
                    raise ValueError('artifact parent is not a directory')
        if must_exist and not target.is_file():
            raise ValueError('artifact missing')
    except (OSError, ValueError) as error:
        raise BenchmarkError(
            'artifact_unsafe', 'Artifact path is missing, unsafe or outside its owned directory.'
        ) from error
    return target
