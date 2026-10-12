"""Per-request Decimal tariffs, exact model identity and unknown coverage."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from benchmark.errors import BenchmarkError
from benchmark.pricing import load_policy, price_request, price_usage


def policy() -> dict:
    """Build a synthetic policy with two context tiers and explicit cache rates."""
    rates = {'input': '2', 'cache_read': '0.2', 'cache_write': None, 'output': '10'}
    return {
        'schema_version': 1,
        'id': 'fixture',
        'source': 'synthetic',
        'currency': 'USD',
        'service_tier': 'standard',
        'models': [
            {
                'provider': 'p',
                'model': 'm',
                'rates': rates,
                'long_context': {
                    'threshold': 100,
                    'rates': {'input': '4', 'cache_read': '0.4', 'cache_write': None, 'output': '15'},
                },
            }
        ],
    }


def request(*, context: int | None = 100, input_tokens: int | None = 100) -> dict:
    """Build an owned billing unit with configurable context and token counters."""
    return {
        'id': 'req',
        'session_id': 'root',
        'provider': 'p',
        'model': 'm',
        'complete': True,
        'billing_tokens': {'input': input_tokens, 'cache_read': 0, 'cache_write': 0, 'output': 20},
        'input_context_tokens': context,
    }


def test_threshold_is_strict_and_applies_to_whole_request() -> None:
    """Verify threshold is strict and applies to whole request."""
    assert price_request(request(), policy())['amount'] == Decimal('0.0004')
    assert price_request(request(context=101, input_tokens=101), policy())['amount'] == Decimal('0.000704')


@pytest.mark.parametrize(
    'modify',
    [
        lambda r: r.update(model='alias'),
        lambda r: r.update(complete=False),
        lambda r: r['billing_tokens'].update(cache_read=None),
        lambda r: r.update(input_context_tokens=None),
    ],
)
def test_unknown_is_not_free(modify: Callable[[dict], object]) -> None:
    """Verify unknown is not free."""
    row = request()
    modify(row)
    assert price_request(row, policy())['amount'] is None


def test_multiple_models_are_priced_per_request() -> None:
    """Verify multiple models are priced per request."""
    data = policy()
    data['models'].append(
        {
            'provider': 'p',
            'model': 'small',
            'rates': {'input': '1', 'cache_read': '0.1', 'cache_write': None, 'output': '4'},
            'long_context': None,
        }
    )
    first, second = request(), request()
    second.update(id='child', model='small', session_id='child')
    result = price_usage({'complete': True, 'requests': [first, second]}, data)
    assert result['status'] == 'complete'
    assert result['amount'] == Decimal('0.000580000000')
    assert result['priced_requests'] == result['total_requests'] == 2


def test_partial_keeps_known_subtotal_but_no_full_amount() -> None:
    """Verify partial keeps known subtotal but no full amount."""
    first, second = request(), request()
    second.update(id='unknown', model='unknown')
    result = price_usage({'complete': True, 'requests': [first, second]}, policy())
    assert result['status'] == 'partial' and result['amount'] is None
    assert result['known_subtotal'] == Decimal('0.0004')


def test_policy_duplicate_keys_models_and_numeric_rates_rejected(tmp_path: Path) -> None:
    """Verify policy duplicate keys models and numeric rates rejected."""
    path = tmp_path / 'pricing.yaml'
    for mutate in [lambda p: p['models'].append(p['models'][0]), lambda p: p['models'][0]['rates'].update(input=2)]:
        data = policy()
        mutate(data)
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(BenchmarkError):
            load_policy(path)
    path.write_text('schema_version: 1\nschema_version: 1\n')
    with pytest.raises(BenchmarkError):
        load_policy(path)


def test_frozen_policy_hashes_exact_bytes(tmp_path: Path) -> None:
    """Verify frozen policy hashes exact bytes."""
    path = tmp_path / 'pricing.yaml'
    path.write_text(yaml.safe_dump(policy()))
    one = load_policy(path)
    path.write_text(path.read_text() + '\n')
    two = load_policy(path)
    assert one.data == two.data and one.sha256 != two.sha256
