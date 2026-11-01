"""Project selected immutable evidence into compact task display measurements."""

from dataclasses import dataclass
from decimal import Decimal

from viewer.models.evidence import Seal
from viewer.models.report import Report
from viewer.presentation.cost_summary import (
    CostEvidenceError,
    CostStore,
    _native,
    _priced,
)
from viewer.presentation.measurements import (
    TOKEN_CATEGORIES,
    Measurement,
    token_measurements,
)


@dataclass(frozen=True)
class TaskMeasurements:
    """Keep only measured scalars, never the ledger or private runtime database."""

    tokens: dict[str, Measurement]
    cost: Measurement
    cost_source: str


def build_task_measurements(store: CostStore, report: Report) -> dict[str, TaskMeasurements]:
    """Read final selections only, preserving selected pricing over native costs."""
    if [task.id for task in store.state.tasks] != [task.id for task in report.tasks]:
        raise CostEvidenceError('report_owner_mismatch', 'Display tasks differ from authoritative state.')
    result = {}
    for owner, row in zip(store.state.tasks, report.tasks, strict=True):
        if owner.seal is not None:
            seal = store.read_artifact(owner.seal, Seal)
            if seal.run_id != store.registry.id or seal.task.id != row.id or seal.task.final_attempt != owner.attempt:
                raise CostEvidenceError(
                    'usage_owner_mismatch',
                    'Display evidence belongs to another attempt.',
                )
        native, ledger, receipt = _native(store, owner, row.id)
        if ledger is None and row.telemetry.usage_ledger is not None:
            raise CostEvidenceError(
                'usage_receipt_missing',
                'Declared display usage requires a sealed receipt.',
            )
        if ledger is not None:
            tokens = token_measurements(ledger)
        else:
            tokens = {}
            for category in TOKEN_CATEGORIES:
                value = getattr(row.telemetry.tokens, category)
                tokens[category] = Measurement(
                    subtotal=0 if value is None else int(value),
                    known=value is not None,
                    complete=value is not None,
                )
        observation = _priced(store, owner, row.id, ledger, receipt) if owner.pricing is not None else native
        cost = Measurement(
            subtotal=observation.subtotal,
            known=observation.known,
            complete=observation.complete,
            known_requests=int(observation.known_requests),
            total_requests=(None if observation.total_requests is None else int(observation.total_requests)),
        )
        if ledger is None and owner.pricing is None:
            legacy = row.telemetry.api_equivalent
            amount = (legacy.amount if legacy.status == 'complete' else None) if legacy else row.telemetry.cost.amount
            cost = Measurement(
                subtotal=Decimal(0) if amount is None else Decimal(str(amount)),
                known=amount is not None,
                complete=amount is not None,
            )
        result[row.id] = TaskMeasurements(
            tokens=tokens,
            cost=cost,
            cost_source=(
                'reprice' if owner.pricing is not None or row.telemetry.api_equivalent is not None else 'opencode'
            ),
        )
    return result
