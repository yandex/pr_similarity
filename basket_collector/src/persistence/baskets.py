"""Read published baskets independently of mutable collector stage storage."""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from basket_collector.models.frozen_baskets import FrozenBasketDocument, validate_frozen_basket
from basket_collector.persistence.artifact_io import StorageError, _unique_keys


@dataclass(frozen=True)
class FrozenBasket:
    """Pair a typed published document with its exact unchanged bytes and content digest."""

    document: FrozenBasketDocument
    content: bytes
    sha256: str


def _nonfinite(value: str) -> Never:
    """Reject non-finite JSON tokens at the same boundary as other saved artifacts."""
    raise StorageError(f'non-finite JSON number: {value}')


def read_basket(path: Path) -> FrozenBasket:
    """Validate basket v1/v2/v3 without resuming stages or rewriting the published input.

    Args:
        path: Published JSON basket, including an export owned by an older collector.

    Returns:
        A typed document with the original file bytes and SHA-256.

    Raises:
        StorageError: The file is unreadable, malformed, or not a valid completed basket.

    """
    try:
        content = path.read_bytes()
        value = json.loads(content, object_pairs_hook=_unique_keys, parse_constant=_nonfinite)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StorageError(f'cannot read {path}: {error}') from error
    try:
        document = validate_frozen_basket(value)
    except ValueError as error:
        raise StorageError(f'{path}: invalid published basket: {error}') from error
    if document.meta.schema_version in (1, 2):
        logging.getLogger('basket_collector.basket').warning(
            'Basket schema v%s is deprecated; use v3 for new exports. Historical input is read-only.',
            document.meta.schema_version,
        )
    return FrozenBasket(document=document, content=content, sha256=hashlib.sha256(content).hexdigest())
