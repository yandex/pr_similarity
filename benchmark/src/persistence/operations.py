"""State-selected typed operations bind immutable results to exact input snapshots."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.models import Run, State
from benchmark.models.evidence import (
    OperationOwner,
    OperationReceipt,
    OperationResult,
    OperationSelection,
    PriceItem,
    ScoreItem,
)
from benchmark.persistence.artifact_refs import read_reference, read_reference_json
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.storage import bytes_sha256
from benchmark.presentation.result import validate_report
from benchmark.validation.contracts import validate_document

OperationReference = OperationSelection


@dataclass(frozen=True)
class ValidatedOperation:
    """Expose a verified result and its exact selected input state."""

    result: OperationResult
    receipt: OperationReceipt
    inputs: State


def operation_identifier(value: str) -> str:
    """Require one canonical UUID before constructing operation filesystem paths."""
    try:
        identifier = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise BenchmarkError('invalid_operation_reference', 'Operation UUID is invalid.') from error
    if value != identifier:
        raise BenchmarkError('invalid_operation_reference', 'Operation UUID must use its canonical representation.')
    return identifier


def validate_operation(directory: Path, registry: Run, selection: OperationSelection, kind: str) -> ValidatedOperation:
    """Validate scope, byte ownership, closed receipt and exact selected result."""
    if selection.model_extra:
        raise BenchmarkError('invalid_operation_reference', 'Selected operation has unsupported fields.')
    identifier = operation_identifier(selection.id)
    if kind not in ('rescore', 'reprice') or selection.kind != kind:
        raise BenchmarkError('operation_owner_mismatch', 'Selected result has an unexpected operation kind.')
    prefix = f'operations/{kind}/{identifier}/'
    if selection.result.path != prefix + 'result.json' or selection.receipt.path != prefix + 'receipt.json':
        raise BenchmarkError('operation_owner_mismatch', 'Selected result or receipt has an unexpected owner path.')
    result = read_reference(directory, selection.result, OperationResult)
    receipt = read_reference(directory, selection.receipt, OperationReceipt)
    _validate_owner(result, registry, identifier, kind, 'operation_result')
    _validate_owner(receipt, registry, identifier, kind, 'operation_receipt')
    if receipt.status not in ('completed', 'completed_with_gaps') or receipt.result != selection.result:
        raise BenchmarkError('operation_not_complete', 'Selected operation has no matching closed result receipt.')
    if receipt.input_state.path != prefix + 'input_state.json':
        raise BenchmarkError('operation_owner_mismatch', 'Operation input state has an unexpected path.')
    raw_inputs = read_reference_json(directory, receipt.input_state)
    validate_document('state', raw_inputs)
    inputs = State.model_validate(raw_inputs)
    registry_hash = bytes_sha256(artifact_path(directory, 'run.json', must_exist=True).read_bytes())
    if inputs.run_id != registry.id or inputs.registry_sha256 != registry_hash:
        raise BenchmarkError('operation_owner_mismatch', 'Operation inputs belong to another frozen run.')
    if type(selection.accepted_revision) is not int or selection.accepted_revision != inputs.revision + 1:
        raise BenchmarkError('operation_commit_mismatch', 'Selected operation commit revision differs from its inputs.')
    if receipt.input_report.path != prefix + 'input_report.json':
        raise BenchmarkError('operation_owner_mismatch', 'Operation input report has an unexpected path.')
    report = validate_report(read_reference_json(directory, receipt.input_report))
    if (
        report.run.id != registry.id
        or report.run.revision != inputs.revision
        or report.state.path != '_meta/state.json'
        or report.state.sha256 != receipt.input_state.sha256
        or report.basket.sha256 != registry.baseline.basket.sha256
        or report.config.identity_sha256 != registry.baseline.config.identity_sha256
    ):
        raise BenchmarkError('operation_owner_mismatch', 'Operation input report differs from its selected state.')
    _validate_items(result, inputs, kind)
    return ValidatedOperation(result, receipt, inputs)


def _validate_owner(value: OperationOwner, registry: Run, identifier: str, kind: str, family: str) -> None:
    """Bind operation metadata to the frozen experiment and its canonical UUID."""
    baseline = registry.baseline
    if (
        value.schema_family != 'pr_similarity_os.' + family
        or type(value.schema_version) is not int
        or value.schema_version != 2
        or value.id != identifier
        or value.kind != kind
        or value.run_id != registry.id
        or value.basket_sha256 != baseline.basket.sha256
        or value.config_identity_sha256 != baseline.config.identity_sha256
    ):
        raise BenchmarkError('operation_owner_mismatch', 'Operation belongs to another run, kind or input identity.')


def _validate_items(result: OperationResult, inputs: State, kind: str) -> None:
    """Reject duplicated, stale or foreign native owners in an offline result."""
    rows: Sequence[ScoreItem | PriceItem] | None = result.tasks if kind == 'rescore' else result.attempts
    if rows is None:
        raise BenchmarkError('invalid_operation_result', 'Operation result must contain an explicit item list.')
    allowed: set[tuple[str, str]] = set()
    for task in inputs.tasks:
        attempts = [entry.attempt for entry in task.history]
        if task.attempt is not None:
            attempts.append(task.attempt)
        for attempt in attempts:
            if kind == 'reprice' or attempt == task.attempt:
                allowed.add((task.id, attempt.id))
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = row.task_id, row.attempt_id
        if key in seen or key not in allowed:
            raise BenchmarkError(
                'operation_owner_mismatch', 'Operation result contains duplicated or foreign attempts.'
            )
        seen.add(key)
