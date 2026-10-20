"""The shipped real v1 baskets remain readable without conversion or frozen-input edits."""

from pathlib import Path

import pytest
from benchmark.basket import load_basket
from benchmark.commands import Command
from benchmark.storage import bytes_sha256


@pytest.mark.parametrize(('name', 'count'), [('opensearch-10.json', 10), ('pandas-25.json', 25)])
def test_shipped_v1_basket_preserves_all_tasks_and_warns_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, count: int
) -> None:
    """Use the actual examples, preserving every row and the exact original digest."""
    path = Path(__file__).parents[1] / 'examples/baskets/legacy' / name
    original = path.read_bytes()
    basket = load_basket(path)
    assert basket.schema_version == 1
    assert len(basket.rows) == count
    assert basket.content == original and basket.sha256 == bytes_sha256(original)
    (tmp_path / 'basket.json').write_bytes(original)
    command = Command(tmp_path, 'visualize', level='ERROR')
    command.warn_basket(basket)
    command.warn_basket(load_basket(path))
    command.finish('completed')
    assert capsys.readouterr().err.count('Basket schema v1 is deprecated') == 1
    assert path.read_bytes() == original
