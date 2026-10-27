"""Exercise the viewer consumer against every shipped current-format example basket."""

from pathlib import Path

import pytest

from viewer.util import json_bytes, parse_json
from viewer.validation.basket import validate_basket

_EXAMPLES = tuple(
    (f'{project}/{project}-2026H1-{count}.json', 3, count)
    for project in ('opensearch', 'pandas')
    for count in (1, 5, 10, 25, 100)
)


@pytest.mark.parametrize(('name', 'version', 'count'), _EXAMPLES)
def test_published_basket_preserves_frozen_content(name: str, version: int, count: int) -> None:
    """Validate complete published populations without normalizing their saved bytes."""
    root = Path(__file__).parents[2] / 'benchmark/examples/baskets'
    if not root.is_dir():
        pytest.skip('requires the sibling benchmark example basket catalogue')
    path = root / name
    original = path.read_bytes()
    document = parse_json(original)
    basket = validate_basket(document)
    assert basket.meta.schema_version == version and len(basket.tasks) == count
    assert json_bytes(basket.to_wire()) == json_bytes(document)
    assert path.read_bytes() == original
