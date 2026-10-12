"""Pure per-request Decimal API-equivalent estimation from a frozen policy."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path

import yaml
from benchmark.config import _json_values, _UniqueLoader
from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError
from benchmark.storage import bytes_sha256

CALCULATOR_VERSION = 'api-equivalent-v1'
_QUANTUM = Decimal('0.000000000001')


@dataclass(frozen=True)
class PricingPolicy:
    """Keep validated pricing data alongside its exact frozen bytes."""

    data: dict
    content: bytes
    sha256: str


def validate_policy(data: dict) -> None:
    """Reject ambiguous pairs; provider/model aliases have no implicit resolution."""
    validate_document('pricing', data)
    pairs = [(row['provider'], row['model']) for row in data['models']]
    if len(pairs) != len(set(pairs)):
        raise BenchmarkError('invalid_pricing', 'Pricing policy contains duplicate provider/model pairs.')


def parse_policy(content: bytes) -> PricingPolicy:
    """Validate strict pricing YAML without changing its original bytes."""
    try:
        data = yaml.load(content.decode('utf-8'), Loader=_UniqueLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise BenchmarkError('invalid_pricing', 'Pricing policy must be valid UTF-8 YAML.') from exc
    if not isinstance(data, dict):
        raise BenchmarkError('invalid_pricing', 'Pricing policy must contain a mapping.')
    _json_values(data)
    validate_policy(data)
    return PricingPolicy(data, content, bytes_sha256(content))


def load_policy(path: Path) -> PricingPolicy:
    """Read once, preserving exact policy bytes for archival and identity."""
    try:
        return parse_policy(path.read_bytes())
    except OSError as exc:
        raise BenchmarkError(
            'pricing_missing', 'Cannot read pricing policy; check PRICING path and permissions.'
        ) from exc


def price_request(request: dict, policy: dict) -> dict:
    """Return an unrounded price or a precise coverage reason."""
    result = {'id': request['id'], 'amount': None, 'reason': None}
    if not request['complete']:
        return {**result, 'reason': 'request_incomplete'}
    matches = [
        row for row in policy['models'] if (row['provider'], row['model']) == (request['provider'], request['model'])
    ]
    if len(matches) != 1:
        return {**result, 'reason': 'model_unpriced'}
    model = matches[0]
    rates = model['rates']
    long_context = model['long_context']
    if long_context is not None:
        if request['input_context_tokens'] is None:
            return {**result, 'reason': 'context_unknown'}
        if request['input_context_tokens'] > long_context['threshold']:
            rates = long_context['rates']
    total = Decimal(0)
    precision = max(64, sum(len(str(value)) for value in rates.values() if value is not None) + 32)
    with localcontext() as context:
        context.prec = precision
        for key in ('input', 'cache_read', 'cache_write', 'output'):
            count = request['billing_tokens'][key]
            if count is None:
                return {**result, 'reason': 'usage_unknown'}
            if type(count) is not int or count < 0:
                raise BenchmarkError('invalid_usage', 'Billing token counts must be nonnegative integers.', exit_code=1)
            if count == 0:
                continue
            if rates[key] is None:
                return {**result, 'reason': 'category_unpriced'}
            total += Decimal(count) * Decimal(rates[key]) / Decimal(1_000_000)
    return {**result, 'amount': total}


def price_usage(ledger: dict, policy: dict) -> dict:
    """Price request-level usage without changing original provider costs.

    Args:
        ledger: Validated attempt-owned usage ledger.
        policy: Exact provider/model tariffs with optional long-context thresholds.

    Returns:
        Decimal totals and per-request coverage. Unknown components prevent a
        complete amount; the known subtotal remains separate from that amount.

    """
    validate_policy(policy)
    results = [price_request(row, policy) for row in ledger['requests']]
    priced = [row['amount'] for row in results if row['amount'] is not None]
    with localcontext() as context:
        context.prec = max(
            64, max((len(value.as_tuple().digits) + abs(value.adjusted()) for value in priced), default=1) + 32
        )
        subtotal = sum(priced, Decimal(0)).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    complete = ledger['complete'] and len(priced) == len(results)
    return {
        'status': 'complete' if complete else ('partial' if priced else 'unavailable'),
        'amount': subtotal if complete else None,
        'known_subtotal': subtotal,
        'priced_requests': len(priced),
        'total_requests': len(results),
        'requests': results,
    }
