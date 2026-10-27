"""Validated billing units and nullable token aggregation independent of SQLite."""

import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonValue
from benchmark.models import Usage
from benchmark.models.report import (
    ReportTelemetry,
    ReportTelemetryCost,
    ReportTelemetrySessionCounts,
    ReportTelemetryTokens,
)
from benchmark.models.usage import UsageRequestItemBillingTokens
from benchmark.validation.contracts import validate_document

TOKEN_FIELDS = ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total')


def validate_usage(ledger: object) -> Usage:
    """Prove request uniqueness, session ownership and acyclic parent links."""
    validate_document('usage', ledger)
    ledger = Usage.model_validate(ledger.to_wire() if isinstance(ledger, Usage) else ledger)
    sessions = {row.id: row for row in ledger.sessions}
    if len(sessions) != len(ledger.sessions) or sum(row.kind == 'root' for row in sessions.values()) > 1:
        raise BenchmarkError('invalid_usage', 'Usage has duplicate or ambiguous session identities.', exit_code=1)
    for row in sessions.values():
        current = row.parent_id
        visited = {row.id}
        if row.kind in ('root', 'detached') and current is not None:
            raise BenchmarkError('invalid_usage', 'Root/detached session has an unexpected parent.', exit_code=1)
        if row.kind == 'subagent' and current is None:
            raise BenchmarkError('invalid_usage', 'Subagent session is missing its parent.', exit_code=1)
        while current is not None:
            if current not in sessions or current in visited:
                raise BenchmarkError('invalid_usage', 'Usage session parents are missing or cyclic.', exit_code=1)
            visited.add(current)
            current = sessions[current].parent_id
    ids = [row.id for row in ledger.requests]
    if len(ids) != len(set(ids)) or any(row.session_id not in sessions for row in ledger.requests):
        raise BenchmarkError('invalid_usage', 'Usage contains duplicate or foreign billing units.', exit_code=1)
    if ledger.complete and not all(row.complete for row in ledger.requests):
        raise BenchmarkError('invalid_usage', 'Complete usage contains unfinished billing units.', exit_code=1)
    return ledger


def nullable_sum[Number: (int, float)](values: Sequence[Number | None]) -> Number | Literal[0] | None:
    """Keep a sum unknown if any component is unknown, including known zero peers."""
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _counter(value: JsonValue) -> int | None:
    """Require a native counter to be an exact nonnegative integer or unknown."""
    if value is not None and (type(value) is not int or value < 0):
        raise BenchmarkError('invalid_usage', 'OpenCode reported invalid token counters.', exit_code=1)
    return value


def native_tokens(value: JsonValue) -> ReportTelemetryTokens:
    """Keep exactly the pinned runtime's disjoint raw categories, without guesses."""
    value = value or {}
    if not isinstance(value, dict):
        raise BenchmarkError('invalid_usage', 'OpenCode reported invalid token counters.', exit_code=1)
    cache = value.get('cache') or {}
    if not isinstance(cache, dict):
        raise BenchmarkError('invalid_usage', 'OpenCode reported invalid token counters.', exit_code=1)
    return ReportTelemetryTokens(
        input=_counter(value.get('input')),
        output=_counter(value.get('output')),
        reasoning=_counter(value.get('reasoning')),
        total=_counter(value.get('total')),
        cache_read=_counter(cache.get('read')),
        cache_write=_counter(cache.get('write')),
    )


def billing_tokens(raw: ReportTelemetryTokens) -> tuple[UsageRequestItemBillingTokens, int | None]:
    """OpenCode 1.18.33 input excludes cache and output excludes reasoning."""
    billing = UsageRequestItemBillingTokens(
        input=raw.input,
        cache_read=raw.cache_read,
        cache_write=raw.cache_write,
        output=nullable_sum([raw.output, raw.reasoning]),
    )
    return billing, nullable_sum([raw.input, raw.cache_read, raw.cache_write])


def telemetry_from_usage(ledger: Usage) -> ReportTelemetry:
    """One attempt's totals; message/session aggregates never enter this sum."""
    ledger = validate_usage(ledger)
    requests = ledger.requests
    tokens = ReportTelemetryTokens(
        input=nullable_sum([row.usage.input for row in requests]),
        output=nullable_sum([row.usage.output for row in requests]),
        reasoning=nullable_sum([row.usage.reasoning for row in requests]),
        cache_read=nullable_sum([row.usage.cache_read for row in requests]),
        cache_write=nullable_sum([row.usage.cache_write for row in requests]),
        total=nullable_sum([row.usage.total for row in requests]),
    )
    source = 'reported'
    if tokens.total is None:
        tokens.total = nullable_sum(
            [tokens.input, tokens.output, tokens.reasoning, tokens.cache_read, tokens.cache_write]
        )
        source = 'derived_disjoint' if tokens.total is not None else 'unavailable'
    elif not requests:
        source = 'derived_disjoint'
    costs = [row.reported_cost_usd for row in requests]
    amount = (
        None
        if any(value is None for value in costs)
        else float(sum((Decimal(str(value)) for value in costs), Decimal(0)))
    )
    if amount is not None and not math.isfinite(amount):
        raise BenchmarkError('invalid_usage', 'OpenCode reported an invalid total cost.', exit_code=1)
    return ReportTelemetry(
        source='opencode',
        status='complete' if ledger.complete else 'partial',
        tokens=tokens,
        total_source=source,
        cost=ReportTelemetryCost(amount=amount, currency='USD', source='opencode'),
        transport='db',
        session_db=ledger.source_db,
        usage_ledger=None,
        session_counts=ReportTelemetrySessionCounts(
            root=sum(row.kind == 'root' for row in ledger.sessions),
            subagent=sum(row.kind == 'subagent' for row in ledger.sessions),
            detached=sum(row.kind == 'detached' for row in ledger.sessions),
        ),
        diagnostics=[],
        api_equivalent=None,
    )
