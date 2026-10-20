"""Atomic stage documents, semantic hashes and single-writer coordination."""

import fcntl
import hashlib
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TypeVar, cast

from basket_collector.artifact_io import (
    StorageError,
    _fsync_directory,
    atomic_write,
    durable_directory,
    read_json,
    sha256,
    stage_sha256,
)
from basket_collector.artifact_io import canonical_bytes as canonical_bytes
from basket_collector.artifact_io import utc_now as utc_now
from basket_collector.checkpoints import validate_checkpoint
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    InputReceipt,
    LineageReceipt,
    Producer,
    StageName,
    TasksDocument,
)
from basket_collector.publication import STAGE_FILES, PublicationStore

StageDocument = CollectDocument | FilterDocument | TasksDocument | BasketDocument
DocumentT = TypeVar('DocumentT', bound=StageDocument)

__all__ = [
    'StageDocument',
    'StageStore',
    'StorageError',
    'atomic_write',
    'canonical_bytes',
    'current_producer',
    'expected_input',
    'read_checkpoint',
    'read_document',
    'read_json',
    'receipt',
    'sha256',
    'single_writer',
    'stage_sha256',
    'utc_now',
    'verify_identity',
]


def read_document(path: Path, model: type[DocumentT]) -> DocumentT:
    """Validate a completed artifact against its document model.

    Raises:
        StorageError: The artifact is invalid or contains checkpoint state.

    """
    payload = read_json(path)
    if 'checkpoint' in payload:
        raise StorageError(f'{path}: checkpoint field in completed document')
    try:
        return model.model_validate(payload)
    except Exception as exc:
        raise StorageError(f'{path}: invalid document: {exc}') from exc


def read_checkpoint(path: Path, model: type[DocumentT]) -> tuple[DocumentT, dict[str, Any]]:
    """Validate unfinished work and return its document and resume state.

    Raises:
        StorageError: The checkpoint or document is missing or invalid.

    """
    payload = read_json(path)
    checkpoint = payload.pop('checkpoint', None)
    if not isinstance(checkpoint, dict):
        raise StorageError(f'{path}: missing checkpoint object')
    try:
        document = model.model_validate(payload)
    except Exception as exc:
        raise StorageError(f'{path}: invalid checkpoint document: {exc}') from exc
    if document.meta.status == 'completed':
        raise StorageError(f'{path}: completed document left in work area')
    try:
        checkpoint = validate_checkpoint(document, checkpoint)
    except ValueError as exc:
        raise StorageError(f'{path}: invalid checkpoint state: {exc}; use OVERWRITE=1') from exc
    return document, checkpoint


@contextmanager
def single_writer(output_dir: Path) -> Iterator[None]:
    """Hold a nonblocking exclusive lock on the collector output directory.

    Raises:
        StorageError: Another collector already holds the lock.

    """
    durable_directory(output_dir)
    path = output_dir / '.collector.lock'
    with path.open('a+b') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageError(f'another basket collector owns {output_dir}') from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def current_producer(project_dir: Path | None = None) -> Producer:
    """Capture the code, dependency, Python, and Git producer identity.

    Args:
        project_dir: Project root for tests; the installed source is used by default.

    Returns:
        A receipt describing the implementation used for stage production.

    """
    source_dir = project_dir / 'src' if project_dir is not None else Path(__file__).resolve().parent
    inventory = hashlib.sha256()
    for path in sorted(source_dir.rglob('*.py')):
        relative = path.relative_to(source_dir).as_posix().encode('utf-8')
        inventory.update(len(relative).to_bytes(4, 'big'))
        inventory.update(relative)
        data = path.read_bytes()
        inventory.update(len(data).to_bytes(8, 'big'))
        inventory.update(data)
    lock_data = (source_dir / 'requirements.lock').read_bytes()
    version_result = subprocess.run(['git', '--version'], check=True, capture_output=True, text=True, timeout=10)
    return Producer(
        version='0.2.0',
        implementation_sha256=inventory.hexdigest(),
        dependencies_sha256=hashlib.sha256(lock_data).hexdigest(),
        python_version=f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
        git_version=version_result.stdout.strip(),
    )


def receipt(document: StageDocument) -> LineageReceipt:
    """Build a lineage receipt for an upstream stage document."""
    meta = document.meta
    return LineageReceipt(
        stage=meta.stage,
        sha256=stage_sha256(document),
        settings_hash=meta.settings_hash,
        settings=meta.settings,
        producer=meta.producer,
    )


def expected_input(document: StageDocument) -> InputReceipt:
    """Build the input identity expected by a downstream stage."""
    meta = document.meta
    return InputReceipt(stage=meta.stage, sha256=stage_sha256(document))


def verify_identity(
    document: StageDocument,
    *,
    stage: str,
    settings: dict[str, Any],
    producer: Producer,
    input_receipt: InputReceipt | None,
) -> None:
    """Ensure a saved document matches the requested stage inputs.

    Raises:
        StorageError: Stage, settings, producer, or input receipt changed.

    """
    meta = document.meta
    expected_settings_hash = sha256(settings)
    mismatches: list[str] = []
    if meta.stage != stage:
        mismatches.append(f'stage {meta.stage} != {stage}')
    if meta.settings_hash != expected_settings_hash or meta.settings != settings:
        mismatches.append(f'settings {meta.settings_hash} != {expected_settings_hash}')
    if meta.producer != producer:
        mismatches.append('producer changed')
    if meta.input != input_receipt:
        mismatches.append(f'input {meta.input} != {input_receipt}')
    if mismatches:
        raise StorageError('; '.join(mismatches) + '; use OVERWRITE=1')


class StageStore:
    """Manage one stage's completed artifact and resumable work file."""

    def __init__(self, output_dir: Path, stage: str) -> None:
        """Bind a stage name to its completed and work artifact paths.

        Raises:
            ValueError: The stage name is unknown.

        """
        if stage not in STAGE_FILES:
            raise ValueError(f'unknown stage: {stage}')
        self.stage: StageName = cast(StageName, stage)
        self.publication = PublicationStore(output_dir)
        self.path = output_dir / STAGE_FILES[self.stage]
        self.work_path = output_dir / '.work' / f'{stage}.json'

    def load(
        self,
        model: type[DocumentT],
        *,
        settings: dict[str, Any],
        producer: Producer,
        input_receipt: InputReceipt | None,
        overwrite: bool = False,
        validate_loaded: Callable[[DocumentT, dict[str, Any] | None], None] | None = None,
    ) -> tuple[DocumentT | None, dict[str, Any] | None]:
        """Reuse matching completed work or return a resumable checkpoint.

        ``validate_loaded`` checks source-dependent content before promotion or
        returning saved state. Completed work is promoted before returning. With
        ``overwrite=True``, the caller starts a fresh stage without reading
        saved artifacts.

        Returns:
            The saved document and optional checkpoint state, or two ``None``
            values when no reusable work exists.

        Raises:
            StorageError: Saved state is damaged, mismatched, or not resumable.

        """
        self.publication.check_layout()
        if overwrite:
            return None, None
        if self.work_path.exists():
            payload = read_json(self.work_path)
            if payload.get('meta', {}).get('status') == 'completed':
                state = payload.pop('checkpoint', {})
                try:
                    document = model.model_validate(payload)
                except ValueError as error:
                    raise StorageError(f'invalid completed checkpoint: {error}') from error
                verify_identity(
                    document, stage=self.stage, settings=settings, producer=producer, input_receipt=input_receipt
                )
                if validate_loaded is not None:
                    validate_loaded(document, None)
                self.publication.commit(document, state)
                self.work_path.unlink()
                _fsync_directory(self.work_path.parent)
                return document, None
            document, state = read_checkpoint(self.work_path, model)
            if document.meta.error is not None and not document.meta.error.retryable:
                raise StorageError(f'{document.meta.error.code}: checkpoint requires OVERWRITE=1')
            verify_identity(
                document, stage=self.stage, settings=settings, producer=producer, input_receipt=input_receipt
            )
            if validate_loaded is not None:
                validate_loaded(document, state)
            return document, state
        payload = self.publication.load(self.stage)
        if payload is None:
            return None, None
        document = model.model_validate(payload)
        verify_identity(document, stage=self.stage, settings=settings, producer=producer, input_receipt=input_receipt)
        if document.meta.status != 'completed':
            raise StorageError('only completed stage heads may be reused')
        if validate_loaded is not None:
            validate_loaded(document, None)
        return document, None

    def prior(
        self,
        model: type[DocumentT],
        *,
        settings: dict[str, Any],
        producer: Producer,
        overwrite: bool = False,
        validate_loaded: Callable[[DocumentT, dict[str, Any] | None], None] | None = None,
    ) -> tuple[DocumentT | None, dict[str, Any] | None]:
        """Load a validated older input revision before explicitly reconciling per-record inputs."""
        self.publication.check_layout()
        if overwrite:
            return None, None
        if self.work_path.exists():
            payload = read_json(self.work_path)
        else:
            payload = self.publication.load(self.stage)
        if payload is None:
            return None, None
        raw_input = payload.get('meta', {}).get('input')
        previous_input = InputReceipt.model_validate(raw_input) if raw_input is not None else None
        return self.load(
            model, settings=settings, producer=producer, input_receipt=previous_input, validate_loaded=validate_loaded
        )

    def operation_state(self) -> dict[str, Any]:
        """Read the journal associated with the current completed stage revision."""
        return self.publication.journal(self.stage)

    def checkpoint(self, document: StageDocument, state: dict[str, Any]) -> None:
        """Persist unfinished stage output with its resume state.

        Raises:
            StorageError: The document is completed or belongs to another stage.

        """
        self.publication.check_layout()
        if document.meta.stage != self.stage or document.meta.status == 'completed':
            raise StorageError('checkpoint requires unfinished document of matching stage')
        payload = document.model_dump(mode='json')
        try:
            payload['checkpoint'] = validate_checkpoint(document, state)
        except ValueError as exc:
            raise StorageError(f'invalid checkpoint state: {exc}') from exc
        atomic_write(self.work_path, payload)

    def publish(
        self,
        document: StageDocument,
        *,
        current_input: InputReceipt | None = None,
        operation_state: dict[str, Any] | None = None,
    ) -> None:
        """Publish a completed stage after checking its current input.

        Raises:
            StorageError: Stage status or input identity is invalid.

        """
        if document.meta.stage != self.stage or document.meta.status != 'completed':
            raise StorageError('publish requires completed document of matching stage')
        if current_input is not None and document.meta.input != current_input:
            raise StorageError('input_changed: stage input changed before publication')
        self.publication.check_layout()
        state: dict[str, Any] = {}
        if self.work_path.exists():
            state = read_json(self.work_path).get('checkpoint', {})
        if operation_state is not None:
            state = validate_checkpoint(document, operation_state)
        payload = document.model_dump(mode='json')
        payload['checkpoint'] = state
        atomic_write(self.work_path, payload)
        self._promote()

    def _promote(self) -> None:
        """Commit completed work through the single authoritative publication pointer."""
        payload = read_json(self.work_path)
        state = payload.pop('checkpoint', {})
        models = {
            'collect': CollectDocument,
            'filter': FilterDocument,
            'generate': TasksDocument,
            'export': BasketDocument,
        }
        document = models[self.stage].model_validate(payload)
        self.publication.commit(document, state)
        self.work_path.unlink()
        _fsync_directory(self.work_path.parent)
