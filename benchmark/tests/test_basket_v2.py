"""Frozen v2 population and selection invariants, independently of collector runtime."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from benchmark.basket import load_basket
from benchmark.commands import Command
from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError
from benchmark.storage import canonical_bytes, sha256
from test_basket import payload as legacy_payload


def payload(provider: str = 'github') -> dict:
    """Read an offline producer-validated fixture."""
    return json.loads((Path(__file__).parent / 'fixtures/basket-v2' / f'{provider}.json').read_text())


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_v2_preserves_frozen_bytes(tmp_path: Path, provider: str) -> None:
    """Accept v2 without converting its bytes, rows or provenance."""
    data = payload(provider)
    content = canonical_bytes(data) + b'\n'
    path = tmp_path / 'basket.json'
    path.write_bytes(content)
    basket = load_basket(path)
    assert basket.content == content
    assert basket.rows == data['tasks']
    assert basket.failed_count == 0
    assert basket.rejected_count == 0


@pytest.mark.parametrize(
    'mutate',
    [
        lambda d: d['meta'].update(schema_version=2.0),
        lambda d: d['meta']['coverage']['collect'].update(input=2),
        lambda d: d['meta']['coverage']['filter'].update(pending=1),
        lambda d: d['meta']['selection'].update(fingerprint='f' * 64),
        lambda d: d['meta']['selection'].update(generation_keys={}),
        lambda d: d['tasks'][0]['provenance']['generation'].update(input_sha256='f' * 64),
    ],
)
def test_v2_rejects_inconsistent_inputs(tmp_path: Path, mutate: Callable[[dict], object]) -> None:
    """Schema acceptance alone cannot validate a frozen selection."""
    data = payload()
    mutate(data)
    path = tmp_path / 'basket.json'
    path.write_bytes(canonical_bytes(data))
    with pytest.raises(BenchmarkError):
        load_basket(path)


def add_omission(data: dict, stage: str, kind: str = 'failed') -> str:
    """Add one upstream failure or selected unsuccessful generation to a fixture."""
    meta = data['meta']
    identity = 'change:' + ('d' if stage == 'generate' else 'e') * 64
    meta['partial'] = True
    meta['omissions'].append(
        {'id': identity, 'stage': stage, 'kind': kind, 'code': 'fixture', 'result_sha256': 'f' * 64}
    )
    coverage = meta['coverage']
    coverage['discovery']['matched'] += 1
    coverage['discovery']['selected'] += 1
    for predecessor in ('collect', 'filter', 'generate'):
        coverage[predecessor]['input'] += 1
        coverage[predecessor][kind if predecessor == stage else 'succeeded'] += 1
        if predecessor == stage:
            break
    if stage == 'generate':
        coverage['selected'] += 1
        selection = meta['selection']
        selection['selected_ids'].append(identity)
        selection['generation_keys'][identity] = 'a' * 64
        selection['fingerprint'] = sha256(
            {'tasks': [[value, selection['generation_keys'][value]] for value in selection['selected_ids']]}
        )
    return identity


@pytest.mark.parametrize(
    ('stage', 'kind'), [('collect', 'failed'), ('filter', 'failed'), ('generate', 'failed'), ('generate', 'rejected')]
)
def test_v2_partial_accounting(tmp_path: Path, stage: str, kind: str) -> None:
    """Keep only exported tasks and distinguish rejection from failure."""
    data = payload()
    add_omission(data, stage, kind)
    path = tmp_path / 'basket.json'
    path.write_bytes(canonical_bytes(data))
    basket = load_basket(path)
    assert basket.failed_count == (kind == 'failed')
    assert basket.rejected_count == (kind == 'rejected')
    assert basket.rows == data['tasks']


@pytest.mark.parametrize(
    'mutate',
    [
        lambda d: d['meta']['omissions'][0].update(id=d['tasks'][0]['id']),
        lambda d: d['meta']['omissions'][0].update(stage='filter'),
        lambda d: d['meta']['omissions'][0].update(kind='failed'),
        lambda d: d['meta']['omissions'][0].update(result_sha256='not-a-hash'),
        lambda d: d['meta']['coverage']['generate'].update(rejected=0),
        lambda d: d['meta'].update(partial=False),
    ],
)
def test_v2_omission_mismatch_rejected(tmp_path: Path, mutate: Callable[[dict], object]) -> None:
    """Reject selected rejection edits even when the task itself is unchanged."""
    data = payload()
    add_omission(data, 'generate', 'rejected')
    mutate(data)
    path = tmp_path / 'basket.json'
    path.write_bytes(canonical_bytes(data))
    with pytest.raises(BenchmarkError):
        load_basket(path)


def test_v2_capped_discovery_is_not_partial(tmp_path: Path) -> None:
    """A deliberately bounded population does not imply a lost task."""
    data = payload()
    data['meta']['coverage']['discovery'].update(matched=100, capped=True)
    path = tmp_path / 'basket.json'
    path.write_bytes(canonical_bytes(data))
    assert not load_basket(path).data['meta']['partial']


def test_v2_schema_and_fixtures_match_producer() -> None:
    """Keep independently packaged consumers aligned with the producer snapshot."""
    root = Path(__file__).resolve().parents[2]
    assert (root / 'benchmark/src/schemas/benchmark-basket-v2.schema.json').read_bytes() == (
        root / 'basket_collector/src/schemas/basket-v2.schema.json'
    ).read_bytes()
    for provider in ('github', 'gitlab'):
        assert (root / f'benchmark/tests/fixtures/basket-v2/{provider}.json').read_bytes() == (
            root / f'basket_collector/tests/fixtures/basket-contract/{provider}.json'
        ).read_bytes()


@pytest.mark.parametrize('version', [1, 2])
def test_command_warns_once_and_logs_at_error_level(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: int
) -> None:
    """Internal reads stay silent; command warnings survive disabled progress and ERROR."""
    data = payload() if version == 2 else legacy_payload()
    path = tmp_path / 'basket.json'
    path.write_bytes(canonical_bytes(data))
    load_basket(path)
    load_basket(path)
    assert capsys.readouterr().err == ''
    command = Command(tmp_path, 'visualize', level='ERROR', progress_mode='off')
    command.watch_interrupts(lambda: 0)
    command.finish('completed')
    command.close()
    output = capsys.readouterr().err
    assert output.count('Basket schema v1 is deprecated') == (version == 1)
    assert (command.logs_dir / 'benchmark.log').read_text().count('Basket schema v1 is deprecated') == (version == 1)


@pytest.mark.parametrize('value', [None, [], True, {'meta': None}, {'meta': {'schema_version': '2'}}])
def test_direct_schema_dispatch_rejects_unknown_shapes(value: object) -> None:
    """Direct validation callers receive a safe contract error too."""
    with pytest.raises(BenchmarkError):
        validate_document('basket', value)
