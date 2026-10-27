"""Frozen Decimal pricing operations selected only through authoritative run state."""

import math
import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path

from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.execution.telemetry import read_saved_telemetry
from benchmark.models import Usage, WireModel
from benchmark.models.evidence import (
    ArtifactReceipt,
    OperationCoverage,
    OperationDetails,
    PriceCoverage,
    PriceInputs,
    PriceItem,
    PriceRequest,
    PriceResult,
    PricingRequest,
    Seal,
)
from benchmark.models.pricing import Pricing
from benchmark.models.report import ReportError, ReportTelemetryApiEquivalent
from benchmark.models.state import StateTaskItem, StateTaskItemHistoryItem
from benchmark.models.telemetry import TelemetryReceipt
from benchmark.persistence.artifact_refs import read_reference, reference
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.operations import operation_identifier, validate_operation
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import (
    atomic_write,
    bytes_sha256,
    run_lock,
    sha256,
    write_json,
)
from benchmark.pricing.policy import (
    CALCULATOR_VERSION,
    PriceCalculation,
    PricingPolicy,
    load_policy,
    parse_policy,
    price_usage,
)
from benchmark.runtime.commands import Command


class PricingInputIdentity(WireModel):
    """Bind the calculator signature to one selected attempt's usage receipt."""

    task_id: str
    attempt_id: str
    usage: ArtifactReceipt | None
    reason: str | None


class PricingIdentity(WireModel):
    """Describe the semantic pricing inputs used to identify a no-op calculation."""

    policy: Pricing
    calculator: str
    attempts: list[PricingInputIdentity]
    final: list[list[str]]


@dataclass(frozen=True)
class OwnedAttempt:
    """Pair an explicit state attempt with its registered task identity."""

    task_id: str
    selection: StateTaskItem | StateTaskItemHistoryItem


def freeze_pricing(run_dir: Path, policy: PricingPolicy, command: Command, *, store: RunStore) -> PricingRequest:
    """Persist immutable tariffs and calculator before any native work begins."""
    lease = store.lease
    if store.directory != run_dir or lease is None or not lease.active or lease.directory != run_dir.resolve():
        raise BenchmarkError('writer_lease_required', 'Pricing inputs require the active owning run lease.')
    calculation = str(uuid.uuid4())
    relative = Path('operations/reprice') / calculation
    directory = run_dir / relative
    artifact_path(run_dir, (relative / 'request.json').as_posix())
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write(directory / 'policy.yaml', policy.content)
    frozen = [reference(run_dir, (relative / 'policy.yaml').as_posix())]
    baseline = store.registry.baseline
    request = PricingRequest(
        schema_family='pr_similarity_os.pricing_request',
        schema_version=2,
        id=calculation,
        kind='reprice',
        run_id=store.registry.id,
        basket_sha256=baseline.basket.sha256,
        config_identity_sha256=baseline.config.identity_sha256,
        invocation_id=command.id,
        calculation_id=calculation,
        policy_path=(relative / 'policy.yaml').as_posix(),
        policy_sha256=policy.sha256,
        calculator_version=CALCULATOR_VERSION,
        frozen_artifacts=frozen,
        status='pending',
    )
    write_json(directory / 'request.json', request)
    request.request_reference = reference(run_dir, (relative / 'request.json').as_posix())
    command.metadata.pricing_request = request
    command.update()
    return request


def _plain(value: PriceCalculation) -> PriceResult:
    """Preserve Decimal result precision as strings at the artifact boundary."""
    result = PriceResult(
        status=value.status,
        amount=None if value.amount is None else str(value.amount),
        known_subtotal=str(value.known_subtotal),
        priced_requests=value.priced_requests,
        total_requests=value.total_requests,
        requests=[
            PriceRequest(id=item.id, amount=None if item.amount is None else str(item.amount), reason=item.reason)
            for item in value.requests
        ],
    )
    if value.reason is not None:
        result.reason = value.reason
    return result


def _estimate(result: PriceCalculation, request: PricingRequest, usage_sha: str | None) -> ReportTelemetryApiEquivalent:
    """Expose report amounts with explicit coverage and exact pricing provenance."""
    amount = None if result.amount is None else float(result.amount)
    if amount is not None and not math.isfinite(amount):
        raise BenchmarkError('invalid_pricing', 'API-equivalent amount cannot be represented in the report.')
    return ReportTelemetryApiEquivalent(
        status=result.status,
        amount=amount,
        currency='USD',
        basis='api_equivalent',
        calculation_id=request.calculation_id,
        pricing_sha256=request.policy_sha256,
        calculator_version=request.calculator_version,
        usage_sha256=usage_sha,
        priced_requests=result.priced_requests,
        total_requests=result.total_requests,
    )


def _unknown(reason: str | None) -> PriceCalculation:
    """Record unmeasured usage without implying a free model call."""
    return PriceCalculation(
        status='unavailable',
        amount=None,
        known_subtotal=Decimal(0),
        priced_requests=0,
        total_requests=None,
        requests=[],
        reason=reason,
    )


def _owners(store: RunStore) -> list[OwnedAttempt]:
    """List only explicit current and historical attempts registered in state."""
    return [
        OwnedAttempt(task.id, item)
        for task in store.state.tasks
        for item in [*task.history, task]
        if item.attempt is not None
    ]


def _saved_usage(store: RunStore, owner: OwnedAttempt) -> tuple[Usage | None, PriceInputs]:
    """Verify every usage input against native seal ownership and exact-byte receipts."""
    selected = owner.selection
    if selected.seal is None:
        return None, PriceInputs(reason='native_unsealed', usage=None, receipt=None, db=None)
    native = read_reference(store.directory, selected.seal, Seal)
    row, attempt = native.task, selected.attempt
    assert attempt is not None
    directory = store.attempt_directory(owner.task_id, attempt.sequence) / 'telemetry'
    owned = {item.path: item for item in native.artifact_receipts}
    receipt_path = (directory / 'receipt.json').as_posix()
    if receipt_path not in owned:
        if row.telemetry.usage_ledger is not None:
            raise BenchmarkError('usage_receipt_missing', 'Native seal does not bind its declared usage.')
        return None, PriceInputs(reason='usage_receipt_missing', usage=None, receipt=None, db=None)
    receipt = read_reference(store.directory, owned[receipt_path], TelemetryReceipt)
    expected = (store.registry.id, owner.task_id, attempt.id)
    if (receipt.run_id, receipt.task_id, receipt.attempt_id) != expected:
        raise BenchmarkError('usage_owner_mismatch', 'Usage receipt belongs to a different native attempt.')
    inputs = PriceInputs(reason=None, usage=None, receipt=owned[receipt_path], db=None)
    if receipt.status != 'saved':
        inputs.reason = receipt.status
        return None, inputs
    usage_path, db_path = (directory / 'usage.json').as_posix(), (directory / 'opencode.sqlite.xz').as_posix()
    if usage_path not in owned or db_path not in owned:
        raise BenchmarkError('usage_receipt_missing', 'Native seal lacks the original ledger or sanitized database.')
    saved = read_saved_telemetry(
        store.directory / directory, run_id=expected[0], task_id=expected[1], attempt_id=expected[2]
    )
    if saved.data.usage_ledger != row.telemetry.usage_ledger:
        raise BenchmarkError('usage_digest_mismatch', 'Native usage projection disagrees with its ledger receipt.')
    ledger = read_reference(store.directory, owned[usage_path], Usage)
    if reference(store.directory, db_path) != owned[db_path]:
        raise BenchmarkError('usage_digest_mismatch', 'Sanitized usage database differs from native evidence.')
    inputs.usage, inputs.db = owned[usage_path], owned[db_path]
    return ledger, inputs


def _verify_request(run_dir: Path, request: PricingRequest, store: RunStore) -> PricingPolicy:
    """Use frozen inputs, rejecting changed calculator versions or tariff bytes."""
    identifier = operation_identifier(request.calculation_id)
    prefix = f'operations/reprice/{identifier}/'
    if request.request_reference is None or request.request_reference.path != prefix + 'request.json':
        raise BenchmarkError('pricing_owner_mismatch', 'Pricing request has an unexpected operation path.')
    frozen = read_reference(run_dir, request.request_reference, PricingRequest)
    baseline = store.registry.baseline
    # Status and request_reference are coordinator state, not frozen calculator inputs.
    unchanged = all(
        left == right
        for left, right in (
            (request.schema_family, frozen.schema_family),
            (request.schema_version, frozen.schema_version),
            (request.id, frozen.id),
            (request.kind, frozen.kind),
            (request.run_id, frozen.run_id),
            (request.basket_sha256, frozen.basket_sha256),
            (request.config_identity_sha256, frozen.config_identity_sha256),
            (request.invocation_id, frozen.invocation_id),
            (request.calculation_id, frozen.calculation_id),
            (request.policy_path, frozen.policy_path),
            (request.policy_sha256, frozen.policy_sha256),
            (request.calculator_version, frozen.calculator_version),
            (request.frozen_artifacts, frozen.frozen_artifacts),
        )
    )
    if (
        frozen.id != identifier
        or frozen.run_id != store.registry.id
        or frozen.basket_sha256 != baseline.basket.sha256
        or frozen.config_identity_sha256 != baseline.config.identity_sha256
        or not unchanged
    ):
        raise BenchmarkError('pricing_owner_mismatch', 'Pricing request differs from its frozen run inputs.')
    for item in frozen.frozen_artifacts:
        if not item.path.startswith(prefix) or reference(run_dir, item.path) != item:
            raise BenchmarkError('pricing_changed', 'Frozen pricing evidence differs from its original bytes.')
    if frozen.calculator_version != CALCULATOR_VERSION:
        raise BenchmarkError('calculator_changed', 'Installed calculator version differs from frozen operation inputs.')
    content = artifact_path(run_dir, frozen.policy_path, must_exist=True).read_bytes()
    if bytes_sha256(content) != frozen.policy_sha256:
        raise BenchmarkError('pricing_changed', 'Frozen tariff bytes differ from the operation request.')
    return parse_policy(content)


def _coverage(items: list[PriceItem], final: set[tuple[str, str]], missing_tasks: int = 0) -> OperationCoverage:
    """Sum exact known subtotals while keeping final and all-attempt coverage separate."""

    def summarize(rows: list[PriceItem], missing: int) -> PriceCoverage:
        """Keep incomplete attempts separate from measured zero in each total."""
        complete = sum(row.result.status == 'complete' for row in rows)
        values = [Decimal(row.result.known_subtotal) for row in rows]
        with localcontext() as context:
            context.prec = max(
                64,
                max((len(value.as_tuple().digits) + abs(value.adjusted()) for value in values), default=1)
                + len(str(len(values)))
                + 8,
            )
            subtotal = sum(values, Decimal(0))
        requests = [row.result.total_requests for row in rows]
        full = complete == len(rows) and not missing
        return PriceCoverage(
            status='complete' if full else 'partial',
            amount=str(subtotal) if full else None,
            known_subtotal=str(subtotal),
            total_attempts=len(rows),
            complete_attempts=complete,
            missing_tasks=missing,
            priced_requests=sum(row.result.priced_requests for row in rows),
            total_requests=(
                None
                if any(value is None for value in requests)
                else sum(value for value in requests if value is not None)
            ),
        )

    return OperationCoverage(
        final=summarize([item for item in items if (item.task_id, item.attempt_id) in final], missing_tasks),
        all_attempts=summarize(items, 0),
    )


def _calculate(
    store: RunStore, policy: PricingPolicy, request: PricingRequest
) -> tuple[list[PriceItem], OperationCoverage, str]:
    """Calculate state-owned attempts without updating native usage or selected prices."""
    items: list[PriceItem] = []
    inputs: list[PricingInputIdentity] = []
    for owner in _owners(store):
        attempt = owner.selection.attempt
        assert attempt is not None
        ledger, evidence = _saved_usage(store, owner)
        priced = _unknown(evidence.reason) if ledger is None else price_usage(ledger, policy.data)
        usage_hash = None if evidence.usage is None else evidence.usage.sha256
        items.append(
            PriceItem(
                task_id=owner.task_id,
                attempt_id=attempt.id,
                sequence=attempt.sequence,
                api_equivalent=_estimate(priced, request, usage_hash),
                result=_plain(priced),
                inputs=evidence,
            )
        )
        inputs.append(
            PricingInputIdentity(
                task_id=owner.task_id, attempt_id=attempt.id, usage=evidence.usage, reason=evidence.reason
            )
        )
    final = {(task.id, task.attempt.id) for task in store.state.tasks if task.attempt is not None}
    coverage = _coverage(items, final, sum(task.attempt is None for task in store.state.tasks))
    signature = sha256(
        PricingIdentity(
            policy=policy.data,
            calculator=request.calculator_version,
            attempts=inputs,
            final=[list(pair) for pair in sorted(final)],
        )
    )
    return items, coverage, signature


def _record_failure(
    run_dir: Path, request: PricingRequest, command: Command, store: RunStore, error: BaseException
) -> None:
    """Close an untouched pending operation as failed without overwriting immutable facts."""
    directory = run_dir / 'operations/reprice' / request.calculation_id
    if any(
        (directory / name).exists() for name in ('input_state.json', 'input_report.json', 'result.json', 'receipt.json')
    ):
        return
    try:
        store.prepare_operation(
            'reprice',
            [],
            invocation_id=command.id,
            operation_id=request.calculation_id,
            preallocated=True,
            status='failed',
            extra=OperationDetails(
                request=request.request_reference,
                error=ReportError(
                    code=error.code if isinstance(error, BenchmarkError) else 'reprice_failed',
                    message='Pricing failed; previous selection is preserved.',
                ),
            ),
        )
    except OPERATION_ERRORS as secondary:
        command.diagnostic(secondary)


def apply_pricing(run_dir: Path, request: PricingRequest, command: Command, *, store: RunStore) -> int:
    """Complete frozen pricing after native execution, then select only its exact operation."""
    try:
        policy = _verify_request(run_dir, request, store)
        items, coverage, signature = _calculate(store, policy, request)
        previous = store.last_operation('reprice')
        no_op = False
        if previous is not None:
            prior = validate_operation(run_dir, store.registry, previous, 'reprice')
            no_op = prior.receipt.details.inputs_identity_sha256 == signature
        gaps = any(value.status != 'complete' for value in (coverage.final, coverage.all_attempts))
        status = 'no_op' if no_op else ('completed_with_gaps' if gaps else 'completed')
        selected = store.prepare_operation(
            'reprice',
            items,
            invocation_id=command.id,
            operation_id=request.calculation_id,
            preallocated=True,
            status=status,
            extra=OperationDetails(
                inputs_identity_sha256=signature,
                coverage=coverage,
                request=request.request_reference,
                frozen_artifacts=request.frozen_artifacts,
            ),
        )
        if not no_op:
            store.accept_operation(selected)
        request.status = status
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
        request.status = 'committed' if accepted is not None and accepted.id == request.calculation_id else 'failed'
        command.update()
        raise


def reprice(run_dir: Path, pricing_path: Path, *, level: str = 'INFO') -> int:
    """Price state-selected native usage offline under one reusable writer lease."""
    with run_lock(run_dir) as lease:
        store = RunStore.open(run_dir, lease=lease)
        report = store.rebuild()
        if report.run.status in ('running', 'stopping'):
            raise BenchmarkError('run_active', 'Repricing requires a stopped run.')
        policy = load_policy(pricing_path)
        command = Command(run_dir, 'reprice', level=level, previous=report.run.current_invocation_id)
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
