"""State-selected offline operations bind results to exact input snapshots."""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from benchmark.artifact_refs import ArtifactReference, read_reference
from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError
from benchmark.layout import artifact_path
from benchmark.result import validate_report
from benchmark.storage import bytes_sha256


class OperationReference(TypedDict):
    """Select one immutable result/receipt pair at its authoritative commit revision."""

    id: str
    kind: str
    accepted_revision: int
    result: ArtifactReference
    receipt: ArtifactReference


@dataclass(frozen=True)
class ValidatedOperation:
    """Expose verified result and provenance without filesystem latest heuristics."""

    result: dict
    receipt: dict
    inputs: dict


def operation_identifier(value: str) -> str:
    """Require one canonical UUID before constructing any operation filesystem path."""
    try:
        identifier = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise BenchmarkError('invalid_operation_reference', 'Operation UUID is invalid.') from error
    if value != identifier:
        raise BenchmarkError('invalid_operation_reference', 'Operation UUID must use its canonical representation.')
    return identifier


def validate_operation(
    directory: Path, registry: dict, selection: OperationReference | dict, kind: str
) -> ValidatedOperation:
    """Validate scope, input ownership, closed receipt and exact selected result bytes."""
    if set(selection) != {'id', 'kind', 'accepted_revision', 'result', 'receipt'}:
        raise BenchmarkError('invalid_operation_reference', 'Selected operation has unsupported fields.')
    identifier = operation_identifier(selection['id'])
    if kind not in ('rescore', 'reprice') or selection['kind'] != kind:
        raise BenchmarkError('operation_owner_mismatch', 'Selected result has an unexpected operation kind.')
    prefix = f'operations/{kind}/{identifier}/'
    if selection['result']['path'] != prefix + 'result.json' or selection['receipt']['path'] != prefix + 'receipt.json':
        raise BenchmarkError('operation_owner_mismatch', 'Selected result or receipt has an unexpected owner path.')
    result = read_reference(directory, selection['result'])
    receipt = read_reference(directory, selection['receipt'])
    _validate_owner(result, registry, identifier, kind, 'operation_result')
    _validate_owner(receipt, registry, identifier, kind, 'operation_receipt')
    if receipt['status'] not in ('completed', 'completed_with_gaps') or receipt['result'] != selection['result']:
        raise BenchmarkError('operation_not_complete', 'Selected operation has no matching closed result receipt.')
    if receipt['input_state']['path'] != prefix + 'input_state.json':
        raise BenchmarkError('operation_owner_mismatch', 'Operation input state has an unexpected path.')
    inputs = read_reference(directory, receipt['input_state'])
    validate_document('state', inputs)
    registry_hash = bytes_sha256(artifact_path(directory, 'run.json', must_exist=True).read_bytes())
    if inputs['run_id'] != registry['id'] or inputs['registry_sha256'] != registry_hash:
        raise BenchmarkError('operation_owner_mismatch', 'Operation inputs belong to another frozen run.')
    if type(selection['accepted_revision']) is not int or selection['accepted_revision'] != inputs['revision'] + 1:
        raise BenchmarkError('operation_commit_mismatch', 'Selected operation commit revision differs from its inputs.')
    if receipt['input_report']['path'] != prefix + 'input_report.json':
        raise BenchmarkError('operation_owner_mismatch', 'Operation input report has an unexpected path.')
    report = read_reference(directory, receipt['input_report'])
    validate_report(report)
    if (
        report['run']['id'] != registry['id']
        or report['run']['revision'] != inputs['revision']
        or report['state'] != {'path': '_meta/state.json', 'sha256': receipt['input_state']['sha256']}
        or report['basket']['sha256'] != registry['baseline']['basket']['sha256']
        or report['config']['identity_sha256'] != registry['baseline']['config']['identity_sha256']
    ):
        raise BenchmarkError('operation_owner_mismatch', 'Operation input report differs from its selected state.')
    _validate_items(result, inputs, kind)
    return ValidatedOperation(result, receipt, inputs)


def _validate_owner(value: dict, registry: dict, identifier: str, kind: str, family: str) -> None:
    """Bind operation metadata to frozen experiment identities and its UUID."""
    baseline = registry['baseline']
    if (
        value.get('schema_family') != 'pr_similarity_os.' + family
        or type(value.get('schema_version')) is not int
        or value['schema_version'] != 1
        or value.get('id') != identifier
        or value.get('kind') != kind
        or value.get('run_id') != registry['id']
        or value.get('basket_sha256') != baseline['basket']['sha256']
        or value.get('config_identity_sha256') != baseline['config']['identity_sha256']
    ):
        raise BenchmarkError('operation_owner_mismatch', 'Operation belongs to another run, kind or input identity.')


def _validate_items(result: dict, inputs: dict, kind: str) -> None:
    """Reject duplicated, stale or foreign task/attempt results inside an operation."""
    rows = result.get('tasks' if kind == 'rescore' else 'attempts')
    if not isinstance(rows, list):
        raise BenchmarkError('invalid_operation_result', 'Operation result must contain an explicit item list.')
    allowed = {}
    for task in inputs['tasks']:
        attempts = [entry['attempt'] for entry in task['history']]
        if task['attempt'] is not None:
            attempts.append(task['attempt'])
        for attempt in attempts:
            if kind == 'reprice' or attempt == task['attempt']:
                allowed[(task['id'], attempt['id'])] = True
    seen = set()
    for row in rows:
        key = (row['task_id'], row['attempt_id'])
        if key in seen or key not in allowed:
            raise BenchmarkError(
                'operation_owner_mismatch', 'Operation result contains duplicated or foreign attempts.'
            )
        seen.add(key)
