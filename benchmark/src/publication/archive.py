"""Archive exact RunStore snapshots without rediscovering experiment state."""

import os
import stat
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.models import ArchiveManifest, Report
from benchmark.models.archive import ArchiveManifestMissingArtifactItem
from benchmark.models.evidence import ArtifactReceipt, Outcome, Seal, Start
from benchmark.models.run import RunTaskItem
from benchmark.models.state import StateAttempt, StateTaskItem, StateTaskItemHistoryItem
from benchmark.persistence.artifact_refs import read_reference
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.operations import validate_operation
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import (
    RunLease,
    bytes_sha256,
    document_bytes,
    parse_json,
    run_lock,
    utc_now,
    write_json,
)
from benchmark.validation.contracts import validate_document
from benchmark.vcs.git import safe_path


@dataclass(frozen=True)
class Archive:
    """Describe one verified immutable archive and its inventory."""

    path: Path
    sha256: str
    manifest: ArchiveManifest


def _missing(items: list[ArchiveManifestMissingArtifactItem], path: str | None, reason: str) -> None:
    """Record a required absence once without replacing evidence with a guess."""
    row = ArchiveManifestMissingArtifactItem(path=path, reason=reason, required=True)
    if row not in items:
        items.append(row)


def _attempts(selection: StateTaskItem) -> list[StateTaskItem | StateTaskItemHistoryItem]:
    """Return historical then current selections in their persisted sequence order."""
    values: list[StateTaskItem | StateTaskItemHistoryItem] = list(selection.history)
    if selection.attempt is not None:
        values.append(selection)
    return values


def _private(relative: str) -> bool:
    """Reject host/private namespaces while allowing role-owned source filenames."""
    parts = relative.split('/')
    if parts[:1] in (['.work'], ['home'], ['source']):
        return True
    basename = parts[-1]
    if basename not in {'.env', 'auth.json', 'opencode.db', 'opencode.db-wal', 'opencode.db-shm'}:
        return False
    # Source variants have role suffixes, so these exact names always denote private state.
    return True


def _publisher_payload(relative: str) -> bool:
    """Exclude only the self-referential immutable payload frozen outside the run."""
    parts = relative.split('/')
    if len(parts) != 4 or parts[:2] != ['operations', 'publish'] or parts[-1] != 'payload.tar.gz':
        return False
    try:
        return str(uuid.UUID(parts[2])) == parts[2]
    except ValueError:
        return False


def _inventory(run_dir: Path) -> list[ArtifactReceipt]:
    """Collect regular files only, never following links or exporting private state."""
    result = []
    for path in sorted(run_dir.rglob('*')):
        relative = path.relative_to(run_dir).as_posix()
        if relative in {'.lock', 'manifest.json'} or _publisher_payload(relative):
            continue
        if path.is_symlink():
            raise BenchmarkError('archive_unsafe', 'Run contains a symlink; archive creation refused.', exit_code=1)
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise BenchmarkError(
                'archive_unsafe', 'Run contains a special device or FIFO; archive creation refused.', exit_code=1
            )
        safe_path(relative)
        if _private(relative):
            raise BenchmarkError(
                'archive_private_state', 'Private task state or credentials must not be stored in the run.', exit_code=1
            )
        content = path.read_bytes()
        result.append(ArtifactReceipt(path=relative, size_bytes=len(content), sha256=bytes_sha256(content)))
    return sorted(result, key=lambda entry: entry.path)


def _require_file(run_dir: Path, relative: str, missing: list[ArchiveManifestMissingArtifactItem], reason: str) -> bool:
    """Validate a safe required artifact without deriving a path from a hash or mtime."""
    try:
        artifact_path(run_dir, relative, must_exist=True)
    except BenchmarkError:
        _missing(missing, relative, reason)
        return False
    return True


def _available_host_score(store: RunStore, task_id: str, item: StateTaskItem | StateTaskItemHistoryItem) -> bool:
    """Accept an absent native outcome only when state selected an exact available host score."""
    selected = item.scoring
    if selected is None:
        return False
    operation = validate_operation(store.directory, store.registry, selected, 'rescore')
    assert item.attempt is not None
    matches = [
        row for row in operation.result.tasks or [] if row.task_id == task_id and row.attempt_id == item.attempt.id
    ]
    if len(matches) != 1:
        raise BenchmarkError(
            'archive_identity_mismatch',
            'Selected host score has no unique native task/attempt owner.',
            exit_code=1,
        )
    return matches[0].scoring.status == 'available'


def _validate_selections(store: RunStore, missing: list[ArchiveManifestMissingArtifactItem]) -> None:
    """Require every state-selected attempt's explicit immutable ownership facts."""
    for selected in store.state.tasks:
        registration = store.task(selected.id)
        for item in _attempts(selected):
            attempt = item.attempt
            assert attempt is not None
            prefix = store.attempt_directory(selected.id, attempt.sequence).as_posix()
            for kind in ('start', 'seal'):
                receipt = item.start if kind == 'start' else item.seal
                expected = prefix + '/' + kind + '.json'
                if receipt is None:
                    _missing(missing, expected, f'State selection has no committed {kind} receipt.')
                    continue
                if receipt.path != expected:
                    raise BenchmarkError(
                        'archive_identity_mismatch',
                        'Selected native receipt has an unexpected owner path.',
                        exit_code=1,
                    )
                try:
                    value = read_reference(store.directory, receipt, Start if kind == 'start' else Seal)
                except BenchmarkError:
                    _missing(missing, expected, f'Selected {kind} receipt is unavailable or damaged.')
                    continue
                if isinstance(value, Seal):
                    _validate_seal(store, registration, attempt, value, missing)
            outcome = item.outcome
            expected = prefix + '/outcome.json'
            if outcome is None:
                if not _available_host_score(store, selected.id, item):
                    _missing(
                        missing,
                        expected,
                        'Attempt has neither a committed native outcome nor an available selected host score.',
                    )
                continue
            if outcome.path != expected:
                raise BenchmarkError(
                    'archive_identity_mismatch',
                    'Selected native outcome has an unexpected owner path.',
                    exit_code=1,
                )
            try:
                value = read_reference(store.directory, outcome, Outcome)
            except BenchmarkError:
                _missing(missing, expected, 'Selected outcome receipt is unavailable or damaged.')
                continue
            if value.seal != item.seal:
                raise BenchmarkError(
                    'archive_identity_mismatch',
                    'Selected outcome does not reference its selected seal.',
                    exit_code=1,
                )


def _validate_seal(
    store: RunStore,
    registration: RunTaskItem,
    attempt: StateAttempt,
    seal: Seal,
    missing: list[ArchiveManifestMissingArtifactItem],
) -> None:
    """Check the explicit native evidence allowlist signed by one selected seal."""
    prefix = store.attempt_directory(registration.id, attempt.sequence).as_posix() + '/'
    if seal.schema_family != 'pr_similarity_os.seal' or seal.task.id != registration.id:
        raise BenchmarkError('archive_identity_mismatch', 'Selected seal belongs to another task.', exit_code=1)
    receipts = seal.artifact_receipts
    shared = store.registry.agent_input
    allowed = [shared.config, *shared.resources]
    if store.state.python_runtime is not None:
        allowed.append(store.state.python_runtime)
    effective = seal.task.artifacts.effective_config
    if effective.status == 'saved':
        found = next((receipt for receipt in receipts if receipt.path == effective.path), None)
        if found is not None:
            allowed.append(found)
    paths: set[str] = set()
    for receipt in receipts:
        if receipt.path in paths or (not receipt.path.startswith(prefix) and receipt not in allowed):
            raise BenchmarkError(
                'archive_identity_mismatch', 'Seal inventory escapes or duplicates native ownership.', exit_code=1
            )
        paths.add(receipt.path)
        try:
            path = artifact_path(store.directory, receipt.path, must_exist=True)
            content = path.read_bytes()
            if (
                type(receipt.size_bytes) is not int
                or len(content) != receipt.size_bytes
                or bytes_sha256(content) != receipt.sha256
            ):
                raise BenchmarkError('artifact_digest_mismatch', 'Sealed evidence digest differs.')
        except BenchmarkError:
            _missing(missing, receipt.path, 'Sealed native evidence is unavailable or damaged.')
    evidence = seal.evidence
    if evidence is None:
        _missing(missing, prefix + 'seal.json', 'Seal has no structured evidence status.')
        return
    required = evidence.required
    if required is None or required.status != 'saved':
        for name in required.missing if required is not None else []:
            _missing(missing, prefix + str(name), 'Required sealed evidence is missing.')
        if required is None or not required.missing:
            _missing(missing, prefix + 'seal.json', 'Seal marks required native evidence incomplete.')
    streams = evidence.streams
    if streams is None or streams.status != 'saved':
        _missing(missing, prefix + 'stream_receipt.json', 'Raw native stream capture is incomplete.')
    usage = evidence.usage
    if usage is None or usage.archive_complete is not True:
        _missing(missing, prefix + 'telemetry/receipt.json', 'Usage is unknown or incomplete for this sealed attempt.')
    if seal.archive_complete is not True:
        _missing(missing, prefix + 'seal.json', 'Seal declares incomplete required evidence.')


def _validate_snapshot(run_dir: Path, store: RunStore, report: bytes) -> Report:
    """Bind a report to the exact authoritative state bytes, never to a stale cache."""
    expected = document_bytes(store.snapshot())
    if report != expected:
        raise BenchmarkError('stale_report', 'Report does not equal the current authoritative run state.', exit_code=1)
    value = Report.model_validate(parse_json(report))
    if value.state.path != '_meta/state.json' or value.state.sha256 != bytes_sha256(
        (run_dir / '_meta/state.json').read_bytes()
    ):
        raise BenchmarkError(
            'archive_identity_mismatch', 'Report state receipt differs from authoritative state bytes.', exit_code=1
        )
    if value.run.status in ('running', 'stopping'):
        raise BenchmarkError('run_active', 'Archive creation requires a stopped run.', exit_code=1)
    return value


def build_manifest(run_dir: Path, *, store: RunStore | None = None) -> ArchiveManifest:
    """Build a read-only manifest from registry/state-selected facts and regular files."""
    store = RunStore.open(run_dir) if store is None else store
    store.validate()
    report_path = artifact_path(run_dir, 'report.json', must_exist=True)
    report_bytes = report_path.read_bytes()
    report = _validate_snapshot(run_dir, store, report_bytes)
    missing: list[ArchiveManifestMissingArtifactItem] = []
    for relative in (
        'run.json',
        '_meta/state.json',
        '_meta/runtime.json',
        '_meta/sandbox.json',
        '_meta/python.json',
        'basket.json',
        'config_identity.json',
    ):
        _require_file(run_dir, relative, missing, 'Required run registry, state or provenance is missing.')
    _validate_selections(store, missing)
    manifest = ArchiveManifest(
        schema_family='pr_similarity_os.archive',
        schema_version=2,
        run_id=report.run.id,
        created_at=utc_now(),
        report_revision=report.run.revision,
        report_sha256=bytes_sha256(report_bytes),
        complete=not any(row.required for row in missing),
        missing_artifacts=missing,
        files=_inventory(run_dir),
    )
    validate_document('manifest', manifest)
    return manifest


def _validate_locked(run_dir: Path, store: RunStore, lease: RunLease) -> None:
    """Reject a stale or foreign lease instead of taking a nested writer lock."""
    if (
        not lease.active
        or lease.directory != run_dir.resolve()
        or store.directory.resolve() != run_dir.resolve()
        or store.lease is not lease
    ):
        raise BenchmarkError('writer_lease_expired', 'Archive creation requires the active lease for this run store.')


def create_archive_locked(
    run_dir: Path,
    destination: Path,
    *,
    store: RunStore,
    lease: RunLease,
    require_complete: bool = True,
) -> Archive:
    """Repair the derived report under one existing writer lease, then snapshot it."""
    run_dir = run_dir.resolve()
    destination = destination.resolve()
    _validate_locked(run_dir, store, lease)
    if destination.is_relative_to(run_dir):
        raise BenchmarkError('archive_recursion', 'Archive destination must be outside its source run.')
    store.rebuild()
    manifest = build_manifest(run_dir, store=store)
    write_json(run_dir / 'manifest.json', manifest)
    if require_complete and not manifest.complete:
        raise BenchmarkError(
            'archive_incomplete', 'Required run artifacts are missing; publish refused. See manifest.json.', exit_code=1
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='archive-', suffix='.tar.gz', dir=destination.parent)
    os.close(descriptor)
    staging = Path(name)
    try:
        with tarfile.open(staging, 'w:gz') as output:
            for entry in manifest.files:
                output.add(run_dir / entry.path, arcname=entry.path, recursive=False)
            output.add(run_dir / 'manifest.json', arcname='manifest.json', recursive=False)
        verify_archive(staging)
        with staging.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(staging, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    data = destination.read_bytes()
    return Archive(destination, bytes_sha256(data), manifest)


def create_archive(run_dir: Path, destination: Path, *, require_complete: bool = True) -> Archive:
    """Acquire the only writer lease once for a standalone archive operation."""
    run_dir = run_dir.expanduser().resolve()
    with run_lock(run_dir) as lease:
        store = RunStore.open(run_dir, lease=lease)
        return create_archive_locked(run_dir, destination, store=store, lease=lease, require_complete=require_complete)


def verify_archive(path: Path) -> ArchiveManifest:
    """Verify tar members, exact inventory and registry/state ownership without execution."""
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if len(names) != len(set(names)) or 'manifest.json' not in names:
            raise BenchmarkError('archive_invalid', 'Archive has duplicate members or no manifest.', exit_code=1)
        for item in members:
            safe_path(item.name)
            if not item.isfile():
                raise BenchmarkError('archive_unsafe', 'Archive members must be regular files.', exit_code=1)
        stream = archive.extractfile('manifest.json')
        if stream is None:
            raise BenchmarkError('archive_invalid', 'Archive manifest cannot be read.', exit_code=1)
        document = parse_json(stream.read())
        validate_document('manifest', document)
        manifest = ArchiveManifest.model_validate(document)
        if manifest.complete != (not any(item.required for item in manifest.missing_artifacts)):
            raise BenchmarkError(
                'archive_invalid', 'Archive completeness contradicts its missing artifacts.', exit_code=1
            )
        declared = {entry.path: entry for entry in manifest.files}
        if (
            list(declared) != sorted(declared)
            or len(declared) != len(manifest.files)
            or set(names) != set(declared) | {'manifest.json'}
        ):
            raise BenchmarkError('archive_invalid', 'Archive inventory does not match its members.', exit_code=1)
        with tempfile.TemporaryDirectory(prefix='benchmark-archive-verify-') as temporary:
            root = Path(temporary)
            for name, entry in declared.items():
                stream = archive.extractfile(name)
                if stream is None:
                    raise BenchmarkError('archive_invalid', 'Archive member cannot be read.', exit_code=1)
                content = stream.read()
                if len(content) != entry.size_bytes or bytes_sha256(content) != entry.sha256:
                    raise BenchmarkError('archive_digest_mismatch', 'Archive member checksum differs.', exit_code=1)
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            report = artifact_path(root, 'report.json', must_exist=True).read_bytes()
            # A prior publication payload is intentionally omitted to avoid
            # recursive archives; RunStore still validates its request/receipt
            # metadata and fingerprint. Local replay keeps the strict default.
            store = RunStore.open(root, allow_omitted_publish_payloads=True)
            _validate_snapshot(root, store, report)
            rebuilt = build_manifest(root, store=store)
            if (
                rebuilt.run_id != manifest.run_id
                or rebuilt.report_revision != manifest.report_revision
                or rebuilt.report_sha256 != manifest.report_sha256
                or rebuilt.complete != manifest.complete
                or rebuilt.missing_artifacts != manifest.missing_artifacts
                or rebuilt.files != manifest.files
            ):
                raise BenchmarkError(
                    'archive_identity_mismatch', 'Archive facts differ from its registry/state snapshot.', exit_code=1
                )
    return manifest
