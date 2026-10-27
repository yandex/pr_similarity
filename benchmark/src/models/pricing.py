"""Typed pricing-v1 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import WireModel


class PricingModelItemRates(WireModel):
    """Represent the PricingModelItemRates object in the pricing-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/models/items/properties/rates'
    input: str | None
    cache_read: str | None
    cache_write: str | None
    output: str | None


PricingModelItemLongContextRates = PricingModelItemRates


class PricingModelItemLongContext(WireModel):
    """Represent the PricingModelItemLongContext object in the pricing-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/models/items/properties/long_context/anyOf/0'
    threshold: int
    rates: PricingModelItemLongContextRates


class PricingModelItem(WireModel):
    """Represent the PricingModelItem object in the pricing-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/models/items'
    provider: str
    model: str
    rates: PricingModelItemRates
    long_context: PricingModelItemLongContext | None


class Pricing(WireModel):
    """Represent the Pricing object in the pricing-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_version: int
    id: str
    source: str
    currency: Literal['USD']
    service_tier: str
    models: list[PricingModelItem]


Pricing.model_rebuild()
