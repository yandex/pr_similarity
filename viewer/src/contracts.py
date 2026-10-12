"""Package-local consumer schemas and arithmetic/reference invariants."""

import math
from viewer.agent_input import validate_agent_input
import re
from collections.abc import Callable
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from viewer.cost_summary import CostEvidenceError, build_cost_summary
from viewer.errors import ViewerError
from viewer.inference import build_inference, public_inference
from viewer.operations import OperationStore
from viewer.triplet_paths import triplet_path
from viewer.util import digest, json_bytes, parse_json, safe_path


def require(condition: object, code: str = 'invalid_contract') -> None:
    """Reject inconsistent public artifacts with a safe stable error code."""
    if not condition:
        raise ViewerError(code, 'Uploaded documents or artifacts are inconsistent.')


@lru_cache(maxsize=6)
def validator(name: str) -> Draft202012Validator:
    """Load the package-owned schema for an explicit consumer contract."""
    schema = parse_json(files('viewer').joinpath('schemas', name + '-v1.json').read_bytes())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_document(name: str, value: object) -> None:
    """Reject unsupported schema shapes without exposing submitted field values."""
    try:
        validator(name).validate(value)
    except (ValidationError, RecursionError):
        raise ViewerError('invalid_' + name, 'Unsupported or invalid ' + name + ' contract.') from None


def validate_report(report: dict) -> None:
    """Validate the direct schema-1 report, public paths and aggregate arithmetic."""
    validate_document('report', report)
    tasks = report['tasks']
    require(report['run']['status'] not in ('running', 'stopping'))
    require(len(tasks) == report['basket']['total_tasks'] == report['summary']['total_tasks'])
    require(len({t['id'] for t in tasks}) == len(tasks))
    counts = {'total_tasks': len(tasks)}
    for state, name in [
        ('completed', 'completed_tasks'),
        ('pending', 'pending_tasks'),
        ('running', 'active_tasks'),
        ('retry_pending', 'retry_pending_tasks'),
        ('interrupted', 'interrupted_tasks'),
    ]:
        counts[name] = sum(t['processing_status'] == state for t in tasks)
    counts['infrastructure_error_tasks'] = sum(t['infrastructure_error'] is not None for t in tasks)
    for state in ('timeout', 'error', 'cancelled'):
        counts[state + '_tasks'] = sum(t['execution']['status'] == state for t in tasks)
    for state in ('nonempty', 'empty', 'unknown'):
        counts[state + '_diff_tasks'] = sum(t['changes']['status'] == state for t in tasks)
    counts['scored_tasks'] = sum(t['scoring']['status'] == 'available' for t in tasks)
    require(counts == report['summary'])
    for task in tasks:
        _validate_scores(task)


def _validate_scores(task: dict) -> None:
    """Check role references and recompute reported score counts and aggregates."""
    scoring = task['scoring']
    if scoring['status'] != 'available':
        return
    rows = scoring['files']
    if rows:
        prefix = _triplet_prefix(task)
        for row in rows:
            original = _source_path(row['path'])
            ordinal = None
            for role in ('base', 'human', 'ai'):
                reference = row[role]
                if not row['exists'][role]:
                    require(reference is None, 'invalid_public_artifact')
                    continue
                require(isinstance(reference, str) and reference.startswith(prefix), 'invalid_public_artifact')
                assert isinstance(reference, str)
                relative = reference[len(prefix) :]
                number = relative.split('/', 1)[0]
                require(re.fullmatch(r'[0-9]{3,}', number) is not None, 'invalid_public_artifact')
                try:
                    expected = prefix + triplet_path(int(number), original, role)
                except ValueError as error:
                    raise ViewerError('invalid_public_artifact', 'Invalid triplet variant reference.') from error
                require(reference == expected and (ordinal is None or ordinal == number), 'invalid_public_artifact')
                ordinal = number
    paths = [safe_path(f['path']) for f in rows]
    require(paths == sorted(set(paths)))
    totals = dict.fromkeys(
        [
            'human_files',
            'agent_files',
            'matched_files',
            'missed_files',
            'extra_files',
            'human_loc',
            'agent_loc',
            'loc_weight',
        ],
        0,
    )
    for row in rows:
        h, a = row['changed_human'], row['changed_agent']
        require(h or a)
        require(row['loc_weight'] == (max(1, row['human_loc']) if h else 0))
        for k, v in [
            ('human_files', h),
            ('agent_files', a),
            ('matched_files', h and a),
            ('missed_files', h and not a),
            ('extra_files', a and not h),
        ]:
            totals[k] += int(v)
        for k in ('human_loc', 'agent_loc', 'loc_weight'):
            totals[k] += row[k]
    require(totals == scoring['counts'])
    mean = sum(f['mlp'] for f in rows) / len(rows) if rows else 1
    weighted = sum(f['mlp'] * f['loc_weight'] for f in rows) / totals['loc_weight'] if totals['loc_weight'] else mean
    require(
        math.isclose(mean, scoring['Mf_mlp'], abs_tol=1e-12)
        and math.isclose(weighted, scoring['LCf_mlp'], abs_tol=1e-12)
    )


def validate_identity(
    report: dict,
    basket: dict,
    basket_bytes: bytes,
    manifest: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> None:
    """Bind untrusted archive documents to frozen input, native evidence and exact role receipts."""
    validate_report(report)
    validate_document('basket', basket)
    validate_document('archive', manifest)
    require(manifest['complete'] and not any(x['required'] for x in manifest['missing_artifacts']))
    require(manifest['run_id'] == report['run']['id'] and manifest['report_revision'] == report['run']['revision'])
    require(manifest['report_sha256'] == inventory['report.json']['sha256'])
    require(report['basket']['sha256'] == digest(basket_bytes))
    require(basket['meta']['stage'] == 'export' and basket['meta']['status'] == 'completed')
    require(basket['meta']['partial'] == report['basket']['partial'])
    require(len(basket['meta']['omitted_failed_ids']) == report['basket']['omitted_failed_count'])
    require([t['id'] for t in basket['tasks']] == [t['id'] for t in report['tasks']])
    for name in (
        'run.json',
        '_meta/state.json',
        '_meta/runtime.json',
        '_meta/sandbox.json',
        'basket.json',
        'config_identity.json',
    ):
        _reference(name, inventory)
    _validate_registry_state(report, basket, inventory, read_file)
    identity = parse_json(read_file('config_identity.json'))
    require(isinstance(identity, dict), 'invalid_inference')
    require(digest(json_bytes(identity)) == report['config']['identity_sha256'])
    validate_agent_input(report['agent_input'], identity, inventory, read_file)
    validate_inference_evidence(report, identity, inventory, read_file)
    _validate_runtime_receipt(report, inventory, read_file)
    _reference(report['config']['identity_path'], inventory)
    for task, source in zip(report['tasks'], basket['tasks']):
        _validate_archive_task(task, source, report, inventory, read_file)
    for key in ('last_rescore', 'last_reprice'):
        entry = report[key]
        if entry:
            for k in ('receipt_path', 'backup_path'):
                if k in entry:
                    _reference(entry[k], inventory)


def _validate_runtime_receipt(report: dict, inventory: dict[str, dict], read_file: Callable[[str], bytes]) -> None:
    """Bind the projected pinned runtime to its explicit host provisioning receipt."""
    runtime = report['agent']['resolved_runtime']
    require(isinstance(runtime, dict) and runtime.get('receipt') == '_meta/runtime.json', 'invalid_state')
    assert isinstance(runtime, dict)
    receipt = _document('_meta/runtime.json', inventory, read_file)
    for field in ('asset_name', 'platform', 'archive_sha256', 'binary_sha256'):
        require(runtime[field] == receipt[field], 'invalid_state')
    require(runtime['version'] == receipt['actual_version'], 'invalid_state')
    require(report['agent']['runtime'] == 'opencode:v' + runtime['version'], 'invalid_state')


def _receipt(value: object, inventory: dict[str, dict], path: str) -> None:
    """Require one exact immutable receipt instead of selecting a newest directory."""
    require(
        isinstance(value, dict) and set(value) == {'path', 'size_bytes', 'sha256'},
        'invalid_state',
    )
    assert isinstance(value, dict)
    require(type(value['size_bytes']) is int, 'invalid_state')
    require(value['path'] == path and inventory.get(path) == value, 'invalid_state')


def _document(path: str, inventory: dict[str, dict], read_file: Callable[[str], bytes]) -> dict:
    """Read a JSON fact only after the tar inventory has bound its exact bytes."""
    _reference(path, inventory)
    content = read_file(path)
    require(
        len(content) == inventory[path]['size_bytes'] and digest(content) == inventory[path]['sha256'],
        'invalid_state',
    )
    value = parse_json(content)
    require(isinstance(value, dict), 'invalid_state')
    assert isinstance(value, dict)
    return value


def _validate_registry_state(
    report: dict,
    basket: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> None:
    """Bind report rows to the direct v1 registry/state selection, including history."""
    registry = _document('run.json', inventory, read_file)
    state = _document('_meta/state.json', inventory, read_file)
    validate_document('run', registry)
    validate_document('state', state)
    require(
        registry.get('schema_family') == 'pr_similarity_os.run'
        and registry.get('schema_version') == 1
        and registry.get('id') == report['run']['id']
        and isinstance(registry.get('baseline'), dict)
        and isinstance(registry.get('tasks'), list),
        'invalid_state',
    )
    require(
        state.get('schema_family') == 'pr_similarity_os.state'
        and state.get('schema_version') == 1
        and state.get('run_id') == registry['id']
        and state.get('registry_sha256') == inventory['run.json']['sha256']
        and state.get('revision') == report['run']['revision']
        and isinstance(state.get('tasks'), list),
        'invalid_state',
    )
    require(
        report.get('state')
        == {
            'path': '_meta/state.json',
            'sha256': inventory['_meta/state.json']['sha256'],
        },
        'invalid_state',
    )
    require(registry['agent_input'] == report['agent_input'], 'invalid_agent_input')
    baseline = registry['baseline']
    require(
        baseline.get('basket', {}).get('sha256') == report['basket']['sha256']
        and baseline.get('basket', {}).get('path') == 'basket.json'
        and baseline.get('config', {}).get('identity_sha256') == report['config']['identity_sha256']
        and baseline.get('config', {}).get('identity_path') == 'config_identity.json',
        'invalid_state',
    )
    entries = registry['tasks']
    require(
        [entry.get('id') for entry in entries] == [task['id'] for task in basket['tasks']],
        'invalid_state',
    )
    require(
        [entry.get('id') for entry in entries] == [task['id'] for task in report['tasks']],
        'invalid_state',
    )
    require(
        [entry.get('id') for entry in state['tasks']] == [entry.get('id') for entry in entries],
        'invalid_state',
    )
    _validate_projection_header(report, registry, state)
    store = OperationStore(registry, state, inventory, read_file, validate_report)
    require(isinstance(state.get('selected_operations'), dict), 'invalid_state')
    for kind, field in (('rescore', 'last_rescore'), ('reprice', 'last_reprice')):
        selection = state['selected_operations'].get(kind)
        if selection is None:
            require(report[field] is None, 'invalid_state')
            if kind == 'rescore':
                require(state['metric'] == registry['baseline']['metric'], 'invalid_state')
        else:
            result = store.operation(selection, kind)
            if kind == 'rescore':
                require(result['metric'] == state['metric'], 'invalid_state')
            receipt = store.read_artifact(selection['receipt'])
            metadata = {
                'at': receipt['finished_at'],
                'invocation_id': receipt['invocation_id'],
                'previous_report_sha256': receipt['input_report']['sha256'],
                'backup_path': receipt['input_report']['path'],
                'receipt_path': selection['receipt']['path'],
            }
            if kind == 'reprice':
                metadata['calculation_id'] = selection['id']
            require(report[field] == metadata, 'invalid_state')
    for registration, selected, task in zip(entries, state['tasks'], report['tasks'], strict=True):
        _validate_registered_task(registration, selected, task, basket, store)
    try:
        costs = build_cost_summary(store)
    except CostEvidenceError as error:
        raise ViewerError(error.code, error.message) from error
    require(report['costs'] == costs, 'invalid_costs')
    for field, closed in (('pending_publish', False), ('last_publish', True)):
        selected = state.get(field)
        if selected is not None:
            _validate_publication_selection(selected, closed, report, inventory, read_file)


def _validate_projection_header(report: dict, registry: dict, state: dict) -> None:
    """Keep public control fields and immutable display metadata tied to their one source."""
    baseline = registry['baseline']
    for field in ('id', 'created_at', 'tags'):
        require(report['run'][field] == baseline['run'][field], 'invalid_state')
    for field in ('status', 'finished_at', 'error', 'current_invocation_id'):
        require(report['run'][field] == state['run'][field], 'invalid_state')
    for field in ('name', 'runtime', 'auth_mode'):
        require(report['agent'][field] == baseline['agent'][field], 'invalid_state')
    require(report['run']['updated_at'] == state['updated_at'], 'invalid_state')
    require(report['agent']['resolved_runtime'] == state['resolved_runtime'], 'invalid_state')
    require(report['metric'] == state['metric'], 'invalid_state')
    require(report['basket'] == baseline['basket'] and report['config'] == baseline['config'], 'invalid_state')


def _validate_registered_task(
    registration: dict, selected: dict, task: dict, basket: dict, store: OperationStore
) -> None:
    """Validate native history and derive the public row from its selected immutable facts."""
    require(
        registration.get('ordinal') == task.get('ordinal') == selected.get('ordinal')
        and registration.get('path') == task.get('directory')
        and registration.get('basket_row') == basket['tasks'][task['ordinal'] - 1],
        'invalid_state',
    )
    require(task['processing_status'] == selected['processing_status'], 'invalid_state')
    require(task['progress'] == selected['progress'], 'invalid_state')
    owners = [*selected.get('history', []), selected] if selected.get('attempt') is not None else []
    sequences = []
    for owner in owners:
        native, initial = _validate_registered_attempt(registration, owner, task, store)
        sequences.append(owner['attempt']['sequence'])
        if owner is selected:
            _validate_selected_row(task, selected, native, initial, store)
    require(
        sequences == list(range(1, len(sequences) + 1)) and selected.get('next_sequence') == len(sequences) + 1,
        'invalid_state',
    )


def _validate_registered_attempt(
    registration: dict, owner: dict, task: dict, store: OperationStore
) -> tuple[dict, dict | None]:
    """Require a closed native seal and either its outcome or a selected available host score."""
    attempt = owner.get('attempt')
    require(
        isinstance(attempt, dict) and type(attempt.get('sequence')) is int and attempt['sequence'] >= 1, 'invalid_state'
    )
    assert isinstance(attempt, dict)
    prefix = registration['path'] + f"/attempts/{attempt['sequence']:03d}/"
    for name in ('start', 'seal'):
        _receipt(owner.get(name), store.inventory, prefix + name + '.json')
        fact = store.document(prefix + name + '.json')
        require(
            fact.get('schema_family') == 'pr_similarity_os.' + name
            and type(fact.get('schema_version')) is int
            and fact['schema_version'] == 1
            and fact.get('run_id') == store.registry['id']
            and fact.get('basket_sha256') == store.registry['baseline']['basket']['sha256']
            and fact.get('config_identity_sha256') == store.registry['baseline']['config']['identity_sha256'],
            'invalid_state',
        )
    start, seal = store.document(prefix + 'start.json'), store.document(prefix + 'seal.json')
    require(seal.get('agent_input') == store.registry['agent_input'], 'invalid_agent_input')
    require(start.get('task_id') == task['id'], 'invalid_state')
    require(all(start['attempt'][key] == attempt[key] for key in attempt if key != 'finished_at'), 'invalid_state')
    require(seal.get('task', {}).get('id') == task['id'] and seal['task']['final_attempt'] == attempt, 'invalid_state')
    for field, kind in (('scoring', 'rescore'), ('pricing', 'reprice')):
        if owner.get(field) is not None:
            store.item(owner[field], kind, task['id'], attempt['id'])
    if owner.get('outcome') is None:
        require(owner.get('scoring') is not None, 'invalid_state')
        score = store.item(owner['scoring'], 'rescore', task['id'], attempt['id'])
        require(score['scoring']['status'] == 'available', 'invalid_state')
        return seal['task'], None
    _receipt(owner['outcome'], store.inventory, prefix + 'outcome.json')
    outcome = store.document(prefix + 'outcome.json')
    require(
        outcome.get('schema_family') == 'pr_similarity_os.outcome'
        and type(outcome.get('schema_version')) is int
        and outcome['schema_version'] == 1
        and outcome.get('run_id') == store.registry['id']
        and outcome.get('seal') == owner['seal'],
        'invalid_state',
    )
    for field in ('id', 'final_attempt', 'setup', 'execution', 'changes', 'telemetry', 'infrastructure_error'):
        require(outcome['task'][field] == seal['task'][field], 'invalid_state')
    _validate_initial_scoring(prefix, attempt, task, seal, outcome, store.inventory, store.read_file)
    return outcome['task'], outcome['task']['scoring']


def _validate_selected_row(
    task: dict, selected: dict, native: dict, initial: dict | None, store: OperationStore
) -> None:
    """Bind rendered execution, score and price to state-selected native and host results."""
    for field in ('final_attempt', 'setup', 'execution', 'changes'):
        require(task[field] == native[field], 'invalid_state')
    expected_outcome = None if selected['outcome'] is None else selected['outcome']['path']
    require(task['artifacts']['outcome'] == expected_outcome, 'invalid_state')
    scoring = initial
    if selected.get('scoring') is not None:
        scoring = store.item(selected['scoring'], 'rescore', task['id'], selected['attempt']['id'])['scoring']
    require(task['scoring'] == scoring, 'invalid_state')
    for key, value in native['telemetry'].items():
        if key != 'api_equivalent':
            require(task['telemetry'][key] == value, 'invalid_state')
    pricing = native['telemetry']['api_equivalent']
    if selected.get('pricing') is not None:
        pricing = store.item(selected['pricing'], 'reprice', task['id'], selected['attempt']['id'])['api_equivalent']
    require(task['telemetry']['api_equivalent'] == pricing, 'invalid_state')


def _validate_initial_scoring(
    prefix: str,
    attempt: dict,
    report_task: dict,
    seal: dict,
    outcome: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> None:
    """Bind initial inline scoring to its immutable receipt and native metric."""
    receipt = outcome.get('scoring_result')
    _receipt(receipt, inventory, prefix + 'scoring.json')
    scoring = _document(prefix + 'scoring.json', inventory, read_file)
    require(
        scoring.get('schema_family') == 'pr_similarity_os.scoring'
        and type(scoring.get('schema_version')) is int
        and scoring.get('schema_version') == 1
        and scoring.get('run_id') == outcome.get('run_id')
        and scoring.get('task_id') == report_task['id']
        and scoring.get('attempt_id') == attempt['id']
        and scoring.get('seal') == outcome['seal']
        and scoring.get('metric') == seal.get('metric') == outcome.get('metric')
        and scoring.get('scoring') == outcome.get('task', {}).get('scoring'),
        'invalid_state',
    )
    require(
        scoring.get('error') == outcome.get('scoring_error')
        and scoring.get('scoring', {}).get('status') in {'available', 'unavailable'}
        and receipt in outcome.get('artifact_receipts', []),
        'invalid_state',
    )


def _validate_publication_selection(
    selected: object,
    closed: bool,
    report: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> None:
    """Validate publication provenance while allowing only its deliberately omitted nested payload."""
    require(
        isinstance(selected, dict)
        and isinstance(selected.get('id'), str)
        and isinstance(selected.get('request'), dict),
        'invalid_state',
    )
    assert isinstance(selected, dict)
    identifier = selected['id']
    prefix = 'operations/publish/' + identifier + '/'
    _receipt(selected['request'], inventory, prefix + 'request.json')
    request = _document(prefix + 'request.json', inventory, read_file)
    require(
        request.get('schema_family') == 'pr_similarity_os.publish_request'
        and request.get('schema_version') == 1
        and request.get('id') == identifier
        and request.get('run_id') == report['run']['id']
        and request.get('basket_sha256') == report['basket']['sha256']
        and request.get('config_identity_sha256') == report['config']['identity_sha256'],
        'invalid_state',
    )
    payload = request.get('payload')
    require(
        isinstance(payload, dict) and payload.get('path') == prefix + 'payload.tar.gz',
        'invalid_state',
    )
    assert isinstance(payload, dict)
    if payload['path'] in inventory:
        require(inventory[payload['path']] == payload, 'invalid_state')
    if closed:
        _receipt(selected.get('receipt'), inventory, prefix + 'receipt.json')
        receipt = _document(prefix + 'receipt.json', inventory, read_file)
        require(
            receipt.get('schema_family') == 'pr_similarity_os.publish_receipt'
            and receipt.get('schema_version') == 1
            and receipt.get('id') == identifier
            and receipt.get('run_id') == report['run']['id']
            and receipt.get('request') == selected['request'],
            'invalid_state',
        )


def _reference(path: str | None, inventory: dict[str, dict]) -> None:
    """Require each present report artifact to exist in the verified archive inventory."""
    if path is not None:
        safe_path(path)
        require(path in inventory, 'missing_artifact')


def _validate_archive_task(
    task: dict,
    source: dict,
    report: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> None:
    """Check frozen task ownership and all selected native/public artifact references."""
    require(all(task[k] == source[k] for k in ('workspace', 'agent_task', 'source', 'diff_policy')))
    require(all(task[k] == source['change_request'][k] for k in ('repository', 'number', 'url', 'title', 'kind')))
    require(all(task[k] == source['revisions'][k] for k in ('before', 'after', 'before_tree', 'after_tree')))
    _reference(task['changes']['diff'], inventory)
    for key in (
        'outcome',
        'triplets_manifest',
        'setup_log',
        'agent_events',
        'agent_log',
    ):
        _reference(task['artifacts'][key], inventory)
    _reference(task['artifacts']['effective_config']['path'], inventory)
    for key in ('session_db', 'usage_ledger'):
        entry = task['telemetry'][key]
        if entry:
            _reference(entry['path'], inventory)
            require(inventory[entry['path']]['sha256'] == entry['sha256'])
    for row in task['scoring']['files']:
        for role in ('base', 'human', 'ai'):
            _reference(row[role], inventory)
    if task['artifacts']['triplets_manifest'] is not None:
        validate_triplet_index(
            parse_json(read_file(task['artifacts']['triplets_manifest'])),
            task,
            inventory,
            run_id=report['run']['id'],
        )


def _source_path(value: str) -> str:
    """Require a normalized source path without repository metadata components."""
    path = safe_path(value)
    require('.git' not in path.split('/'), 'invalid_triplets')
    return path


def _triplet_prefix(task: dict) -> str:
    """Bind public role paths to the registered directory and selected attempt sequence."""
    directory, attempt = task.get('directory'), task['final_attempt']
    require(isinstance(directory, str) and attempt is not None, 'invalid_public_artifact')
    assert isinstance(directory, str) and isinstance(attempt, dict)
    require(
        re.fullmatch(r'tasks/[0-9]{3,}-[a-z0-9-]+', safe_path(directory)) is not None,
        'invalid_public_artifact',
    )
    sequence = attempt['sequence']
    require(type(sequence) is int and sequence >= 1, 'invalid_public_artifact')
    prefix = f'{directory}/attempts/{sequence:03d}/triplets/'
    require(
        task['artifacts']['triplets_manifest'] == prefix + 'index.json',
        'invalid_public_artifact',
    )
    return prefix


def _triplet_entry(entry: dict, number: int, prefix: str, inventory: dict[str, dict]) -> str:
    """Validate every existing receipt and require absent files to have no bytes or metadata."""
    require(
        isinstance(entry, dict) and set(entry) == {'path', 'number', 'exists', 'modes', 'files'},
        'invalid_triplets',
    )
    path = _source_path(entry['path'])
    require(type(entry['number']) is int and entry['number'] == number, 'invalid_triplets')
    for key in ('exists', 'modes', 'files'):
        require(
            isinstance(entry[key], dict) and set(entry[key]) == {'base', 'human', 'ai'},
            'invalid_triplets',
        )
    for role in ('base', 'human', 'ai'):
        exists, mode, receipt = (
            entry['exists'][role],
            entry['modes'][role],
            entry['files'][role],
        )
        require(type(exists) is bool, 'invalid_triplets')
        expected = triplet_path(number, path, role)
        if not exists:
            require(
                mode is None and receipt is None and prefix + expected not in inventory,
                'invalid_triplets',
            )
            continue
        require(mode in ('100644', '100755'), 'invalid_triplets')
        require(
            isinstance(receipt, dict) and set(receipt) == {'path', 'size_bytes', 'sha256'},
            'invalid_triplets',
        )
        require(
            receipt['path'] == expected and prefix + expected in inventory,
            'invalid_triplets',
        )
        actual = inventory[prefix + expected]
        require(
            type(receipt['size_bytes']) is int
            and receipt['size_bytes'] >= 0
            and receipt['size_bytes'] == actual['size_bytes']
            and receipt['sha256'] == actual['sha256'],
            'invalid_triplets',
        )
    return path


def validate_triplet_index(index: dict, task: dict, inventory: dict[str, dict], *, run_id: str) -> None:
    """Validate the exact schema-1 adjacent variants and its run/task/attempt/revision owner.

    Args:
        index: Decoded immutable triplet index.
        task: Selected report task with registered directory and final attempt.
        inventory: Archive members whose size and digest were verified from bytes.
        run_id: Owning report run identity.

    Raises:
        ViewerError: If ownership, exact paths, nullable states or receipts disagree.

    """
    require(
        isinstance(index, dict) and set(index) == {'version', 'run_id', 'task_id', 'attempt_id', 'source', 'entries'},
        'invalid_triplets',
    )
    require(type(index['version']) is int and index['version'] == 1, 'invalid_triplets')
    attempt = task['final_attempt']
    require(attempt is not None, 'invalid_triplets')
    require(
        index['run_id'] == run_id and index['task_id'] == task['id'] and index['attempt_id'] == attempt['id'],
        'invalid_triplets',
    )
    require(
        index['source'] == {'before': task['before'], 'after': task['after']},
        'invalid_triplets',
    )
    entries = index['entries']
    require(isinstance(entries, list), 'invalid_triplets')
    prefix = _triplet_prefix(task)
    rows = {}
    paths = []
    for number, entry in enumerate(entries, 1):
        path = _triplet_entry(entry, number, prefix, inventory)
        paths.append(path)
        rows[path] = entry
    require(paths == sorted(set(paths)), 'invalid_triplets')
    for scored in task['scoring']['files']:
        require(scored['path'] in rows, 'invalid_triplets')
        row = rows[scored['path']]
        require(scored['exists'] == row['exists'], 'invalid_triplets')
        for role in ('base', 'human', 'ai'):
            receipt = row['files'][role]
            expected = prefix + receipt['path'] if receipt is not None else None
            require(scored[role] == expected, 'invalid_triplets')


def _evidence_document(path: str, inventory: dict[str, dict], read_file: Callable[[str], bytes]) -> dict:
    """Read native ownership facts only after matching exact archived bytes and hashes."""
    require(path in inventory, 'invalid_inference')
    content = read_file(path)
    receipt = inventory[path]
    require(
        len(content) == receipt['size_bytes'] and digest(content) == receipt['sha256'],
        'invalid_inference',
    )
    value = parse_json(content)
    require(isinstance(value, dict), 'invalid_inference')
    assert isinstance(value, dict)
    return value


def _native_owner(value: dict, report: dict, family: str) -> None:
    """Bind a native start/seal to the same frozen run, basket and agent configuration."""
    require(
        value.get('schema_family') == 'pr_similarity_os.' + family
        and type(value.get('schema_version')) is int
        and value['schema_version'] == 1
        and value.get('run_id') == report['run']['id']
        and value.get('basket_sha256') == report['basket']['sha256']
        and value.get('config_identity_sha256') == report['config']['identity_sha256'],
        'invalid_inference',
    )


def _sealed_config(
    claim: dict,
    task: dict,
    report: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> dict:
    """Validate one selected or historical effective config against explicit native owners."""
    ref = safe_path(claim['source_ref'])
    require(re.fullmatch(r'agent/effective/[0-9a-f]{64}\.json', ref) is not None, 'invalid_inference')
    require(ref in inventory and ref.split('/')[-1] == inventory[ref]['sha256'] + '.json', 'invalid_inference')
    state = _evidence_document('_meta/state.json', inventory, read_file)
    matches = [item for row in state['tasks'] if row['id'] == task['id']
               for item in [*row['history'], row]
               if item.get('attempt') is not None and item['attempt']['id'] == claim['attempt_id']]
    require(len(matches) == 1, 'invalid_inference')
    selected = matches[0]
    sequence = selected['attempt']['sequence']
    prefix = task['directory'] + f'/attempts/{sequence:03d}/'
    require(selected['start']['path'] == prefix + 'start.json'
            and selected['seal']['path'] == prefix + 'seal.json', 'invalid_inference')
    final = task['final_attempt']
    require(isinstance(final, dict) and sequence <= final['sequence'], 'invalid_inference')
    if sequence == final['sequence']:
        require(claim['attempt_id'] == final['id'], 'invalid_inference')
        require(task['artifacts']['effective_config']['path'] == ref, 'invalid_inference')
    start = _evidence_document(prefix + 'start.json', inventory, read_file)
    _native_owner(start, report, 'start')
    attempt = start.get('attempt')
    require(isinstance(attempt, dict), 'invalid_inference')
    assert isinstance(attempt, dict)
    require(start.get('task_id') == task['id'], 'invalid_inference')
    require(
        attempt.get('id') == claim['attempt_id']
        and type(attempt.get('sequence')) is int
        and attempt['sequence'] == sequence,
        'invalid_inference',
    )
    seal = _evidence_document(prefix + 'seal.json', inventory, read_file)
    _native_owner(seal, report, 'seal')
    sealed_task = seal.get('task')
    require(isinstance(sealed_task, dict), 'invalid_inference')
    assert isinstance(sealed_task, dict)
    require(sealed_task.get('id') == task['id'], 'invalid_inference')
    sealed_attempt = sealed_task.get('final_attempt')
    require(isinstance(sealed_attempt, dict), 'invalid_inference')
    assert isinstance(sealed_attempt, dict)
    require(
        sealed_attempt.get('id') == claim['attempt_id']
        and type(sealed_attempt.get('sequence')) is int
        and sealed_attempt['sequence'] == sequence,
        'invalid_inference',
    )
    require(set(attempt) == set(sealed_attempt), 'invalid_inference')
    for key in attempt:
        if key != 'finished_at':
            require(attempt[key] == sealed_attempt[key], 'invalid_inference')
            if sequence == final['sequence']:
                require(attempt[key] == final[key], 'invalid_inference')
    for key in (
        'repository',
        'number',
        'kind',
        'source',
        'diff_policy',
        'before',
        'after',
        'before_tree',
        'after_tree',
        'workspace',
        'agent_task',
    ):
        require(sealed_task.get(key) == task[key], 'invalid_inference')
    artifacts = sealed_task.get('artifacts')
    require(isinstance(artifacts, dict), 'invalid_inference')
    assert isinstance(artifacts, dict)
    effective = artifacts.get('effective_config')
    require(
        isinstance(effective, dict) and effective.get('path') == ref,
        'invalid_inference',
    )
    _sealed_receipts(seal, prefix, ref, inventory, report["agent_input"])
    return _evidence_document(ref, inventory, read_file)


def _sealed_receipts(seal: dict, prefix: str, ref: str, inventory: dict[str, dict], agent_input: dict) -> None:
    """Require unique attempt-scoped seal receipts matching actual archive bytes."""
    receipts = seal.get('artifact_receipts')
    require(isinstance(receipts, list), 'invalid_inference')
    assert isinstance(receipts, list)
    require(seal.get('agent_input') == agent_input, 'invalid_inference')
    allowed = [agent_input['config'], *agent_input['resources'], inventory.get(ref)]
    paths = []
    for receipt in receipts:
        require(
            isinstance(receipt, dict) and set(receipt) == {'path', 'size_bytes', 'sha256'},
            'invalid_inference',
        )
        path = safe_path(receipt['path'])
        require((path.startswith(prefix) or receipt in allowed) and path in inventory, 'invalid_inference')
        require(
            type(receipt['size_bytes']) is int and receipt['size_bytes'] >= 0,
            'invalid_inference',
        )
        require(receipt == inventory[path], 'invalid_inference')
        paths.append(path)
    require(len(paths) == len(set(paths)) and ref in paths, 'invalid_inference')


def validate_inference_evidence(
    report: dict,
    identity: dict,
    inventory: dict[str, dict],
    read_file: Callable[[str], bytes],
) -> None:
    """Compare exact configured facts and each declared effective origin with owned sealed evidence.

    Committed run/state selection coverage is a separate validation gate. This
    function never adopts unreferenced attempt directories as inference facts.
    """
    require('inference' in report, 'invalid_inference')
    declared = report['inference']
    try:
        require(public_inference(declared) == declared, 'invalid_inference')
    except ValueError:
        raise ViewerError('invalid_inference', 'Unsupported inference evidence.') from None
    expected = build_inference(identity)
    require(declared['configured'] == expected['configured'], 'invalid_inference')
    tasks = {task.get('directory'): task for task in report['tasks']}
    require(len(tasks) == len(report['tasks']), 'invalid_inference')
    claims = declared['effective']['attempts']
    refs = [claim['source_ref'] for claim in claims]
    identifiers = [claim['attempt_id'] for claim in claims]
    require(
        len(identifiers) == len(set(identifiers)),
        'invalid_inference',
    )
    state = _evidence_document('_meta/state.json', inventory, read_file)
    by_id = {task['id']: task for task in report['tasks']}
    owners = {}
    captured = set()
    for selected in state['tasks']:
        for item in [*selected['history'], selected]:
            attempt = item.get('attempt')
            if attempt is None:
                continue
            require(attempt['id'] not in owners and selected['id'] in by_id, 'invalid_inference')
            owners[attempt['id']] = by_id[selected['id']]
            if item.get('seal') is not None:
                seal = _evidence_document(item['seal']['path'], inventory, read_file)
                sealed_task = seal.get('task')
                require(isinstance(sealed_task, dict), 'invalid_inference')
                artifacts = sealed_task.get('artifacts')
                require(isinstance(artifacts, dict), 'invalid_inference')
                effective = artifacts.get('effective_config')
                require(isinstance(effective, dict), 'invalid_inference')
                if effective.get('status') == 'saved':
                    captured.add(attempt['id'])
    require(set(identifiers) == captured, 'invalid_inference')
    configs = []
    for claim in claims:
        require(claim['attempt_id'] in owners, 'invalid_inference')
        native = _sealed_config(claim, owners[claim['attempt_id']], report, inventory, read_file)
        configs.append({'attempt_id': claim['attempt_id'], 'source_ref': claim['source_ref'], 'config': native})
    for task in report['tasks']:
        ref = task['artifacts']['effective_config']['path']
        if ref is not None:
            require(ref in refs, 'invalid_inference')
    require(declared == build_inference(identity, configs), 'invalid_inference')
