"""Lossless cost coverage over current and historical state-selected native attempts."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal, Protocol

from benchmark.models import Run, State, Usage, WireModel
from benchmark.models.base import ArtifactReceipt as Receipt
from benchmark.models.evidence import (
    ArtifactReceipt,
    OperationResult,
    OperationSelection,
    Seal,
)
from benchmark.models.report import ReportCosts
from benchmark.models.report import ReportCostsFinalAttempts as CostScope
from benchmark.models.report import (
    ReportCostsFinalAttemptsNativeReported as CostSummary,
)
from benchmark.models.report import (
    ReportCostsFinalAttemptsNativeReportedCoverage as CostCoverage,
)
from benchmark.models.state import StateTaskItem, StateTaskItemHistoryItem


class CostEvidenceError(Exception):
    """Report safe contract failures independently of either caller's runtime errors."""

    def __init__(self, code: str, message: str) -> None:
        """Retain only an explicit stable code and credential-free explanation."""
        super().__init__(message)
        self.code = code
        self.message = message


class CostStore(Protocol):
    """Expose authoritative selections without importing the snapshot implementation."""

    registry: Run
    state: State

    def read_artifact[T: WireModel](self, receipt: Receipt, model: type[T]) -> T:
        """Read an exact immutable artifact after adapter-owned size, digest and path validation."""
        ...

    def price_result(self, selection: OperationSelection) -> OperationResult:
        """Return the exact reprice result after adapter-owned operation provenance validation."""
        ...

    def validate_usage(self, ledger: Usage) -> None:
        """Prove the caller's usage schema, request uniqueness and session ownership."""
        ...

    def last_operation(self, kind: str) -> OperationSelection | None:
        """Return the exact accepted root operation for the requested result kind."""
        ...


def _sum(values: list[Decimal]) -> Decimal:
    """Sum stored monetary values without the ambient Decimal context rounding them."""
    with localcontext() as context:
        context.prec = max(
            64,
            max((len(value.as_tuple().digits) + abs(value.adjusted()) for value in values), default=1)
            + len(str(len(values)))
            + 8,
        )
        return sum(values, Decimal(0))


@dataclass(frozen=True)
class CostObservation:
    """Keep one attempt's measured amount and coverage without inventing zeros."""

    subtotal: Decimal = Decimal(0)
    complete: bool = False
    known: bool = False
    known_requests: int = 0
    total_requests: int | None = None
    started: bool | None = None


type AttemptOwner = StateTaskItem | StateTaskItemHistoryItem


def _native(
    store: CostStore, owner: AttemptOwner, task_id: str
) -> tuple[CostObservation, Usage | None, ArtifactReceipt | None]:
    """Read request-level reported cost only from a signed, attempt-owned usage ledger."""
    if owner.seal is None:
        return CostObservation(), None, None
    native = store.read_artifact(owner.seal, Seal)
    row = native.task
    started = row.execution.status != 'not_started'
    if native.native_timing is not None:
        started = native.native_timing.started_at is not None
    ledger_ref = row.telemetry.usage_ledger
    if ledger_ref is None:
        return CostObservation(started=started), None, None
    receipt = next((item for item in reversed(native.artifact_receipts) if item.path == ledger_ref.path), None)
    if receipt is None:
        return CostObservation(started=started), None, None
    if receipt.sha256 != ledger_ref.sha256:
        raise CostEvidenceError('usage_digest_mismatch', 'Declared cost ledger differs from its native seal receipt.')
    ledger = store.read_artifact(receipt, Usage)
    store.validate_usage(ledger)
    if owner.attempt is None:
        raise CostEvidenceError('usage_owner_mismatch', 'Cost summary ledger belongs to a different native attempt.')
    if (ledger.run_id, ledger.task_id, ledger.attempt_id) != (
        store.registry.id,
        task_id,
        owner.attempt.id,
    ):
        raise CostEvidenceError('usage_owner_mismatch', 'Cost summary ledger belongs to a different native attempt.')
    requests = ledger.requests
    known = [
        Decimal(str(item.reported_cost_usd))
        for item in requests
        if item.complete and item.reported_cost_usd is not None
    ]
    complete = ledger.complete and len(known) == len(requests)
    value = CostObservation(
        subtotal=_sum(known),
        complete=complete,
        known=bool(known) or complete,
        known_requests=len(known),
        total_requests=len(requests),
        started=started,
    )
    return value, ledger, receipt


def _priced(
    store: CostStore, owner: AttemptOwner, task_id: str, ledger: Usage | None, ledger_ref: ArtifactReceipt | None
) -> CostObservation:
    """Use this attempt's selected price rather than copying a previous root receipt total."""
    total = None if ledger is None else len(ledger.requests)
    selection = owner.pricing
    if selection is None:
        return CostObservation(total_requests=total)
    result_document = store.price_result(selection)
    if owner.attempt is None:
        raise CostEvidenceError('operation_owner_mismatch', 'Selected cost has no unique registered attempt result.')
    matches = [
        item
        for item in result_document.attempts or []
        if (item.task_id, item.attempt_id) == (task_id, owner.attempt.id)
    ]
    if len(matches) != 1:
        raise CostEvidenceError('operation_owner_mismatch', 'Selected cost has no unique registered attempt result.')
    item = matches[0]
    if item.inputs is None or item.result is None:
        raise CostEvidenceError('invalid_pricing', 'Selected pricing lacks its inputs or result.')
    source = item.inputs.usage
    if source is not None and (ledger_ref is None or source != ledger_ref):
        raise CostEvidenceError('usage_digest_mismatch', 'Selected price differs from current sealed usage.')
    result = item.result
    complete = result.status == 'complete'
    subtotal = Decimal(result.known_subtotal)
    if source is None and (complete or result.priced_requests > 0 or subtotal > 0):
        raise CostEvidenceError('usage_receipt_missing', 'Known selected pricing requires its sealed usage input.')
    if not subtotal.is_finite() or subtotal < 0:
        raise CostEvidenceError('invalid_pricing', 'Selected cost subtotal must be finite and nonnegative.')
    return CostObservation(
        subtotal=subtotal,
        complete=complete,
        known=result.priced_requests > 0 or complete,
        known_requests=result.priced_requests,
        total_requests=total if total is not None else result.total_requests,
    )


def _summary(
    values: list[CostObservation], missing: int, *, requested: bool = True, native: bool = False
) -> CostSummary:
    """Keep complete amounts, known subtotals and attempt/request coverage independent."""
    complete = sum(value.complete for value in values)
    known = sum(value.known for value in values)
    full = bool(values) and complete == len(values) and not missing
    subtotal = _sum([value.subtotal for value in values])
    totals = [value.total_requests for value in values]
    status: Literal['not_requested', 'not_started', 'complete', 'partial', 'unavailable']
    if not requested:
        status = 'not_requested'
    elif not values or (native and not known and all(value.started is False for value in values)):
        status = 'not_started'
    elif full:
        status = 'complete'
    elif known:
        status = 'partial'
    else:
        status = 'unavailable'
    return CostSummary(
        status=status,
        currency='USD',
        amount=format(subtotal, 'f') if full and requested else None,
        known_subtotal=format(subtotal, 'f'),
        coverage=CostCoverage(
            total_attempts=len(values),
            complete_attempts=complete,
            known_attempts=known,
            known_requests=sum(value.known_requests for value in values),
            total_requests=(
                None if any(total is None for total in totals) else sum(total for total in totals if total is not None)
            ),
            missing_tasks=missing,
        ),
    )


def build_cost_summary(store: CostStore) -> ReportCosts:
    """Project native and API-equivalent cost from exact current/history selections.

    Args:
        store: Validated authoritative registry, state and immutable artifact references.

    Returns:
        Final-attempt and all-attempt summaries with lossless monetary strings,
        nullable complete amounts, coverage and explicit not-requested pricing.

    Raises:
        CostEvidenceError: If selected billing inputs have inconsistent evidence ownership.

    """
    final_native: list[CostObservation] = []
    final_priced: list[CostObservation] = []
    all_native: list[CostObservation] = []
    all_priced: list[CostObservation] = []
    requested = store.last_operation('reprice') is not None
    for task in store.state.tasks:
        owners = [*task.history, task] if task.attempt is not None else task.history
        for selected in owners:
            reported, ledger, receipt = _native(store, selected, task.id)
            priced = _priced(store, selected, task.id, ledger, receipt)
            requested |= selected.pricing is not None
            all_native.append(reported)
            all_priced.append(priced)
            if selected is task:
                final_native.append(reported)
                final_priced.append(priced)
    missing = sum(task.attempt is None for task in store.state.tasks)
    return ReportCosts(
        final_attempts=CostScope(
            native_reported=_summary(final_native, missing, native=True),
            api_equivalent=_summary(final_priced, missing, requested=requested),
        ),
        all_attempts=CostScope(
            native_reported=_summary(all_native, 0, native=True),
            api_equivalent=_summary(all_priced, 0, requested=requested),
        ),
    )
