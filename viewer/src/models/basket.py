"""Versioned frozen basket object contracts owned by the viewer."""

from .basket_v1 import BasketV1
from .basket_v2 import BasketV2
from .basket_v3 import BasketV3

Basket = BasketV1 | BasketV2 | BasketV3

__all__ = ['Basket', 'BasketV1', 'BasketV2', 'BasketV3']
