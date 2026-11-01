"""Display coverage keeps unknown totals separate from measured subtotals."""

from decimal import Decimal, localcontext

from benchmark.models import Usage
from benchmark.presentation.measurements import (
    Measurement,
    combine_measurements,
    token_measurements,
)


def test_partial_request_coverage_keeps_full_total_unknown() -> None:
    """Keep the 99 measured requests without claiming a complete total."""
    measured = Measurement(subtotal=990, known=True, complete=False, known_requests=99, total_requests=100)
    result = combine_measurements([measured])
    assert result.amount is None
    assert result.known_subtotal == 990
    assert result.status == 'partial'
    assert result.known_tasks == 0
    assert result.partial_tasks == 1
    assert result.unavailable_tasks == 0
    assert result.known_requests == 99
    assert result.total_requests == 100
    assert result.request_coverage_available


def test_measured_zero_is_not_unavailable() -> None:
    """Distinguish a measured zero from an unmeasured attempt."""
    zero = Measurement(subtotal=0, known=True, known_requests=1, total_requests=2)
    missing = Measurement()
    result = combine_measurements([zero, missing])
    assert result.status == 'partial'
    assert result.known_subtotal == 0
    assert result.partial_tasks == 1
    assert result.unavailable_tasks == 1
    assert result.total_requests is None
    assert result.known_requests == 1
    assert not result.request_coverage_available
    assert combine_measurements([missing]).status == 'unavailable'


def test_empty_selection_has_no_measured_zero() -> None:
    """An empty selection has neither a full amount nor coverage."""
    result = combine_measurements([])
    assert result.amount is None
    assert result.status == 'unavailable'
    assert result.total_tasks == 0
    assert not result.request_coverage_available


def test_legacy_complete_total_does_not_invent_request_coverage() -> None:
    """Retain legacy totals without inventing request counts."""
    result = combine_measurements([Measurement(subtotal=42, known=True, complete=True)])
    assert result.amount == 42
    assert result.status == 'complete'
    assert result.known_tasks == 1
    assert result.total_requests is None
    assert not result.request_coverage_available


def test_all_found_requests_does_not_prove_ledger_complete() -> None:
    """Request counts alone cannot prove capture completeness."""
    result = combine_measurements(
        [
            Measurement(
                subtotal=42,
                known=True,
                complete=False,
                known_requests=1,
                total_requests=1,
            ),
        ]
    )
    assert result.status == 'partial'
    assert result.amount is None


def test_money_subtotal_survives_small_ambient_decimal_context() -> None:
    """Sum monetary parts exactly despite the ambient decimal precision."""
    with localcontext() as context:
        context.prec = 3
        result = combine_measurements(
            [
                Measurement(subtotal=Decimal('123456789.123456789'), known=True, complete=True),
                Measurement(subtotal=Decimal('0.000000001'), known=True),
            ]
        )
    assert result.known_subtotal == Decimal('123456789.123456790')
    assert result.amount is None
    assert result.known_tasks == 1
    assert result.partial_tasks == 1


def test_complete_empty_ledger_proves_zero() -> None:
    """A complete ledger with no requests measures an exact zero."""
    result = combine_measurements(
        [
            Measurement(
                subtotal=0,
                known=True,
                complete=True,
                known_requests=0,
                total_requests=0,
            ),
        ]
    )
    assert result.amount == 0
    assert result.status == 'complete'
    assert result.request_coverage_available


def test_request_projection_ignores_unfinished_counters_and_preserves_categories() -> None:
    """Ignore unfinished counters and preserve independent category coverage."""

    def request(identifier: str, complete: bool) -> dict:
        """Build one synthetic billing unit with disjoint counters."""
        return {
            'id': identifier,
            'session_id': 'root',
            'provider': 'provider',
            'model': 'model',
            'complete': complete,
            'usage': dict(input=10, output=2, reasoning=1, cache_read=3, cache_write=0, total=None),
            'billing_tokens': dict(input=10, output=3, cache_read=3, cache_write=0),
            'input_context_tokens': 13,
            'reported_cost_usd': None,
        }

    complete_request = request('finished', True)
    unfinished_request = request('unfinished', False)
    ledger = Usage.model_validate(
        {
            'schema_family': 'pr_similarity_os.usage',
            'schema_version': 1,
            'run_id': '11111111-1111-4111-8111-111111111111',
            'task_id': 'owner/repo#1',
            'attempt_id': '22222222-2222-4222-8222-222222222222',
            'runtime': 'opencode:v1.18.33',
            'adapter_version': '1',
            'source_db': {'path': 'session.db', 'sha256': '0' * 64},
            'complete': False,
            'sessions': [{'id': 'root', 'parent_id': None, 'agent_name': None, 'kind': 'root'}],
            'requests': [complete_request, unfinished_request],
        }
    )
    measurements = token_measurements(ledger)
    total = combine_measurements([measurements['total']])
    assert total.amount is None
    assert total.known_subtotal == 16
    assert total.known_requests == 1
    assert total.total_requests == 2
    assert measurements['cache_write'].known
    assert measurements['cache_write'].subtotal == 0
    ledger.requests[0].usage.reasoning = None
    measurements = token_measurements(ledger)
    assert measurements['input'].known
    assert not measurements['reasoning'].known
    assert not measurements['total'].known
    ledger.requests[0].usage.total = 20
    assert token_measurements(ledger)['total'].subtotal == 20
