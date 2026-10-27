"""Declared inference facts require exact frozen and sealed native configuration evidence."""

from copy import deepcopy
from pathlib import Path

import pytest
from helpers import archive_fixture, documents
from python_fixtures import python_document

from viewer.config import Limits
from viewer.errors import ViewerError
from viewer.models import JsonValue, Report
from viewer.models.evidence import ArtifactReceipt, ConfigIdentity, parse_evidence
from viewer.presentation.inference import build_inference
from viewer.util import digest, json_bytes, parse_json, require_object
from viewer.validation.archive import validate_archive
from viewer.validation.contracts import validate_inference_evidence


def evidence() -> tuple[dict, dict, dict[str, dict], dict[str, bytes]]:
    """Create independently owned start/seal/config evidence for one selected attempt."""
    report, _, _ = documents()
    task = report['tasks'][0]
    task['directory'] = 'tasks/001-fixture-12345678'
    prefix = task['directory'] + '/attempts/001/'
    native = {
        'model': 'openai/selected',
        'provider': {'openai': {'models': {'selected': {'options': {'reasoningEffort': 'medium'}}}}},
    }
    identity = {'identity_version': 1, 'native_config': native}
    report['config']['identity_sha256'] = digest(json_bytes(identity))
    ref = 'agent/effective/' + digest(json_bytes(native)) + '.json'
    task['artifacts']['effective_config'] = {
        'status': 'saved',
        'path': ref,
        'message': None,
    }
    start = {
        'schema_family': 'pr_similarity_os.start',
        'schema_version': 2,
        'run_id': report['run']['id'],
        'task_id': task['id'],
        'attempt': task['final_attempt'],
        'basket_sha256': report['basket']['sha256'],
        'config_identity_sha256': report['config']['identity_sha256'],
    }
    receipt = {
        'path': ref,
        'size_bytes': len(json_bytes(native)),
        'sha256': digest(json_bytes(native)),
    }
    seal = {
        'agent_input': report['agent_input'],
        'schema_family': 'pr_similarity_os.seal',
        'schema_version': 2,
        'run_id': report['run']['id'],
        'basket_sha256': report['basket']['sha256'],
        'config_identity_sha256': report['config']['identity_sha256'],
        'task': task,
        'archive_complete': True,
        'python_runtime': report['agent']['python_runtime'],
        'artifact_receipts': [receipt, report['agent']['python_runtime']],
    }
    files = {
        '_meta/python.json': json_bytes(python_document()),
        ref: json_bytes(native),
        prefix + 'start.json': json_bytes(start),
        prefix + 'seal.json': json_bytes(seal),
    }

    def file_receipt(path: str) -> dict:
        """Compute the size and digest of a fixture evidence file."""
        return {
            'path': path,
            'size_bytes': len(files[path]),
            'sha256': digest(files[path]),
        }

    files['_meta/state.json'] = json_bytes(
        {
            'tasks': [
                {
                    'id': task['id'],
                    'history': [],
                    'attempt': task['final_attempt'],
                    'start': file_receipt(prefix + 'start.json'),
                    'seal': file_receipt(prefix + 'seal.json'),
                }
            ]
        }
    )
    inventory = {name: {'path': name, 'size_bytes': len(raw), 'sha256': digest(raw)} for name, raw in files.items()}
    report['inference'] = build_inference(
        identity,
        [
            {
                'attempt_id': task['final_attempt']['id'],
                'source_ref': ref,
                'config': native,
            }
        ],
    )
    return report, identity, inventory, files


def test_exact_native_facts_and_owned_seal_are_accepted() -> None:
    """A declared effective source is confirmed only through matching immutable receipts."""
    report, identity, inventory, files = evidence()
    validate_inference_evidence(
        parse_evidence(Report, report, 'invalid_inference'),
        parse_evidence(ConfigIdentity, identity, 'invalid_inference'),
        {path: ArtifactReceipt.model_validate(receipt) for path, receipt in inventory.items()},
        files.__getitem__,
    )


@pytest.mark.parametrize(
    'mutation',
    [
        'configured_model',
        'configured_unknown',
        'configured_reason',
        'configured_origin',
        'effective_value',
        'effective_origin',
        'duplicate',
        'start_owner',
        'seal_owner',
        'receipt_hash',
        'unsealed',
        'omit_selected',
        'start_bool_sequence',
        'seal_bool_sequence',
        'seal_malformed_artifacts',
    ],
)
def test_forged_configured_or_effective_claim_is_rejected(mutation: str) -> None:
    """Valid JSON and archive hashes cannot legitimize invented settings or foreign owners."""
    report, identity, inventory, files = evidence()
    configured = report['inference']['configured']['model']
    attempts = report['inference']['effective']['attempts']
    prefix = report['tasks'][0]['directory'] + '/attempts/001/'
    if mutation == 'configured_model':
        configured['value'] = 'openai/forged'
    if mutation == 'configured_unknown':
        configured.update(status='unknown', value=None, reason='hidden')
    if mutation == 'configured_reason':
        configured['reason'] = 'invented'
    if mutation == 'configured_origin':
        configured['source']['field'] = 'native_config.other'
    if mutation == 'effective_value':
        attempts[0]['effort']['value'] = 'high'
    if mutation == 'effective_origin':
        attempts[0]['source_ref'] = 'logs/effective_config.json'
    if mutation == 'duplicate':
        attempts.append(deepcopy(attempts[0]))
    if mutation == 'start_owner':
        start = require_object(parse_json(files[prefix + 'start.json']))
        start['run_id'] = 'foreign'
        files[prefix + 'start.json'] = json_bytes(start)
    if mutation == 'seal_owner':
        seal = require_object(parse_json(files[prefix + 'seal.json']))
        require_object(seal['task'])['id'] = 'foreign'
        files[prefix + 'seal.json'] = json_bytes(seal)
    if mutation == 'receipt_hash':
        seal = require_object(parse_json(files[prefix + 'seal.json']))
        receipts = seal['artifact_receipts']
        assert isinstance(receipts, list)
        require_object(receipts[0])['sha256'] = 'f' * 64
        files[prefix + 'seal.json'] = json_bytes(seal)
    if mutation == 'unsealed':
        del inventory[prefix + 'seal.json']
    if mutation == 'omit_selected':
        report['inference'] = build_inference(identity)
    if mutation == 'start_bool_sequence':
        start = require_object(parse_json(files[prefix + 'start.json']))
        require_object(start['attempt'])['sequence'] = True
        files[prefix + 'start.json'] = json_bytes(start)
    if mutation == 'seal_bool_sequence':
        seal = require_object(parse_json(files[prefix + 'seal.json']))
        require_object(require_object(seal['task'])['final_attempt'])['sequence'] = True
        files[prefix + 'seal.json'] = json_bytes(seal)
    if mutation == 'seal_malformed_artifacts':
        seal = require_object(parse_json(files[prefix + 'seal.json']))
        require_object(seal['task'])['artifacts'] = None
        files[prefix + 'seal.json'] = json_bytes(seal)
    for path in (prefix + 'start.json', prefix + 'seal.json'):
        if path in inventory:
            inventory[path] = {
                'path': path,
                'size_bytes': len(files[path]),
                'sha256': digest(files[path]),
            }
    with pytest.raises(ViewerError):
        validate_inference_evidence(
            parse_evidence(Report, report, 'invalid_inference'),
            parse_evidence(ConfigIdentity, identity, 'invalid_inference'),
            {path: ArtifactReceipt.model_validate(receipt) for path, receipt in inventory.items()},
            files.__getitem__,
        )


@pytest.mark.parametrize('same_config', [False, True])
def test_effective_mixed_keeps_both_owned_sealed_attempts(same_config: bool) -> None:
    """Historical and selected sealed settings remain distinct before consensus aggregation."""
    report, identity, inventory, files = evidence()
    task = report['tasks'][0]
    first_claim = report['inference']['effective']['attempts'][0]
    first_native = require_object(parse_json(files[first_claim['source_ref']]))
    task['final_attempt'] = {
        **task['final_attempt'],
        'sequence': 2,
        'id': '44444444-4444-4444-8444-444444444444',
    }
    native = deepcopy(first_native)
    if not same_config:
        provider = require_object(require_object(native['provider'])['openai'])
        selected_model = require_object(require_object(provider['models'])['selected'])
        require_object(selected_model['options'])['reasoningEffort'] = 'high'
    ref = 'agent/effective/' + digest(json_bytes(native)) + '.json'
    task['artifacts']['effective_config']['path'] = ref
    prefix = task['directory'] + '/attempts/002/'
    old_prefix = task['directory'] + '/attempts/001/'
    start = require_object(parse_json(files[old_prefix + 'start.json']))
    start['attempt'] = task['final_attempt']
    seal = require_object(parse_json(files[old_prefix + 'seal.json']))
    seal['task'] = task
    seal['artifact_receipts'] = [
        {
            'path': ref,
            'size_bytes': len(json_bytes(native)),
            'sha256': digest(json_bytes(native)),
        },
        report['agent']['python_runtime'],
    ]
    for path, value in [
        (ref, native),
        (prefix + 'start.json', start),
        (prefix + 'seal.json', seal),
    ]:
        raw = json_bytes(value)
        files[path] = raw
        inventory[path] = {'path': path, 'size_bytes': len(raw), 'sha256': digest(raw)}
    state = require_object(parse_json(files['_meta/state.json']))
    tasks = state['tasks']
    assert isinstance(tasks, list)
    selected_task = require_object(tasks[0])
    old = deepcopy(selected_task)
    selected_task.update(
        attempt=task['final_attempt'],
        history=[old],
        start=inventory[prefix + 'start.json'],
        seal=inventory[prefix + 'seal.json'],
    )
    files['_meta/state.json'] = json_bytes(state)
    inventory['_meta/state.json'] = {
        'path': '_meta/state.json',
        'size_bytes': len(files['_meta/state.json']),
        'sha256': digest(files['_meta/state.json']),
    }
    report['inference'] = build_inference(
        identity,
        [
            {
                'attempt_id': first_claim['attempt_id'],
                'source_ref': first_claim['source_ref'],
                'config': first_native,
            },
            {
                'attempt_id': task['final_attempt']['id'],
                'source_ref': ref,
                'config': native,
            },
        ],
    )
    validate_inference_evidence(
        parse_evidence(Report, report, 'invalid_inference'),
        parse_evidence(ConfigIdentity, identity, 'invalid_inference'),
        {path: ArtifactReceipt.model_validate(receipt) for path, receipt in inventory.items()},
        files.__getitem__,
    )
    assert report['inference']['effective']['effort']['status'] == ('known' if same_config else 'mixed')
    assert report['inference']['discrepancy'] is (not same_config)


def test_recomputed_archive_hash_does_not_legitimize_forged_configured_effort(
    tmp_path: Path,
) -> None:
    """The real archive ingress rejects fabricated facts even when all byte hashes match."""

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Inject a false inference field while the fixture rebuilds honest inventory hashes."""
        report['inference']['configured']['effort'].update(status='known', value='medium', reason=None)

    path = tmp_path / 'forged.tar.gz'
    archive_fixture(path, change=change)
    with pytest.raises(ViewerError) as error:
        validate_archive(path, tmp_path / 'data', Limits())
    assert error.value.code == 'invalid_inference'
    assert not (tmp_path / 'data').exists()


def inference_cases() -> list[JsonValue]:
    """Read the shared raw fixtures without asserting unchecked mapping types."""
    cases = require_object(parse_json((Path(__file__).parent / 'fixtures/inference-contract.json').read_bytes()))[
        'cases'
    ]
    assert isinstance(cases, list)
    return cases


@pytest.mark.parametrize(
    'case',
    inference_cases(),
    ids=lambda case: case['name'],
)
def test_portable_native_inference_cases(case: dict) -> None:
    """Shared portable cases pin the supported subset without optional producer imports."""
    value = build_inference(case['identity'], case['effective_configs'])
    expected = case['expected']
    configured = value['configured']
    assert configured['model']['status'] == expected['configured_status']
    assert configured['model']['reason'] == expected['configured_reason']
    assert configured['model']['value'] == expected['model']
    assert configured['effort']['value'] == expected['effort']
    assert value['effective']['effort']['status'] == expected['effective_effort_status']
    assert value['discrepancy'] is expected['discrepancy']
    if 'effort_status' in expected:
        assert configured['effort']['status'] == expected['effort_status']
    if 'effort_reason' in expected:
        assert configured['effort']['reason'] == expected['effort_reason']
    if 'effort_field' in expected:
        assert configured['effort']['source']['field'] == expected['effort_field']
