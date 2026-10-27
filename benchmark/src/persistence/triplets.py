"""Adjacent source variants preserve exact existence, ownership and immutable file receipts."""

from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.models.base import ArtifactReceipt
from benchmark.models.report import ReportFileExists, ReportTask, TaskDraft
from benchmark.models.triplets import (
    TripletEntry,
    TripletFiles,
    TripletIndex,
    TripletModes,
    TripletReferences,
    TripletSource,
    TripletStates,
)
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.storage import (
    atomic_write,
    bytes_sha256,
    read_json,
    write_json,
)
from benchmark.validation.triplet_paths import triplet_path
from benchmark.vcs.base import VCS
from benchmark.vcs.git import safe_path
from benchmark.vcs.workspace import CapturedChanges, FileState, revision_states


@dataclass(frozen=True)
class TripletBundle:
    """Pair validated file states with the manifest-owned role references."""

    states: dict[str, TripletStates]
    references: dict[str, TripletReferences]
    index: TripletIndex


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
) -> TripletIndex:
    """Freeze adjacent repository-relative source variants without fabricating absent files."""
    before_tree, after_tree = cache.tree(before), cache.tree(after)
    human_paths = {
        path for path in before_tree.keys() | after_tree.keys() if before_tree.get(path) != after_tree.get(path)
    }
    paths = sorted(human_paths | captured.states.keys())
    base, human = revision_states(cache, before, paths), revision_states(cache, after, paths)
    index = TripletIndex(
        version=1,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        source=TripletSource(before=before, after=after),
        entries=[],
    )
    if (directory / 'index.json').exists():
        raise BenchmarkError('triplets_immutable', 'Triplet manifest already exists; preserve the original evidence.')
    for number, path in enumerate(paths, 1):
        safe_path(path)
        ai = captured.states.get(path, base[path])
        receipts: dict[str, ArtifactReceipt | None] = {}
        for role, state in (('base', base[path]), ('human', human[path]), ('ai', ai)):
            receipts[role] = None
            if state.exists:
                content = state.text.encode('utf-8')
                relative = triplet_path(number, path, role)
                target = artifact_path(directory, relative)
                if target.exists():
                    raise BenchmarkError(
                        'triplets_immutable', 'Partial triplet files exist; do not overwrite evidence.'
                    )
                atomic_write(target, content)
                receipts[role] = ArtifactReceipt(path=relative, size_bytes=len(content), sha256=bytes_sha256(content))
        index.entries.append(
            TripletEntry(
                path=path,
                number=number,
                modes=TripletModes(base=base[path].mode, human=human[path].mode, ai=ai.mode),
                exists=ReportFileExists(base=base[path].exists, human=human[path].exists, ai=ai.exists),
                files=TripletFiles(base=receipts['base'], human=receipts['human'], ai=receipts['ai']),
            )
        )
    write_json(directory / 'index.json', index)
    return index


def _state(
    directory: Path, path: str, role: str, number: int, exists: bool, mode: str | None, receipt: ArtifactReceipt | None
) -> tuple[FileState, str | None]:
    """Distinguish an absent state from a real empty file and validate its receipt."""
    expected = triplet_path(number, path, role)
    if not exists:
        if mode is not None or receipt is not None:
            raise ValueError('absent state contains file metadata')
        if artifact_path(directory, expected).exists():
            raise ValueError('absent state contains source payload')
        return FileState(False, '', None), None
    if mode not in ('100644', '100755') or receipt is None:
        raise ValueError('invalid mode or receipt')
    if set(receipt.to_wire()) != {'path', 'size_bytes', 'sha256'} or receipt.path != expected:
        raise ValueError('unexpected artifact path')
    content = artifact_path(directory, expected, must_exist=True).read_bytes()
    if type(receipt.size_bytes) is not int or receipt.size_bytes < 0:
        raise ValueError('invalid size')
    if len(content) != receipt.size_bytes or bytes_sha256(content) != receipt.sha256:
        raise ValueError('artifact digest mismatch')
    return FileState(True, content.decode('utf-8'), mode), expected


def read_triplets(
    directory: Path, *, owner: ReportTask | TaskDraft | None = None, run_id: str | None = None
) -> TripletBundle:
    """Read immutable states and their references from the same validated index bytes."""
    raw = read_json(artifact_path(directory, 'index.json', must_exist=True))
    if (
        set(raw) != {'version', 'run_id', 'task_id', 'attempt_id', 'source', 'entries'}
        or type(raw['version']) is not int
        or raw['version'] != 1
    ):
        raise BenchmarkError('invalid_triplets', 'Triplet index has an incompatible schema-1 structure.', exit_code=1)
    states: dict[str, TripletStates] = {}
    references: dict[str, TripletReferences] = {}
    try:
        index = TripletIndex.model_validate(raw)
        if owner is not None:
            validate_triplet_owner(index, owner, run_id=run_id)
        paths = [entry.path for entry in index.entries]
        if paths != sorted(set(paths)):
            raise ValueError('paths are not sorted and unique')
        for number, entry in enumerate(index.entries, 1):
            if set(entry.to_wire()) != {'path', 'number', 'modes', 'exists', 'files'}:
                raise ValueError('unsupported entry fields')
            path = safe_path(entry.path)
            if type(entry.number) is not int or entry.number != number:
                raise ValueError('invalid file number')
            if any(
                set(group.to_wire()) != {'base', 'human', 'ai'} for group in (entry.exists, entry.modes, entry.files)
            ):
                raise ValueError('invalid role set')
            base, base_ref = _state(
                directory, path, 'base', number, entry.exists.base, entry.modes.base, entry.files.base
            )
            human, human_ref = _state(
                directory, path, 'human', number, entry.exists.human, entry.modes.human, entry.files.human
            )
            ai, ai_ref = _state(directory, path, 'ai', number, entry.exists.ai, entry.modes.ai, entry.files.ai)
            states[path] = TripletStates(base, human, ai)
            references[path] = TripletReferences(base=base_ref, human=human_ref, ai=ai_ref)
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as error:
        raise BenchmarkError(
            'invalid_triplets', 'Triplet states or receipts are missing or inconsistent.', exit_code=1
        ) from error
    return TripletBundle(states, references, index)


def load_triplets(
    directory: Path, *, owner: ReportTask | TaskDraft | None = None, run_id: str | None = None
) -> dict[str, TripletStates]:
    """Return validated immutable file states for offline consumers."""
    return read_triplets(directory, owner=owner, run_id=run_id).states


def validate_triplet_owner(index: TripletIndex, row: ReportTask | TaskDraft, *, run_id: str | None = None) -> None:
    """Bind a manifest to the exact task, native attempt and frozen revisions."""
    attempt = row.final_attempt
    if (
        attempt is None
        or index.task_id != row.id
        or index.attempt_id != attempt.id
        or index.source != TripletSource(before=row.before, after=row.after)
        or (run_id is not None and index.run_id != run_id)
    ):
        raise BenchmarkError(
            'triplet_owner_mismatch', 'Triplets belong to another run, task, attempt or revision.', exit_code=1
        )
