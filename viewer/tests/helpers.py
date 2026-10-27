"""Frozen-schema tar fixture builder without producer imports."""

import copy
import io
import json
import tarfile
import uuid
from collections.abc import Callable
from pathlib import Path

from python_fixtures import python_attempt, python_document, python_reference
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viewer.models import Run, State
from viewer.models.evidence import ArtifactReceipt
from viewer.presentation.cost_summary import build_cost_summary
from viewer.service import ViewerService
from viewer.util import digest, json_bytes, parse_json, require_object
from viewer.validation.contracts import validate_report
from viewer.validation.operations import OperationStore
from viewer.validation.triplet_paths import triplet_path

FIXTURES = Path(__file__).parent / 'fixtures'
FileCallback = Callable[[dict, dict, dict[str, bytes]], None]
PrepareCallback = Callable[[dict, dict], None]


def costs() -> dict:
    """Describe synthetic fixture billing truthfully: no native ledger or pricing operation exists."""
    coverage = {
        'total_attempts': 1,
        'complete_attempts': 0,
        'known_attempts': 0,
        'known_requests': 0,
        'missing_tasks': 0,
        'total_requests': None,
    }
    return {
        'final_attempts': {
            'native_reported': {
                'status': 'unavailable',
                'currency': 'USD',
                'amount': None,
                'known_subtotal': '0',
                'coverage': coverage.copy(),
            },
            'api_equivalent': {
                'status': 'not_requested',
                'currency': 'USD',
                'amount': None,
                'known_subtotal': '0',
                'coverage': coverage.copy(),
            },
        },
        'all_attempts': {
            'native_reported': {
                'status': 'unavailable',
                'currency': 'USD',
                'amount': None,
                'known_subtotal': '0',
                'coverage': coverage.copy(),
            },
            'api_equivalent': {
                'status': 'not_requested',
                'currency': 'USD',
                'amount': None,
                'known_subtotal': '0',
                'coverage': coverage.copy(),
            },
        },
    }


def inference(model: str | None = 'openai/selected', effort: str | None = 'medium') -> dict:
    """Create independent consumer facts without importing the producer."""

    def fact(value: str | None, field: str) -> dict:
        """Build one source-bound configured value."""
        unknown_reason = 'effort_not_configured' if model and field != 'model' else 'model_not_configured'
        return {
            'status': 'known' if value is not None else 'unknown',
            'value': value,
            'reason': None if value is not None else unknown_reason,
            'source': {
                'ref': 'config_identity.json',
                'field': 'native_config.' + field,
            },
        }

    configured = {
        'provider': fact(model.split('/')[0] if model else None, 'model'),
        'model': fact(model, 'model'),
        'effort': fact(
            effort,
            (('provider.' + model.replace('/', '.models.', 1) + '.options.reasoningEffort') if model else 'model'),
        ),
    }
    effective = {
        key: {
            'status': 'unknown',
            'value': None,
            'reason': 'effective_evidence_unavailable',
            'source': None,
        }
        for key in ('provider', 'model', 'effort')
    }
    return {
        'schema_version': 1,
        'configured': configured,
        'effective': {**effective, 'attempts': []},
        'discrepancy': False,
    }


def documents() -> tuple[dict, dict, bytes]:
    """Build synthetic consumer documents with one complete source-only task."""
    report = json.loads((FIXTURES / 'completed.json').read_bytes())
    basket = json.loads((FIXTURES / 'basket.json').read_bytes())
    report.setdefault('inference', inference(None, None))
    report['agent']['python_runtime'] = python_reference()
    report['agent_input'] = {
        'config': {
            'path': 'agent/opencode.json',
            'size_bytes': 2,
            'sha256': digest(b'{}'),
        },
        'resources': [],
    }
    report['costs'] = costs()
    report['state'] = {'path': '_meta/state.json', 'sha256': '0' * 64}
    row, source = report['tasks'][0], basket['tasks'][0]
    for key in ('repository', 'number', 'url', 'title', 'kind'):
        row[key] = source['change_request'][key]
    for key in ('workspace', 'agent_task', 'source', 'diff_policy'):
        row[key] = copy.deepcopy(source[key])
    row['ordinal'] = 1
    row['directory'] = f"tasks/001-fixture-{digest(row['id'].encode())[:8]}"
    prefix = row['directory'] + f"/attempts/{row['final_attempt']['sequence']:03d}/"
    row['progress'] = {'phase': 'completed', 'updated_at': report['run']['updated_at']}
    row['locations'] = {
        'agent_log': prefix + 'agent.log',
        'agent_events': prefix + 'events.jsonl',
        'runtime_log': prefix + 'runtime.log',
        'setup_log': prefix + 'setup.log',
        'outcome': prefix + 'outcome.json',
        'triplets_manifest': prefix + 'triplets/index.json',
    }
    row['scoring']['files'][0]['path'] = 'new/empty.py'
    row['scoring']['files'][0]['human_loc'] = 0
    row['scoring']['counts']['human_loc'] = 0
    basket_bytes = json_bytes(basket)
    report['basket']['sha256'] = digest(basket_bytes)
    return report, basket, basket_bytes


def _normalize_attempts(report: dict) -> None:
    """Assign registry ordinals and adjacent variant paths before freezing synthetic facts."""
    seen_attempts = set()
    for ordinal, task in enumerate(report['tasks'], 1):
        task['ordinal'] = ordinal
        task['directory'] = f'tasks/{ordinal:03d}-fixture-{digest(task['id'].encode())[:8]}'
        attempt = task['final_attempt']
        if attempt is None:
            continue
        if attempt['id'] in seen_attempts:
            attempt['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, task['id'] + ':' + str(attempt['sequence'])))
        seen_attempts.add(attempt['id'])
        prefix = f'{task['directory']}/attempts/{attempt['sequence']:03d}/'
        old = task['artifacts']['triplets_manifest']
        old_prefix = old.removesuffix('triplets/index.json') if old else None
        for field, filename in (
            ('outcome', 'outcome.json'),
            ('triplets_manifest', 'triplets/index.json'),
            ('setup_log', 'setup.log'),
            ('agent_events', 'events.jsonl'),
            ('agent_log', 'agent.log'),
        ):
            if task['artifacts'][field] is not None:
                task['artifacts'][field] = prefix + filename
        if task['changes']['diff'] is not None:
            task['changes']['diff'] = prefix + 'agent.diff'
        for number, row in enumerate(task['scoring']['files'], 1):
            for role in ('base', 'human', 'ai'):
                row[role] = (
                    prefix + 'triplets/' + triplet_path(number, row['path'], role) if row['exists'][role] else None
                )
        for field in ('session_db', 'usage_ledger'):
            receipt = task['telemetry'][field]
            if receipt is not None and old_prefix is not None:
                receipt['path'] = receipt['path'].replace(old_prefix, prefix)


def _triplet_document(report: dict, task: dict, files: dict[str, bytes]) -> dict:
    """Bind each existing role to its exact synthetic bytes and original source path."""
    entries = []
    for number, row in enumerate(task['scoring']['files'], 1):
        entries.append(
            {
                'path': row['path'],
                'number': number,
                'exists': row['exists'],
                'modes': {role: '100644' if row['exists'][role] else None for role in ('base', 'human', 'ai')},
                'files': {
                    role: (
                        {
                            'path': triplet_path(number, row['path'], role),
                            'size_bytes': len(files[row[role]]),
                            'sha256': digest(files[row[role]]),
                        }
                        if row['exists'][role]
                        else None
                    )
                    for role in ('base', 'human', 'ai')
                },
            }
        )
    return {
        'version': 1,
        'run_id': report['run']['id'],
        'task_id': task['id'],
        'attempt_id': task['final_attempt']['id'],
        'source': {'before': task['before'], 'after': task['after']},
        'entries': entries,
    }


def _artifact_files(report: dict, basket_bytes: bytes) -> dict[str, bytes]:
    """Create explicit synthetic role, log and telemetry files without producer imports."""
    files = {
        'basket.json': basket_bytes,
        'config_identity.json': b'{}',
        'runtime.json': b'{}',
        '_meta/runtime.json': json_bytes(
            {
                'actual_version': report['agent']['resolved_runtime']['version'],
                **{
                    key: report['agent']['resolved_runtime'][key]
                    for key in (
                        'asset_name',
                        'platform',
                        'archive_sha256',
                        'binary_sha256',
                    )
                },
            }
        ),
        '_meta/sandbox.json': b'{}',
        '_meta/python.json': json_bytes(python_document()),
    }
    report['config']['identity_sha256'] = digest(json_bytes({}))
    for task in report['tasks']:
        for row in task['scoring']['files']:
            for role in ('base', 'human', 'ai'):
                if row[role] is not None:
                    files[row[role]] = b'' if role == 'base' else b'print("hello")\n'
        for value in task['artifacts'].values():
            if isinstance(value, str):
                files[value] = b'{}'
        if task['changes']['diff'] is not None:
            files[task['changes']['diff']] = b'agent diff\n'
        for key in ('session_db', 'usage_ledger'):
            receipt = task['telemetry'][key]
            if receipt is not None:
                files[receipt['path']] = b'fixture'
                receipt['sha256'] = digest(b'fixture')
        files[task['artifacts']['triplets_manifest']] = json_bytes(_triplet_document(report, task, files))
    return files


def _registry(report: dict, basket: dict) -> dict:
    """Freeze basket ownership and local labels from configured synthetic inputs."""
    return {
        'schema_family': 'pr_similarity_os.run',
        'schema_version': 2,
        'id': report['run']['id'],
        'agent_input': copy.deepcopy(report['agent_input']),
        'baseline': {
            key: copy.deepcopy(value)
            for key, value in report.items()
            if key not in ('tasks', 'summary', 'state', 'costs', 'agent_input')
        },
        'tasks': [
            {
                'id': task['id'],
                'ordinal': task['ordinal'],
                'key': task['directory'].removeprefix('tasks/'),
                'path': task['directory'],
                'basket_row': copy.deepcopy(source),
            }
            for task, source in zip(report['tasks'], basket['tasks'], strict=True)
        ],
    }


def _reference(path: str, files: dict[str, bytes]) -> dict:
    """Return the exact closed receipt for one explicitly named fixture artifact."""
    return {'path': path, 'size_bytes': len(files[path]), 'sha256': digest(files[path])}


def _native_facts(report: dict, task: dict, files: dict[str, bytes]) -> dict:
    """Freeze native and host-score documents in dependency order for one attempt."""
    attempt = task['final_attempt']
    prefix = f'{task['directory']}/attempts/{attempt['sequence']:03d}/'
    owner = {
        'schema_version': 2,
        'run_id': report['run']['id'],
        'basket_sha256': report['basket']['sha256'],
        'config_identity_sha256': report['config']['identity_sha256'],
    }
    start = {
        **owner,
        'schema_family': 'pr_similarity_os.start',
        'task_id': task['id'],
        'attempt': attempt,
    }
    files[prefix + 'start.json'] = json_bytes(start)
    files[prefix + 'python.json'] = json_bytes(python_attempt(report['agent']['python_runtime']))
    python_receipts = [
        report['agent']['python_runtime'],
        _reference(prefix + 'python.json', files),
    ]
    seal = {
        **owner,
        'agent_input': copy.deepcopy(report['agent_input']),
        'schema_family': 'pr_similarity_os.seal',
        'task': copy.deepcopy(task),
        'metric': copy.deepcopy(report['metric']),
        'python_runtime': report['agent']['python_runtime'],
        'artifact_receipts': python_receipts,
    }
    files[prefix + 'seal.json'] = json_bytes(seal)
    seal_ref = _reference(prefix + 'seal.json', files)
    scoring = {
        'schema_family': 'pr_similarity_os.scoring',
        'schema_version': 2,
        'run_id': report['run']['id'],
        'task_id': task['id'],
        'attempt_id': attempt['id'],
        'seal': seal_ref,
        'scoring': copy.deepcopy(task['scoring']),
        'metric': copy.deepcopy(report['metric']),
        'diagnostics': [],
        'error': None,
    }
    files[prefix + 'scoring.json'] = json_bytes(scoring)
    scoring_ref = _reference(prefix + 'scoring.json', files)
    outcome = {
        **owner,
        'agent_input': copy.deepcopy(report['agent_input']),
        'schema_family': 'pr_similarity_os.outcome',
        'task': copy.deepcopy(task),
        'seal': seal_ref,
        'metric': copy.deepcopy(report['metric']),
        'scoring_result': scoring_ref,
        'scoring_error': None,
        'python_runtime': report['agent']['python_runtime'],
        'artifact_receipts': [scoring_ref, *python_receipts],
    }
    files[prefix + 'outcome.json'] = json_bytes(outcome)
    task['artifacts']['outcome'] = prefix + 'outcome.json'
    return _selection(report, task, prefix, seal_ref, files)


def _selection(report: dict, task: dict, prefix: str, seal_ref: dict, files: dict[str, bytes]) -> dict:
    """Select explicit native receipts while retaining host-operation fields as absent."""
    attempt = task['final_attempt']
    return {
        'id': task['id'],
        'ordinal': task['ordinal'],
        'processing_status': task['processing_status'],
        'attempt': copy.deepcopy(attempt),
        'next_sequence': attempt['sequence'] + 1,
        'retry_index': attempt['retry_index'],
        'retry_epoch': 0,
        'start': _reference(prefix + 'start.json', files),
        'seal': seal_ref,
        'outcome': _reference(prefix + 'outcome.json', files),
        'scoring': None,
        'pricing': None,
        'progress': copy.deepcopy(task['progress']),
        'history': [],
    }


def _freeze_state(report: dict, basket: dict, files: dict[str, bytes]) -> None:
    """Bind configured inputs, immutable native facts and the accepted projection together."""
    for task in report['tasks']:
        task['progress'] = {
            'phase': task['processing_status'].replace('_', ' '),
            'updated_at': report['run']['updated_at'],
        }
    identity = require_object(parse_json(files['config_identity.json']))
    identity.setdefault('native_config', {})
    identity.setdefault('resources', [])
    identity.setdefault('setup', None)
    identity.setdefault('instructions', {'protocol': 'benchmark_task_v1', 'sha256': digest(b'')})
    files['config_identity.json'] = json_bytes(identity)
    report['config']['identity_sha256'] = digest(files['config_identity.json'])
    files.setdefault('agent/opencode.json', json_bytes(identity['native_config']))
    files['agent/task_instructions.txt'] = b''
    report['agent_input'] = {
        'config': _reference('agent/opencode.json', files),
        'resources': [
            _reference(path, files)
            for path in sorted(files)
            if path.startswith('agent/resources/') or path in {'agent/task_instructions.txt', 'agent/setup.sh'}
        ],
    }
    files['run.json'] = json_bytes(_registry(report, basket))
    selections = [_native_facts(report, task, files) for task in report['tasks']]
    state = {
        'schema_family': 'pr_similarity_os.state',
        'schema_version': 2,
        'run_id': report['run']['id'],
        'registry_sha256': digest(files['run.json']),
        'revision': report['run']['revision'],
        'updated_at': report['run']['updated_at'],
        'run': {key: report['run'][key] for key in ('status', 'finished_at', 'error', 'current_invocation_id')},
        'resolved_runtime': copy.deepcopy(report['agent']['resolved_runtime']),
        'python_runtime': copy.deepcopy(report['agent']['python_runtime']),
        'metric': copy.deepcopy(report['metric']),
        'selected_operations': {'rescore': None, 'reprice': None},
        'pending_publish': None,
        'last_publish': None,
        'tasks': selections,
    }
    files['_meta/state.json'] = json_bytes(state)
    report['state'] = {
        'path': '_meta/state.json',
        'sha256': digest(files['_meta/state.json']),
    }
    registry = Run.model_validate(parse_json(files['run.json']))
    inventory = {name: ArtifactReceipt.model_validate(_reference(name, files)) for name in files}
    adapter = OperationStore(
        registry,
        State.model_validate(state),
        inventory,
        files.__getitem__,
        validate_report,
    )
    report['costs'] = build_cost_summary(adapter).to_wire()


def _manifest(report: dict, files: dict[str, bytes]) -> dict:
    """Inventory exact synthetic archive bytes after any intentional corruption callback."""
    return {
        'schema_family': 'pr_similarity_os.archive',
        'schema_version': 2,
        'run_id': report['run']['id'],
        'created_at': report['run']['created_at'],
        'report_revision': report['run']['revision'],
        'report_sha256': digest(files['report.json']),
        'complete': True,
        'missing_artifacts': [],
        'files': [_reference(name, files) for name in sorted(files)],
    }


def _write_archive(
    path: Path,
    files: dict[str, bytes],
    extra: tuple[tarfile.TarInfo, io.BytesIO] | None,
) -> None:
    """Write real tar members, including explicitly requested malicious test members."""
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra:
            archive.addfile(*extra)


def archive_fixture(
    path: Path,
    *,
    change: FileCallback | None = None,
    extra: tuple[tarfile.TarInfo, io.BytesIO] | None = None,
    run_id: str | None = None,
    prepare: PrepareCallback | None = None,
    configure: FileCallback | None = None,
) -> tuple[dict, dict]:
    """Freeze a synthetic archive; configure valid inputs before facts and corrupt with change after.

    Args:
        path: Destination archive file.
        change: Optional corruption or post-freeze file-edit callback; never reseals facts.
        extra: Optional extra tar member for unsafe-ingress cases.
        run_id: Explicit fixture identity for overwrite tests.
        prepare: Optional logical document changes before artifact paths are materialized.
        configure: Optional valid identity, billing or score inputs before immutable facts are frozen.

    Returns:
        Final report and inventory manifest for the real archive bytes.

    """
    report, basket, basket_bytes = documents()
    if run_id:
        report['run']['id'] = run_id
    if prepare:
        prepare(report, basket)
        basket_bytes = json_bytes(basket)
        report['basket']['sha256'] = digest(basket_bytes)
    _normalize_attempts(report)
    files = _artifact_files(report, basket_bytes)
    if configure:
        configure(report, basket, files)
    _freeze_state(report, basket, files)
    if change:
        change(report, basket, files)
    files['report.json'] = json_bytes(report)
    manifest = _manifest(report, files)
    files['manifest.json'] = json_bytes(manifest)
    _write_archive(path, files, extra)
    return report, manifest


def service_for(client: TestClient) -> ViewerService:
    """Return the concrete service wired into a running Starlette test client."""
    app = client.app
    assert isinstance(app, Starlette)
    service = app.state.service
    assert isinstance(service, ViewerService)
    return service
