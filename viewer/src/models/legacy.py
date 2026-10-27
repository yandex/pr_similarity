"""Immutable historical fields retained when reading version-one publications."""

from typing import Literal

from .base import WireModel


class LegacyMetric(WireModel):
    """Retain the historical metric receipt without recomputing source fingerprints."""

    name: Literal['mlp']
    version: str
    weights_sha256: str
    implementation_sha256: str
    dependencies_sha256: str
    pythonhashseed: int | float


class LegacyApiEquivalent(WireModel):
    """Retain the original calculator digest and exact archived pricing result."""

    status: Literal['complete', 'partial', 'unavailable']
    amount: int | float | None
    currency: Literal['USD']
    basis: Literal['api_equivalent']
    calculation_id: str
    pricing_sha256: str
    calculator_sha256: str
    usage_sha256: str | None
    priced_requests: int | float
    total_requests: int | float | None
