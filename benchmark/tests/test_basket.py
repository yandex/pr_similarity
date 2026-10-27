"""Consumer validation of frozen v3 basket bytes, without importing the collector."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import pytest

from benchmark.errors import BenchmarkError
from benchmark.persistence.storage import canonical_bytes
from benchmark.validation.basket import load_basket


class FixtureChanges(TypedDict):
    """Mutable file and counter sections in a synthetic basket."""

    files: list[dict[str, object]]
    stats: dict[str, int]


class FixtureTask(TypedDict):
    """Known object sections while tests mutate wire values before validation."""

    id: str
    workspace: str
    agent_task: str
    change_request: dict[str, object]
    source: dict[str, object]
    revisions: dict[str, str]
    provenance: dict[str, dict[str, object]]
    changes: FixtureChanges


class FixtureBasket(TypedDict):
    """Stable outer shape shared by valid and deliberately damaged fixtures."""

    meta: dict[str, object]
    tasks: list[FixtureTask]


def payload(provider: str = 'github', *, version: int = 3) -> FixtureBasket:
    """Read an explicit independently owned frozen basket conformance fixture."""
    path = Path(__file__).resolve().parent / f'fixtures/basket-v{version}' / (provider + '.json')
    return json.loads(path.read_text(encoding='utf-8'))


def test_frozen_basket_without_golden_diff(tmp_path: Path) -> None:
    """Verify frozen basket without golden diff."""
    path = tmp_path / 'basket.json'
    content = canonical_bytes(payload()) + b'\n'
    path.write_bytes(content)
    basket = load_basket(path)
    assert basket.content == content
    assert len(basket.rows) == 1
    assert basket.rows[0].workspace == 'new'
    assert 'golden_diff' not in basket.rows[0].model_fields_set


@pytest.mark.parametrize(
    'mutate',
    [
        lambda d: d['meta'].update(schema_version=True),
        lambda d: d['tasks'][0].pop('workspace'),
        lambda d: d['tasks'][0].update(workspace='../escape'),
        lambda d: d['tasks'][0].update(workspace='/absolute'),
        lambda d: d['tasks'][0].update(workspace='.git'),
        lambda d: d['meta']['counts'].update(succeeded=2),
        lambda d: d['meta'].update(partial=True),
        lambda d: d['meta'].update(settings_hash='f' * 64),
        lambda d: d['tasks'][0].update(agent_task='different prompt'),
        lambda d: d['tasks'][0]['revisions'].update(before='not-a-sha'),
        lambda d: d['tasks'][0]['changes']['files'][0].update(path='new/../escape.py'),
        lambda d: d['tasks'][0].update(golden_diff='unexpected'),
        lambda d: d['tasks'][0]['changes']['files'][0].update(before_blob='a' * 40),
        lambda d: d['meta'].update(status='running'),
        lambda d: d['meta']['lineage'].reverse(),
    ],
)
def test_inconsistent_basket_rejected_before_agent(tmp_path: Path, mutate: Callable[[FixtureBasket], object]) -> None:
    """Verify inconsistent basket rejected before agent."""
    data = payload()
    mutate(data)
    path = tmp_path / 'basket.json'
    path.write_bytes(canonical_bytes(data))
    with pytest.raises(BenchmarkError):
        load_basket(path)
