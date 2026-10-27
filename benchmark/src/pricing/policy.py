"""Pure per-request Decimal API-equivalent estimation from a frozen policy."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Literal

import yaml

from benchmark.config import _json_values, _UniqueLoader
from benchmark.errors import BenchmarkError
from benchmark.models import Usage
from benchmark.models.pricing import Pricing
from benchmark.models.usage import UsageRequestItem
from benchmark.persistence.storage import bytes_sha256
from benchmark.validation.contracts import validate_document

CALCULATOR_VERSION = 'api-equivalent-v1'
_QUANTUM = Decimal('0.000000000001')


@dataclass(frozen=True)
class PricingPolicy:
    """Keep validated pricing data alongside its exact frozen bytes."""

    data: Pricing
    content: bytes
    sha256: str


@dataclass(frozen=True)
class RequestPrice:
    """Keep an unrounded request price or its explicit coverage reason."""

    id: str
    amount: Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PriceCalculation:
    """Retain Decimal arithmetic until an explicit artifact serialization boundary."""

    status: Literal['complete', 'partial', 'unavailable']
    amount: Decimal | None
    known_subtotal: Decimal
    priced_requests: int
    total_requests: int | None
    requests: list[RequestPrice]
    reason: str | None = None


def validate_policy(data: object) -> Pricing:
    """Reject ambiguous pairs; provider/model aliases have no implicit resolution."""
    validate_document('pricing', data)
    policy = data if isinstance(data, Pricing) else Pricing.model_validate(data)
    pairs = [(row.provider, row.model) for row in policy.models]
    if len(pairs) != len(set(pairs)):
        raise BenchmarkError('invalid_pricing', 'Pricing policy contains duplicate provider/model pairs.')
    return policy


def parse_policy(content: bytes) -> PricingPolicy:
    """Validate strict pricing YAML without changing its original bytes."""
    try:
        data = yaml.load(content.decode('utf-8'), Loader=_UniqueLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise BenchmarkError('invalid_pricing', 'Pricing policy must be valid UTF-8 YAML.') from exc
    if not isinstance(data, dict):
        raise BenchmarkError('invalid_pricing', 'Pricing policy must contain a mapping.')
    _json_values(data)
    policy = validate_policy(data)
    return PricingPolicy(policy, content, bytes_sha256(content))


def load_policy(path: Path) -> PricingPolicy:
    """Read once, preserving exact policy bytes for archival and identity."""
    try:
        return parse_policy(path.read_bytes())
    except OSError as exc:
        raise BenchmarkError(
            'pricing_missing', 'Cannot read pricing policy; check PRICING path and permissions.'
        ) from exc


def price_request(request: UsageRequestItem, policy: Pricing) -> RequestPrice:
    """Return an unrounded price or a precise coverage reason."""
    if not request.complete:
        return RequestPrice(request.id, reason='request_incomplete')
    matches = [row for row in policy.models if (row.provider, row.model) == (request.provider, request.model)]
    if len(matches) != 1:
        return RequestPrice(request.id, reason='model_unpriced')
    model = matches[0]
    rates = model.rates
    long_context = model.long_context
    if long_context is not None:
        if request.input_context_tokens is None:
            return RequestPrice(request.id, reason='context_unknown')
        if request.input_context_tokens > long_context.threshold:
            rates = long_context.rates
    total = Decimal(0)
    categories = (
        (request.billing_tokens.input, rates.input),
        (request.billing_tokens.cache_read, rates.cache_read),
        (request.billing_tokens.cache_write, rates.cache_write),
        (request.billing_tokens.output, rates.output),
    )
    precision = max(64, sum(len(str(rate)) for _, rate in categories if rate is not None) + 32)
    with localcontext() as context:
        context.prec = precision
        for count, rate in categories:
            if count is None:
                return RequestPrice(request.id, reason='usage_unknown')
            if type(count) is not int or count < 0:
                raise BenchmarkError('invalid_usage', 'Billing token counts must be nonnegative integers.', exit_code=1)
            if count == 0:
                continue
            if rate is None:
                return RequestPrice(request.id, reason='category_unpriced')
            total += Decimal(count) * Decimal(rate) / Decimal(1_000_000)
    return RequestPrice(request.id, amount=total)


def price_usage(ledger: Usage, policy: Pricing) -> PriceCalculation:
    """Price request usage, preserving unknown coverage and exact Decimal totals."""
    validate_policy(policy)
    results = [price_request(row, policy) for row in ledger.requests]
    priced = [row.amount for row in results if row.amount is not None]
    with localcontext() as context:
        context.prec = max(
            64, max((len(value.as_tuple().digits) + abs(value.adjusted()) for value in priced), default=1) + 32
        )
        subtotal = sum(priced, Decimal(0)).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    complete = ledger.complete and len(priced) == len(results)
    return PriceCalculation(
        status='complete' if complete else ('partial' if priced else 'unavailable'),
        amount=subtotal if complete else None,
        known_subtotal=subtotal,
        priced_requests=len(priced),
        total_requests=len(results),
        requests=results,
    )
