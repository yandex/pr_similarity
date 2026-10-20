"""Atomic stage documents, semantic hashes and single-writer coordination."""

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypeVar, cast

from basket_collector.checkpoints import validate_checkpoint
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    InputReceipt,
    JsonValue,
    LineageReceipt,
    Producer,
    TasksDocument,
    settings_sha256,
)
from pydantic import BaseModel

StageDocument = CollectDocument | FilterDocument | TasksDocument | BasketDocument
DocumentT = TypeVar('DocumentT', bound=StageDocument)
STAGE_FILES = {
    'collect': 'collected.json',
    'filter': 'filtered.json',
    'generate': 'tasks.json',
    'export': 'basket.json',
}
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
    """Hash data using the canonical settings hash for dictionaries."""
    if isinstance(value, dict):
        return settings_sha256(cast(dict[str, JsonValue], value))
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


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Durably replace a JSON artifact via a same-directory temporary file.

    The file and containing directory are synced before returning.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
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


@contextmanager
def single_writer(output_dir: Path) -> Iterator[None]:
    """Hold a nonblocking exclusive lock on the collector output directory.

    Raises:
        StorageError: Another collector already holds the lock.

    """
    output_dir.mkdir(parents=True, exist_ok=True)
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
        version='0.1.1',
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
        self.stage = stage
        self.path = output_dir / STAGE_FILES[stage]
        self.work_path = output_dir / '.work' / f'{stage}.json'

    def load(
        self,
        model: type[DocumentT],
        *,
        settings: dict[str, Any],
        producer: Producer,
        input_receipt: InputReceipt | None,
        overwrite: bool = False,
        allow_empty_retry: bool = False,
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
        if overwrite:
            return None, None
        if self.work_path.exists():
            payload = read_json(self.work_path)
            if 'checkpoint' not in payload:
                try:
                    completed = model.model_validate(payload)
                except Exception as exc:
                    raise StorageError(f'{self.work_path}: invalid completed work: {exc}') from exc
                if completed.meta.status != 'completed':
                    raise StorageError(f'{self.work_path}: work without checkpoint is not completed')
                verify_identity(
                    completed,
                    stage=self.stage,
                    settings=settings,
                    producer=producer,
                    input_receipt=input_receipt,
                )
                if validate_loaded is not None:
                    validate_loaded(completed, None)
                self._promote()
                return completed, None
            document, checkpoint = read_checkpoint(self.work_path, model)
            verify_identity(
                document,
                stage=self.stage,
                settings=settings,
                producer=producer,
                input_receipt=input_receipt,
            )
            if validate_loaded is not None:
                validate_loaded(document, checkpoint)
            recover_empty = (
                allow_empty_retry
                and self.stage == 'generate'
                and document.meta.error is not None
                and document.meta.error.code == 'empty_stage_output'
            )
            if document.meta.error is not None and not document.meta.error.retryable and not recover_empty:
                raise StorageError(
                    f'{self.stage}: {document.meta.error.code}: '
                    f'{document.meta.error.message}; rebuild source and use OVERWRITE=1'
                )
            return document, checkpoint
        if self.path.exists():
            document = read_document(self.path, model)
            verify_identity(
                document,
                stage=self.stage,
                settings=settings,
                producer=producer,
                input_receipt=input_receipt,
            )
            if document.meta.status != 'completed':
                raise StorageError(f'{self.path}: only completed results may be reused')
            if validate_loaded is not None:
                validate_loaded(document, None)
            return document, None
        return None, None

    def checkpoint(self, document: StageDocument, state: dict[str, Any]) -> None:
        """Persist unfinished stage output with its resume state.

        Raises:
            StorageError: The document is completed or belongs to another stage.

        """
        if document.meta.stage != self.stage or document.meta.status == 'completed':
            raise StorageError('checkpoint requires unfinished document of matching stage')
        payload = document.model_dump(mode='json')
        try:
            payload['checkpoint'] = validate_checkpoint(document, state)
        except ValueError as exc:
            raise StorageError(f'invalid checkpoint state: {exc}') from exc
        atomic_write(self.work_path, payload)

    def publish(self, document: StageDocument, *, current_input: InputReceipt | None = None) -> None:
        """Publish a completed stage after checking its current input.

        Raises:
            StorageError: Stage status or input identity is invalid.

        """
        if document.meta.stage != self.stage or document.meta.status != 'completed':
            raise StorageError('publish requires completed document of matching stage')
        if current_input is not None and document.meta.input != current_input:
            raise StorageError('input_changed: stage input changed before publication')
        atomic_write(self.work_path, document.model_dump(mode='json'))
        self._promote()

    def _promote(self) -> None:
        """Move completed work into place and sync both directories."""
        os.replace(self.work_path, self.path)
        _fsync_directory(self.work_path.parent)
        _fsync_directory(self.path.parent)
