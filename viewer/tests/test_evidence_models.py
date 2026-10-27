"""Native evidence models preserve immutable JSON and nullable states."""

import pytest
from helpers import _artifact_files, _native_facts, documents
from pydantic import ValidationError

from viewer.errors import ViewerError
from viewer.models.evidence import (
    ArtifactReceipt,
    ConfigIdentity,
    NativeTask,
    OperationInputState,
    OperationResult,
    Outcome,
    PriceResult,
    PublishRequest,
    ScoringEvidence,
    Seal,
    Start,
    TripletIndex,
    parse_evidence,
)
from viewer.util import json_bytes, parse_json


def test_native_evidence_roundtrips_without_new_optional_fields() -> None:
    """Parsing typed task facts must not change their hashed wire representation."""
    report, _, basket_bytes = documents()
    files = _artifact_files(report, basket_bytes)
    task = report['tasks'][0]
    _native_facts(report, task, files)
    prefix = f"{task['directory']}/attempts/{task['final_attempt']['sequence']:03d}/"
    for name, model in [
        ('start', Start),
        ('seal', Seal),
        ('scoring', ScoringEvidence),
        ('outcome', Outcome),
    ]:
        value = parse_json(files[prefix + name + '.json'])
        parsed = parse_evidence(model, value)
        assert json_bytes(parsed.to_wire()) == json_bytes(value)
    seal = parse_evidence(Seal, parse_json(files[prefix + 'seal.json']))
    assert seal.task.final_attempt is not None
    assert seal.task.final_attempt.id == task['final_attempt']['id']
    assert 'native_timing' not in seal.to_wire()


def test_config_identity_preserves_absence_null_and_numbers() -> None:
    """An open provider tree stays JSON while known identity fields are objects."""
    value = {
        'instructions': {'sha256': 'a' * 64},
        'setup': None,
        'resources': [
            {
                'field': 'instructions',
                'index': 0,
                'files': [{'path': '', 'sha256': 'b' * 64}],
            }
        ],
        'native_config': {
            'model': 'provider/model',
            'provider': {'temperature': 1.0, 'seed': 1},
        },
        'timeout_seconds': 30.0,
    }
    identity = ConfigIdentity.model_validate(value)
    assert identity.instructions is not None
    assert identity.resources is not None
    assert identity.instructions.sha256 == 'a' * 64
    assert identity.resources[0].field == 'instructions'
    assert json_bytes(identity.to_wire()) == json_bytes(value)
    assert 'runtime' not in identity.to_wire()
    assert identity.to_wire()['setup'] is None


def test_operation_preserves_absence_and_decimal_precision() -> None:
    """Typed operations must not synthesize result kinds or round decimal money."""
    value = {
        'schema_family': 'pr_similarity_os.operation_result',
        'schema_version': 2,
        'id': 'operation',
        'kind': 'reprice',
        'run_id': 'run',
        'basket_sha256': 'a' * 64,
        'config_identity_sha256': 'b' * 64,
        'metric': None,
        'attempts': [],
    }
    result = OperationResult.model_validate(value)
    assert result.tasks is None
    assert result.to_wire() == value
    decimal = {
        'status': 'complete',
        'amount': '0.123456789012345678901',
        'known_subtotal': '0.123456789012345678901',
        'priced_requests': 1,
        'total_requests': 1,
        'requests': [{'id': 'request', 'amount': '0.123456789012345678901', 'reason': None}],
    }
    assert PriceResult.model_validate(decimal).to_wire() == decimal


@pytest.mark.parametrize('size', [True, 0.0, '0'])
def test_receipt_does_not_coerce_identity_fields(size: object) -> None:
    """Native integer receipt checks must reject booleans and numeric strings."""
    with pytest.raises(ValidationError):
        ArtifactReceipt.model_validate({'path': 'file', 'size_bytes': size, 'sha256': 'a' * 64})


def test_evidence_parse_uses_caller_error_code() -> None:
    """Malformed evidence retains the caller's public failure category."""
    with pytest.raises(ViewerError) as caught:
        parse_evidence(TripletIndex, [], 'invalid_triplets')
    assert caught.value.code == 'invalid_triplets'


def test_inference_identity_accepts_snapshot_without_harness_fields() -> None:
    """The standalone inference API historically accepts native-only identities."""
    value = {'identity_version': 1, 'native_config': {'model': 'provider/model'}}
    assert ConfigIdentity.model_validate(value).to_wire() == value


def test_native_task_preserves_missing_unused_presentation_fields() -> None:
    """Native validation must not require public-only display and progress fields."""
    report, _, _ = documents()
    task = report['tasks'][0]
    for field in (
        'title',
        'url',
        'ordinal',
        'directory',
        'processing_status',
        'progress',
        'locations',
        'scoring',
    ):
        del task[field]
    task['telemetry'] = {'api_equivalent': None}
    native = NativeTask.model_validate(task)
    assert native.to_wire() == task
    assert native.telemetry.to_wire() == {'api_equivalent': None}


@pytest.mark.parametrize('version', [1, 1.0, True])
def test_publish_legacy_numeric_equality_and_omitted_payload(
    version: int | float | bool,
) -> None:
    """Legacy publish evidence compares versions and permits omitted payload bytes."""
    value = {
        'schema_family': 'pr_similarity_os.publish_request',
        'schema_version': version,
        'run_id': 'run',
        'id': 'operation',
        'basket_sha256': 'a' * 64,
        'config_identity_sha256': 'b' * 64,
        'payload': {'path': 'operations/publish/operation/payload.tar.gz'},
    }
    assert json_bytes(PublishRequest.model_validate(value).to_wire()) == json_bytes(value)


def test_operation_input_accepts_only_the_historically_validated_subset() -> None:
    """Operation snapshots do not impose the full live-state schema retroactively."""
    value = {
        'schema_family': 'pr_similarity_os.state',
        'schema_version': 2,
        'run_id': 'run',
        'registry_sha256': 'a' * 64,
        'revision': 1,
        'tasks': [{'id': 'task', 'attempt': {'id': 'attempt'}}],
    }
    assert OperationInputState.model_validate(value).to_wire() == value


@pytest.mark.parametrize(
    'field', ['env', 'retries', 'runtime', 'auth_mode', 'timeout_seconds', 'diff_excludes', 'identity_version']
)
@pytest.mark.parametrize('value', [None, False, 1.0, '', [], {}])
def test_identity_does_not_validate_unconsumed_harness_metadata(field: str, value: object) -> None:
    """Frozen-input proof must leave unrelated display settings for their own consumer."""
    document = {'native_config': {}, field: value}
    identity = ConfigIdentity.model_validate(document)
    assert json_bytes(identity.to_wire()) == json_bytes(document)


def test_start_ignores_completion_metadata_and_absent_reservation_fields() -> None:
    """A reservation comparison examines supplied fields and ignores finished_at."""
    document = {
        'schema_family': 'pr_similarity_os.start',
        'schema_version': 2,
        'run_id': 'run',
        'task_id': 'task',
        'basket_sha256': 'a' * 64,
        'config_identity_sha256': 'b' * 64,
        'attempt': {'finished_at': False},
    }
    assert Start.model_validate(document).to_wire() == document
