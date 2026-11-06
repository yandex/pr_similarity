"""Typed display-only coverage; never serialize these fields into run evidence."""

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from typing import Literal

from viewer.models import Usage

type Number = int | Decimal


@dataclass(frozen=True)
class Measurement:
    """Retain the known part of one metric in one selected attempt."""

    subtotal: Number = 0
    known: bool = False
    complete: bool = False
    known_requests: int = 0
    total_requests: int | None = None


@dataclass(frozen=True)
class DisplayMeasurement:
    """Describe task and request coverage independently of execution status."""

    amount: Number | None
    known_subtotal: Number
    status: Literal['complete', 'partial', 'unavailable']
    known_tasks: int
    partial_tasks: int
    unavailable_tasks: int
    total_tasks: int
    known_requests: int
    total_requests: int | None
    request_coverage_available: bool


def combine_measurements(values: list[Measurement]) -> DisplayMeasurement:
    """Sum measured parts without making a partial or empty selection complete."""
    parts = [value.subtotal for value in values if value.known]
    subtotal: Number
    if any(isinstance(part, Decimal) for part in parts):
        decimals = [Decimal(part) for part in parts]
        # Cover the entire coefficient from the largest integral digit to the
        # smallest fractional digit, plus carry digits for the number of terms.
        high = max((part.adjusted() for part in decimals), default=0)
        low = min((int(part.as_tuple().exponent) for part in decimals), default=0)
        with localcontext() as context:
            context.prec = max(64, high - low + len(str(len(parts))) + 2)
            subtotal = sum(decimals, Decimal(0))
    else:
        subtotal = sum(parts)
    complete = sum(value.complete for value in values)
    partial = sum(value.known and not value.complete for value in values)
    full = bool(values) and complete == len(values)
    coverage = bool(values) and all(value.total_requests is not None for value in values)
    return DisplayMeasurement(
        amount=subtotal if full else None,
        known_subtotal=subtotal,
        status=('complete' if full else 'partial' if complete or partial else 'unavailable'),
        known_tasks=complete,
        partial_tasks=partial,
        unavailable_tasks=len(values) - complete - partial,
        total_tasks=len(values),
        known_requests=sum(value.known_requests for value in values),
        total_requests=(sum(value.total_requests or 0 for value in values) if coverage else None),
        request_coverage_available=coverage,
    )


TOKEN_CATEGORIES = (
    'input',
    'output',
    'reasoning',
    'cache_read',
    'cache_write',
    'total',
)


def token_measurements(ledger: Usage) -> dict[str, Measurement]:
    """Project validated billing units, excluding unfinished request counters."""
    result = {}
    for category in TOKEN_CATEGORIES:
        known = []
        for request in ledger.requests:
            if not request.complete:
                continue
            value = getattr(request.usage, category)
            if category == 'total' and value is None:
                parts = [getattr(request.usage, field) for field in TOKEN_CATEGORIES[:-1]]
                if all(part is not None for part in parts):
                    value = sum(parts)
            if value is not None:
                known.append(int(value))
        complete = ledger.complete and len(known) == len(ledger.requests)
        result[category] = Measurement(
            subtotal=sum(known),
            known=bool(known) or complete,
            complete=complete,
            known_requests=len(known),
            total_requests=len(ledger.requests),
        )
    return result


def measurement_dict(value: DisplayMeasurement) -> dict[str, int | float | str | None]:
    """Convert only final display decimals to JSON numbers, after exact summation."""
    return {key: float(item) if isinstance(item, Decimal) else item for key, item in asdict(value).items()}
