"""Frozen Decimal pricing operations selected only through authoritative run state."""

import math
import sys
import uuid
from decimal import Decimal, localcontext
from importlib.resources import files
from pathlib import Path

from benchmark.artifact_refs import read_reference, reference
from benchmark.commands import Command
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.layout import artifact_path
from benchmark.operations import operation_identifier, validate_operation
from benchmark.pricing import CALCULATOR_VERSION, PricingPolicy, load_policy, parse_policy, price_usage
from benchmark.run_store import RunStore
from benchmark.storage import atomic_write, bytes_sha256, read_json, run_lock, sha256, write_json
from benchmark.telemetry import read_saved_telemetry

_CALCULATOR_FILES = (
    'pricing.py',
    'repricing.py',
    'usage.py',
    'contracts.py',
    'config.py',
    'errors.py',
    'storage.py',
    'requirements.lock',
    'schemas/benchmark-pricing-v1.schema.json',
    'schemas/benchmark-usage-v1.schema.json',
)


def calculator_snapshot() -> tuple[dict, dict[str, bytes]]:
    """Freeze source and dependencies for audit without executing archived code."""
    resources = files('benchmark')
    payload = {name: resources.joinpath(name).read_bytes() for name in _CALCULATOR_FILES}
    manifest = {
        'version': CALCULATOR_VERSION,
        'python_version': sys.version.split()[0],
        'files': {name: bytes_sha256(content) for name, content in payload.items()},
    }
    return manifest, payload


def freeze_pricing(run_dir: Path, policy: PricingPolicy, command: Command, *, store: RunStore) -> dict:
    """Persist immutable tariffs and calculator before any native work begins."""
    lease = store.lease
    if store.directory != run_dir or lease is None or not lease.active or lease.directory != run_dir.resolve():
        raise BenchmarkError('writer_lease_required', 'Pricing inputs require the active owning run lease.')
    calculation = str(uuid.uuid4())
    relative = Path('operations/reprice') / calculation
    directory = run_dir / relative
    artifact_path(run_dir, (relative / 'request.json').as_posix())
    directory.mkdir(parents=True, exist_ok=False)
    manifest, payload = calculator_snapshot()
    atomic_write(directory / 'policy.yaml', policy.content)
    for name, content in payload.items():
        atomic_write(directory / 'calculator' / name, content)
    write_json(directory / 'calculator/manifest.json', manifest)
    frozen = [reference(run_dir, (relative / 'policy.yaml').as_posix())]
    frozen.extend(reference(run_dir, (relative / 'calculator' / name).as_posix()) for name in payload)
    frozen.append(reference(run_dir, (relative / 'calculator/manifest.json').as_posix()))
    baseline = store.registry['baseline']
    request = {
        'schema_family': 'pr_similarity_os.pricing_request',
        'schema_version': 1,
        'id': calculation,
        'kind': 'reprice',
        'run_id': store.registry['id'],
        'basket_sha256': baseline['basket']['sha256'],
        'config_identity_sha256': baseline['config']['identity_sha256'],
        'invocation_id': command.id,
        'calculation_id': calculation,
        'policy_path': (relative / 'policy.yaml').as_posix(),
        'policy_sha256': policy.sha256,
        'calculator_path': (relative / 'calculator').as_posix(),
        'calculator_sha256': sha256(manifest),
        'frozen_artifacts': frozen,
        'status': 'pending',
    }
    write_json(directory / 'request.json', request)
    request['request_reference'] = reference(run_dir, (relative / 'request.json').as_posix())
    command.metadata['pricing_request'] = request
    command.update()
    return request


def _plain(value: object) -> object:
    """Preserve Decimal result precision as JSON strings."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _estimate(result: dict, request: dict, usage_sha: str | None) -> dict:
    """Expose report amounts with explicit coverage and exact pricing provenance."""
    amount = None if result['amount'] is None else float(result['amount'])
    if amount is not None and not math.isfinite(amount):
        raise BenchmarkError('invalid_pricing', 'API-equivalent amount cannot be represented in the report.')
    return {
        'status': result['status'],
        'amount': amount,
        'currency': 'USD',
        'basis': 'api_equivalent',
        'calculation_id': request['calculation_id'],
        'pricing_sha256': request['policy_sha256'],
        'calculator_sha256': request['calculator_sha256'],
        'usage_sha256': usage_sha,
        'priced_requests': result['priced_requests'],
        'total_requests': result['total_requests'],
    }


def _unknown(reason: str) -> dict:
    """Record unmeasured usage without implying a free model call."""
    return {
        'status': 'unavailable',
        'amount': None,
        'known_subtotal': Decimal(0),
        'priced_requests': 0,
        'total_requests': None,
        'requests': [],
        'reason': reason,
    }


def _owners(store: RunStore) -> list[dict]:
    """List only explicit current and historical attempts registered in state."""
    return [
        {**item, 'task_id': task['id']}
        for task in store.state['tasks']
        for item in [*task['history'], task]
        if item['attempt'] is not None
    ]


def _saved_usage(store: RunStore, owner: dict) -> tuple[dict | None, dict]:
    """Verify every usage input against native seal ownership and exact-byte receipts."""
    if owner['seal'] is None:
        return None, {'reason': 'native_unsealed', 'usage': None, 'receipt': None, 'db': None}
    native = read_reference(store.directory, owner['seal'])
    row, attempt = native['task'], owner['attempt']
    directory = store.attempt_directory(owner['task_id'], attempt['sequence']) / 'telemetry'
    owned = {item['path']: item for item in native['artifact_receipts']}
    receipt_path = (directory / 'receipt.json').as_posix()
    if receipt_path not in owned:
        if row['telemetry']['usage_ledger'] is not None:
            raise BenchmarkError('usage_receipt_missing', 'Native seal does not bind its declared usage.')
        return None, {'reason': 'usage_receipt_missing', 'usage': None, 'receipt': None, 'db': None}
    receipt = read_reference(store.directory, owned[receipt_path])
    expected = (store.registry['id'], owner['task_id'], attempt['id'])
    if (receipt['run_id'], receipt['task_id'], receipt['attempt_id']) != expected:
        raise BenchmarkError('usage_owner_mismatch', 'Usage receipt belongs to a different native attempt.')
    inputs = {'reason': None, 'usage': None, 'receipt': owned[receipt_path], 'db': None}
    if receipt['status'] != 'saved':
        inputs['reason'] = receipt['status']
        return None, inputs
    usage_path, db_path = (directory / 'usage.json').as_posix(), (directory / 'opencode.sqlite.xz').as_posix()
    if usage_path not in owned or db_path not in owned:
        raise BenchmarkError('usage_receipt_missing', 'Native seal lacks the original ledger or sanitized database.')
    saved = read_saved_telemetry(
        store.directory / directory, run_id=expected[0], task_id=expected[1], attempt_id=expected[2]
    )
    if saved.data['usage_ledger'] != row['telemetry']['usage_ledger']:
        raise BenchmarkError('usage_digest_mismatch', 'Native usage projection disagrees with its ledger receipt.')
    ledger = read_reference(store.directory, owned[usage_path])
    # Verify the compressed projection bytes too, even though pricing reads the ledger.
    actual = reference(store.directory, db_path)
    if actual != owned[db_path]:
        raise BenchmarkError('usage_digest_mismatch', 'Sanitized usage database differs from native evidence.')
    inputs.update(usage=owned[usage_path], db=owned[db_path])
    return ledger, inputs


def _verify_request(run_dir: Path, request: dict, store: RunStore) -> PricingPolicy:
    """Use only the frozen operation inputs, rejecting changed installed code or tariff bytes."""
    identifier = operation_identifier(request['calculation_id'])
    prefix = f'operations/reprice/{identifier}/'
    expected = prefix + 'request.json'
    if request['request_reference']['path'] != expected:
        raise BenchmarkError('pricing_owner_mismatch', 'Pricing request has an unexpected operation path.')
    frozen = read_reference(run_dir, request['request_reference'])
    baseline = store.registry['baseline']
    if (
        frozen['id'] != identifier
        or frozen['run_id'] != store.registry['id']
        or frozen['basket_sha256'] != baseline['basket']['sha256']
        or frozen['config_identity_sha256'] != baseline['config']['identity_sha256']
        or any(request[key] != frozen[key] for key in frozen if key != 'status')
    ):
        raise BenchmarkError('pricing_owner_mismatch', 'Pricing request differs from its frozen run inputs.')
    for item in frozen['frozen_artifacts']:
        if not item['path'].startswith(prefix) or reference(run_dir, item['path']) != item:
            raise BenchmarkError('pricing_changed', 'Frozen pricing evidence differs from its original bytes.')
    manifest, _payload = calculator_snapshot()
    saved = read_json(artifact_path(run_dir, frozen['calculator_path'] + '/manifest.json', must_exist=True))
    if sha256(manifest) != frozen['calculator_sha256'] or sha256(saved) != frozen['calculator_sha256']:
        raise BenchmarkError('calculator_changed', 'Installed calculator differs from the frozen operation inputs.')
    content = artifact_path(run_dir, frozen['policy_path'], must_exist=True).read_bytes()
    if bytes_sha256(content) != frozen['policy_sha256']:
        raise BenchmarkError('pricing_changed', 'Frozen tariff bytes differ from the operation request.')
    return parse_policy(content)


def _coverage(items: list[dict], final: set[tuple[str, str]], missing_tasks: int = 0) -> dict:
    """Sum exact known subtotals while keeping final and all-attempt coverage separate."""

    def summarize(rows: list[dict], missing: int) -> dict:
        """Keep an incomplete attempt from being replaced by zero in a total."""
        complete = sum(row['result']['status'] == 'complete' for row in rows)
        values = [Decimal(row['result']['known_subtotal']) for row in rows]
        with localcontext() as context:
            context.prec = max(
                64,
                max((len(value.as_tuple().digits) + abs(value.adjusted()) for value in values), default=1)
                + len(str(len(values)))
                + 8,
            )
            subtotal = sum(values, Decimal(0))
        requests = [row['result']['total_requests'] for row in rows]
        full = complete == len(rows) and not missing
        return {
            'status': 'complete' if full else 'partial',
            'amount': str(subtotal) if full else None,
            'known_subtotal': str(subtotal),
            'total_attempts': len(rows),
            'complete_attempts': complete,
            'missing_tasks': missing,
            'priced_requests': sum(row['result']['priced_requests'] for row in rows),
            'total_requests': None if any(value is None for value in requests) else sum(requests),
        }

    return {
        'final': summarize([item for item in items if (item['task_id'], item['attempt_id']) in final], missing_tasks),
        'all_attempts': summarize(items, 0),
    }


def _calculate(store: RunStore, policy: PricingPolicy, request: dict) -> tuple[list[dict], dict, str]:
    """Calculate state-owned attempts without updating native usage or selected prices."""
    items, inputs = [], []
    for owner in _owners(store):
        attempt = owner['attempt']
        ledger, evidence = _saved_usage(store, owner)
        priced = _unknown(evidence['reason']) if ledger is None else price_usage(ledger, policy.data)
        usage_hash = None if evidence['usage'] is None else evidence['usage']['sha256']
        items.append(
            {
                'task_id': owner['task_id'],
                'attempt_id': attempt['id'],
                'sequence': attempt['sequence'],
                'api_equivalent': _estimate(priced, request, usage_hash),
                'result': _plain(priced),
                'inputs': evidence,
            }
        )
        inputs.append(
            {
                'task_id': owner['task_id'],
                'attempt_id': attempt['id'],
                'usage': evidence['usage'],
                'reason': evidence['reason'],
            }
        )
    final = {(task['id'], task['attempt']['id']) for task in store.state['tasks'] if task['attempt'] is not None}
    coverage = _coverage(items, final, sum(task['attempt'] is None for task in store.state['tasks']))
    signature = sha256(
        {'policy': policy.data, 'calculator': request['calculator_sha256'], 'attempts': inputs, 'final': sorted(final)}
    )
    return items, coverage, signature


def _record_failure(run_dir: Path, request: dict, command: Command, store: RunStore, error: BaseException) -> None:
    """Close an untouched pending operation as failed without overwriting immutable facts."""
    directory = run_dir / 'operations/reprice' / request['calculation_id']
    if any(
        (directory / name).exists() for name in ('input_state.json', 'input_report.json', 'result.json', 'receipt.json')
    ):
        return
    try:
        store.prepare_operation(
            'reprice',
            [],
            invocation_id=command.id,
            operation_id=request['calculation_id'],
            preallocated=True,
            status='failed',
            extra={
                'request': request['request_reference'],
                'error': {
                    'code': getattr(error, 'code', 'reprice_failed'),
                    'message': 'Pricing failed; previous selection is preserved.',
                },
            },
        )
    except OPERATION_ERRORS as secondary:
        command.diagnostic(secondary)


def apply_pricing(run_dir: Path, request: dict, command: Command, *, store: RunStore) -> int:
    """Complete frozen pricing after native execution, then select only its exact operation."""
    try:
        policy = _verify_request(run_dir, request, store)
        items, coverage, signature = _calculate(store, policy, request)
        previous = store.last_operation('reprice')
        no_op = False
        if previous is not None:
            prior = validate_operation(run_dir, store.registry, previous, 'reprice')
            no_op = prior.receipt['details'].get('inputs_identity_sha256') == signature
        gaps = any(value['status'] != 'complete' for value in coverage.values())
        status = 'no_op' if no_op else ('completed_with_gaps' if gaps else 'completed')
        selected = store.prepare_operation(
            'reprice',
            items,
            invocation_id=command.id,
            operation_id=request['calculation_id'],
            preallocated=True,
            status=status,
            extra={
                'inputs_identity_sha256': signature,
                'coverage': coverage,
                'request': request['request_reference'],
                'frozen_artifacts': request['frozen_artifacts'],
            },
        )
        if not no_op:
            store.accept_operation(selected)
        request['status'] = status
        command.update()
        command.message(
            'Pricing inputs are unchanged; recorded a no-op.'
            if no_op
            else 'Selected API-equivalent pricing from saved usage.'
        )
        if gaps:
            command.message(
                'API-equivalent pricing has gaps; known subtotals and coverage remain explicit.', level='WARNING'
            )
        return 1 if gaps else 0
    except OPERATION_ERRORS as error:
        command.diagnostic(error)
        _record_failure(run_dir, request, command, store, error)
        accepted = store.last_operation('reprice')
        request['status'] = (
            'committed' if accepted is not None and accepted['id'] == request['calculation_id'] else 'failed'
        )
        command.update()
        raise


def reprice(run_dir: Path, pricing_path: Path, *, level: str = 'INFO') -> int:
    """Price state-selected native usage offline under one reusable writer lease."""
    with run_lock(run_dir) as lease:
        store = RunStore.open(run_dir, lease=lease)
        report = store.rebuild()
        if report['run']['status'] in ('running', 'stopping'):
            raise BenchmarkError('run_active', 'Repricing requires a stopped run.')
        policy = load_policy(pricing_path)
        command = Command(run_dir, 'reprice', level=level, previous=report['run']['current_invocation_id'])
        try:
            request = freeze_pricing(run_dir, policy, command, store=store)
            status = apply_pricing(run_dir, request, command, store=store)
            command.finish('completed' if status == 0 else 'completed_with_gaps')
            return status
        except OPERATION_ERRORS:
            command.finish('failed')
            raise
        finally:
            command.close()
