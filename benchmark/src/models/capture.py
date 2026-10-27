"""Closed source-capture receipts independent of host scoring."""

from .base import WireModel


class CaptureReceipt(WireModel):
    """Record native diff exclusions and the captured source paths."""

    excluded: dict[str, list[str]]
    paths: list[str]


class CaptureError(WireModel):
    """Record a bounded capture failure without exposing source contents."""

    code: str
    paths: list[str]
