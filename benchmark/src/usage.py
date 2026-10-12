"""Validated billing units and nullable token aggregation independent of SQLite."""

import math
from decimal import Decimal
from typing import cast

from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError

TOKEN_FIELDS = ('input', 'output', 'reasoning', 'cache_read', 'cache_write', 'total')


def validate_usage(ledger: dict) -> None:
    """Prove request uniqueness, session ownership and acyclic parent links."""
    validate_document('usage', ledger)
    sessions = {row['id']: row for row in ledger['sessions']}
    if len(sessions) != len(ledger['sessions']) or sum(row['kind'] == 'root' for row in sessions.values()) > 1:
        raise BenchmarkError('invalid_usage', 'Usage has duplicate or ambiguous session identities.', exit_code=1)
    for row in sessions.values():
        current = row['parent_id']
        visited = {row['id']}
        if row['kind'] in ('root', 'detached') and current is not None:
            raise BenchmarkError('invalid_usage', 'Root/detached session has an unexpected parent.', exit_code=1)
        if row['kind'] == 'subagent' and current is None:
            raise BenchmarkError('invalid_usage', 'Subagent session is missing its parent.', exit_code=1)
        while current is not None:
            if current not in sessions or current in visited:
                raise BenchmarkError('invalid_usage', 'Usage session parents are missing or cyclic.', exit_code=1)
            visited.add(current)
            current = sessions[current]['parent_id']
    ids = [row['id'] for row in ledger['requests']]
    if len(ids) != len(set(ids)) or any(row['session_id'] not in sessions for row in ledger['requests']):
        raise BenchmarkError('invalid_usage', 'Usage contains duplicate or foreign billing units.', exit_code=1)
    if ledger['complete'] and not all(row['complete'] for row in ledger['requests']):
        raise BenchmarkError('invalid_usage', 'Complete usage contains unfinished billing units.', exit_code=1)


def nullable_sum(values: list[int | float | None]) -> int | float | None:
    """Keep a sum unknown if any component is unknown, including known zero peers."""
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def native_tokens(value: dict | None) -> dict:
    """Keep exactly the pinned runtime's disjoint raw categories, without guesses."""
    value = value or {}
    cache = value.get('cache') or {}
    result = {key: value.get(key) for key in ('input', 'output', 'reasoning', 'total')}
    result.update(cache_read=cache.get('read'), cache_write=cache.get('write'))
    if any(item is not None and (type(item) is not int or item < 0) for item in result.values()):
        raise BenchmarkError('invalid_usage', 'OpenCode reported invalid token counters.', exit_code=1)
    return result


def billing_tokens(raw: dict) -> tuple[dict, int | None]:
    """OpenCode 1.18.33 input excludes cache and output excludes reasoning."""
    billing = {key: raw[key] for key in ('input', 'cache_read', 'cache_write')}
    billing['output'] = nullable_sum([raw['output'], raw['reasoning']])
    context = nullable_sum([raw['input'], raw['cache_read'], raw['cache_write']])
    return billing, cast(int | None, context)


def telemetry_from_usage(ledger: dict) -> dict:
    """One attempt's totals; message/session aggregates never enter this sum."""
    validate_usage(ledger)
    requests = ledger['requests']
    tokens = {field: nullable_sum([row['usage'][field] for row in requests]) for field in TOKEN_FIELDS}
    source = 'reported'
    if tokens['total'] is None:
        tokens['total'] = nullable_sum([tokens[key] for key in TOKEN_FIELDS if key != 'total'])
        source = 'derived_disjoint' if tokens['total'] is not None else 'unavailable'
    elif not requests:
        source = 'derived_disjoint'
    costs = [row['reported_cost_usd'] for row in requests]
    amount = (
        None
        if any(value is None for value in costs)
        else float(sum((Decimal(str(value)) for value in costs), Decimal(0)))
    )
    if amount is not None and not math.isfinite(amount):
        raise BenchmarkError('invalid_usage', 'OpenCode reported an invalid total cost.', exit_code=1)
    counts = {kind: sum(row['kind'] == kind for row in ledger['sessions']) for kind in ('root', 'subagent', 'detached')}
    return {
        'source': 'opencode',
        'status': 'complete' if ledger['complete'] else 'partial',
        'tokens': tokens,
        'total_source': source,
        'cost': {'amount': amount, 'currency': 'USD', 'source': 'opencode'},
        'transport': 'db',
        'session_db': ledger['source_db'],
        'usage_ledger': None,
        'session_counts': counts,
        'diagnostics': [],
        'api_equivalent': None,
    }
