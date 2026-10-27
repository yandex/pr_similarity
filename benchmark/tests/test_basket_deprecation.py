"""Legacy input warnings remain visible without rewriting frozen baskets."""

from pathlib import Path

import pytest

from benchmark.runtime.commands import Command
from benchmark.validation.basket import load_basket
from benchmark.validation.contracts import document_validator


@pytest.mark.parametrize('version', [1, 2, 3])
@pytest.mark.parametrize('kind', ['run', 'rescore', 'reprice', 'publish', 'visualize'])
def test_version_warning_is_visible_once_per_invocation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int, kind: str
) -> None:
    """Warn for old formats at ERROR level, deduplicate reads, and retain exact bytes."""
    original = (Path(__file__).parent / f'fixtures/basket-v{version}/github.json').read_bytes()
    path = tmp_path / 'basket.json'
    path.write_bytes(original)
    basket = load_basket(path)
    assert capsys.readouterr().err == ''
    command = Command(tmp_path, kind, level='ERROR', progress_mode='off')
    try:
        command.watch_interrupts(lambda: 0)
        command.warn_basket(basket)
        command.warn_basket(load_basket(path))
        command.finish('completed')
    finally:
        command.close()
    expected = int(version < 3)
    assert capsys.readouterr().err.count('is deprecated;') == expected
    assert (command.logs_dir / 'benchmark.log').read_text().count('is deprecated;') == expected
    assert basket.content == path.read_bytes() == original


def test_default_basket_contract_is_v3() -> None:
    """Keep old versions opt-in to schema lookup while the default stays current."""
    assert document_validator('basket').schema == document_validator('basket', 3).schema
