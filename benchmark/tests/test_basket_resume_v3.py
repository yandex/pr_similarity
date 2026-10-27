"""Resume binds complete v3 input bytes, not merely matching exported task IDs."""

import json
from pathlib import Path

import pytest
from test_basket_v3 import add_omission, payload
from test_runner import config_and_basket

from benchmark.errors import BenchmarkError
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import canonical_bytes
from benchmark.runtime.coordinator import _validate_inputs, initial_report
from benchmark.validation.basket import load_basket


@pytest.mark.parametrize('mutation', ['format', 'selection', 'omission'])
def test_resume_rejects_changed_v3_input_without_rewriting_saved_basket(tmp_path: Path, mutation: str) -> None:
    """Even valid metadata-only changes require a new run; original bytes stay intact."""
    config, path = config_and_basket(tmp_path)
    data = payload()
    path.write_bytes(canonical_bytes(data))
    basket = load_basket(path)
    directory = tmp_path / 'run'
    directory.mkdir()
    report = initial_report(
        config, basket, '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
    )
    store = RunStore.create(directory, report, basket, config)
    original = (directory / 'basket.json').read_bytes()
    _validate_inputs(store, config, basket)
    if mutation == 'selection':
        data['meta']['selection']['input_sha256'] = 'f' * 64
    elif mutation == 'omission':
        add_omission(data, 'collect')
    path.write_bytes(json.dumps(data, indent=2).encode())
    changed = load_basket(path)
    assert changed.rows == basket.rows
    with pytest.raises(BenchmarkError, match='Basket|input'):
        _validate_inputs(store, config, changed)
    assert (directory / 'basket.json').read_bytes() == original
    RunStore.open(directory)
