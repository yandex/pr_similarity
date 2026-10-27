"""Published example baskets remain readable with their exact frozen identity."""

from pathlib import Path

import pytest

from benchmark.persistence.storage import bytes_sha256, canonical_bytes, parse_json
from benchmark.validation.basket import load_basket

_EXAMPLES = tuple(
    (f'{project}/{project}-2026H1-{count}.json', 3, count)
    for project in ('opensearch', 'pandas')
    for count in (1, 5, 10, 25, 100)
)


def test_published_example_catalog_is_complete() -> None:
    """Require every shipped basket to participate in frozen-input compatibility checks."""
    root = Path(__file__).parents[1] / 'examples/baskets'
    assert {path.relative_to(root).as_posix() for path in root.glob('*/*.json')} == {name for name, _, _ in _EXAMPLES}


@pytest.mark.parametrize(('name', 'version', 'count'), _EXAMPLES)
def test_shipped_published_basket_preserves_original_bytes(name: str, version: int, count: int) -> None:
    """Read all published v3 examples without converting their bytes or provenance."""
    path = Path(__file__).parents[1] / 'examples/baskets' / name
    original = path.read_bytes()
    document = parse_json(original)
    meta, tasks = document['meta'], document['tasks']
    assert isinstance(meta, dict) and meta['schema_version'] == version
    assert isinstance(tasks, list) and len(tasks) == count
    basket = load_basket(path)
    assert basket.schema_version == version and len(basket.rows) == count
    assert basket.content == original and basket.sha256 == bytes_sha256(original)
    assert canonical_bytes(basket.data) == canonical_bytes(document)
    assert path.read_bytes() == original
