"""Adjacent source variants preserve exact existence, ownership and immutable file receipts."""

from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.git import safe_path
from benchmark.layout import artifact_path
from benchmark.storage import atomic_write, bytes_sha256, read_json, write_json
from benchmark.triplet_paths import triplet_path
from benchmark.vcs import VCS
from benchmark.workspace import CapturedChanges, FileState, revision_states

_ROLES = ('base', 'human', 'ai')


@dataclass(frozen=True)
class TripletBundle:
    """Pair validated file states with the manifest-owned role references."""

    states: dict[str, dict[str, FileState]]
    references: dict[str, dict[str, str | None]]
    index: dict


def save_triplets(
    directory: Path,
    cache: VCS,
    before: str,
    after: str,
    captured: CapturedChanges,
    task_id: str,
    attempt_id: str,
    *,
    run_id: str | None = None,
) -> dict:
    """Freeze adjacent repository-relative source variants without fabricating absent files."""
    before_tree, after_tree = cache.tree(before), cache.tree(after)
    human_paths = {
        path for path in before_tree.keys() | after_tree.keys() if before_tree.get(path) != after_tree.get(path)
    }
    paths = sorted(human_paths | captured.states.keys())
    base, human = revision_states(cache, before, paths), revision_states(cache, after, paths)
    index = {
        'version': 1,
        'run_id': run_id,
        'task_id': task_id,
        'attempt_id': attempt_id,
        'source': {'before': before, 'after': after},
        'entries': [],
    }
    if (directory / 'index.json').exists():
        raise BenchmarkError('triplets_immutable', 'Triplet manifest already exists; preserve the original evidence.')
    for number, path in enumerate(paths, 1):
        safe_path(path)
        states = {'base': base[path], 'human': human[path], 'ai': captured.states.get(path, base[path])}
        entry = {'path': path, 'number': number, 'modes': {}, 'exists': {}, 'files': {}}
        for role, state in states.items():
            entry['modes'][role], entry['exists'][role] = state.mode, state.exists
            entry['files'][role] = None
            if state.exists:
                content = state.text.encode('utf-8')
                relative = triplet_path(number, path, role)
                target = artifact_path(directory, relative)
                if target.exists():
                    raise BenchmarkError(
                        'triplets_immutable', 'Partial triplet files exist; do not overwrite evidence.'
                    )
                atomic_write(target, content)
                entry['files'][role] = {'path': relative, 'size_bytes': len(content), 'sha256': bytes_sha256(content)}
        index['entries'].append(entry)
    write_json(directory / 'index.json', index)
    return index


def _state(directory: Path, path: str, role: str, entry: dict) -> tuple[FileState, str | None]:
    """Distinguish an absent state from a real empty file and validate its receipt."""
    exists, mode, receipt = entry['exists'][role], entry['modes'][role], entry['files'][role]
    if type(exists) is not bool:
        raise ValueError('invalid existence')
    expected = triplet_path(entry['number'], path, role)
    if not exists:
        if mode is not None or receipt is not None:
            raise ValueError('absent state contains file metadata')
        target = artifact_path(directory, expected)
        if target.exists():
            raise ValueError('absent state contains source payload')
        return FileState(False, '', None), None
    if mode not in ('100644', '100755') or not isinstance(receipt, dict):
        raise ValueError('invalid mode or receipt')
    if set(receipt) != {'path', 'size_bytes', 'sha256'} or receipt['path'] != expected:
        raise ValueError('unexpected artifact path')
    artifact = artifact_path(directory, expected, must_exist=True)
    content = artifact.read_bytes()
    if type(receipt['size_bytes']) is not int or receipt['size_bytes'] < 0:
        raise ValueError('invalid size')
    if len(content) != receipt['size_bytes'] or bytes_sha256(content) != receipt['sha256']:
        raise ValueError('artifact digest mismatch')
    return FileState(True, content.decode('utf-8'), mode), expected


def read_triplets(directory: Path, *, owner: dict | None = None, run_id: str | None = None) -> TripletBundle:
    """Read immutable states and their references from the same validated index bytes."""
    index = read_json(artifact_path(directory, 'index.json', must_exist=True))
    if (
        set(index) != {'version', 'run_id', 'task_id', 'attempt_id', 'source', 'entries'}
        or type(index['version']) is not int
        or index['version'] != 1
    ):
        raise BenchmarkError('invalid_triplets', 'Triplet index has an incompatible schema-1 structure.', exit_code=1)
    if owner is not None:
        validate_triplet_owner(index, owner, run_id=run_id)
    states: dict[str, dict[str, FileState]] = {}
    references: dict[str, dict[str, str | None]] = {}
    try:
        paths = [entry['path'] for entry in index['entries']]
        if paths != sorted(set(paths)):
            raise ValueError('paths are not sorted and unique')
        for number, entry in enumerate(index['entries'], 1):
            if set(entry) != {'path', 'number', 'modes', 'exists', 'files'}:
                raise ValueError('unsupported entry fields')
            path = safe_path(entry['path'])
            if type(entry['number']) is not int or entry['number'] != number:
                raise ValueError('invalid file number')
            if any(set(entry[key]) != set(_ROLES) for key in ('exists', 'modes', 'files')):
                raise ValueError('invalid role set')
            states[path], references[path] = {}, {}
            for role in _ROLES:
                state, reference = _state(directory, path, role, entry)
                states[path][role], references[path][role] = state, reference
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as error:
        raise BenchmarkError(
            'invalid_triplets', 'Triplet states or receipts are missing or inconsistent.', exit_code=1
        ) from error
    return TripletBundle(states, references, index)


def load_triplets(
    directory: Path, *, owner: dict | None = None, run_id: str | None = None
) -> dict[str, dict[str, FileState]]:
    """Return validated immutable file states for offline consumers."""
    return read_triplets(directory, owner=owner, run_id=run_id).states


def validate_triplet_owner(index: dict, row: dict, *, run_id: str | None = None) -> None:
    """Bind a manifest to the exact task, native attempt and frozen revisions."""
    attempt = row['final_attempt']
    if (
        attempt is None
        or index.get('task_id') != row['id']
        or index.get('attempt_id') != attempt['id']
        or index.get('source') != {key: row[key] for key in ('before', 'after')}
        or (run_id is not None and index.get('run_id') != run_id)
    ):
        raise BenchmarkError(
            'triplet_owner_mismatch', 'Triplets belong to another run, task, attempt or revision.', exit_code=1
        )
