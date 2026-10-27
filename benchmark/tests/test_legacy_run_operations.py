"""Retired run formats are rejected before execution, pricing or artifact changes."""

from pathlib import Path
from typing import Never

import pytest
from test_runner import config_and_basket

from benchmark.errors import BenchmarkError
from benchmark.metrics import rescore as scoring_operations
from benchmark.persistence import run_store
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json, write_json
from benchmark.pricing import operations as pricing_operations
from benchmark.runtime import coordinator
from benchmark.validation.basket import load_basket


def _inventory(directory: Path) -> dict[str, bytes | None]:
    """Capture exact files and directory names in this bounded temporary fixture."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes() if path.is_file() else None
        for path in directory.rglob('*')
    }


@pytest.mark.parametrize('operation', ['resume', 'reprice', 'rescore'])
@pytest.mark.parametrize('document', ['run', 'state'])
def test_old_run_format_stops_operations_before_work_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, document: str
) -> None:
    """Exercise each public operation against an isolated legacy-version gate.

    All other fixture fields remain current and valid. This distinguishes rejection
    of a retired format from subsequent input-digest or native-runtime failures.
    """
    config, dataset = config_and_basket(tmp_path)
    basket = load_basket(dataset)
    directory = tmp_path / 'run'
    directory.mkdir()
    initial = coordinator.initial_report(
        config, basket, '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
    )
    store = RunStore.create(directory, initial, basket, config)
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    artifact = directory / ('run.json' if document == 'run' else '_meta/state.json')
    raw = read_json(artifact)
    raw['schema_version'] = 1
    write_json(artifact, raw)
    before = _inventory(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> Never:
        """Fail immediately if a retired run reaches work or artifact publication."""
        pytest.fail('A retired run reached execution, calculation or an artifact write.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    monkeypatch.setattr(coordinator.Coordinator, 'prepare_agents', forbidden)
    monkeypatch.setattr(scoring_operations, 'metric_receipt', forbidden)
    monkeypatch.setattr(pricing_operations, 'load_policy', forbidden)
    monkeypatch.setattr(pricing_operations, 'freeze_pricing', forbidden)
    monkeypatch.setattr(run_store, 'write_json', forbidden)
    monkeypatch.setattr(run_store, 'atomic_write', forbidden)
    with pytest.raises(BenchmarkError) as error:
        if operation == 'resume':
            coordinator.run(config, dataset, resume=directory, progress='off', attempt_function=forbidden)
        elif operation == 'reprice':
            pricing_operations.reprice(directory, tmp_path / 'must-not-read-policy.yaml')
        else:
            scoring_operations.rescore(directory)
    assert error.value.code == 'invalid_contract'
    assert f'Invalid {document} field at schema_version' in error.value.message
    assert _inventory(tmp_path) == before
