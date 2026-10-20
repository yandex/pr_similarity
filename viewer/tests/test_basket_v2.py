"""Independent basket validation and safe v1/v2 population summaries."""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import archive_fixture
from test_service import publish, service
from viewer.archive import validate_archive
from viewer.basket import basket_summary, validate_basket, validate_basket_report
from viewer.errors import ViewerError
from viewer.presentation import basket_metadata
from viewer.util import digest, json_bytes, parse_json


def payload(provider: str = 'github') -> dict:
    """Read the independently shipped producer fixture."""
    return json.loads((Path(__file__).parent / 'fixtures/basket-v2' / f'{provider}.json').read_text())


def add_rejected(data: dict) -> None:
    """Include one selected generation rejection without any failed record."""
    meta = data['meta']
    identity = 'change:' + 'd' * 64
    meta['partial'] = True
    meta['omissions'].append(
        {'id': identity, 'stage': 'generate', 'kind': 'rejected', 'code': 'fixture', 'result_sha256': 'f' * 64}
    )
    coverage = meta['coverage']
    coverage['discovery'].update(matched=2, selected=2)
    for stage in ('collect', 'filter', 'generate'):
        coverage[stage]['input'] += 1
        coverage[stage]['rejected' if stage == 'generate' else 'succeeded'] += 1
    coverage['selected'] += 1
    selection = meta['selection']
    selection['selected_ids'].append(identity)
    selection['generation_keys'][identity] = 'a' * 64
    selection['fingerprint'] = digest(
        json_bytes({'tasks': [[value, selection['generation_keys'][value]] for value in selection['selected_ids']]})
    )


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
@pytest.mark.parametrize('partial', [False, True])
def test_v2_population_and_public_projection(provider: str, partial: bool) -> None:
    """The public summary distinguishes rejected omissions without exposing receipts."""
    data = payload(provider)
    if partial:
        add_rejected(data)
    before = json_bytes(data)
    validate_basket(data)
    summary = basket_summary(data)
    assert summary['partial'] is partial
    assert summary['failed_count'] == 0
    assert summary['rejected_count'] == int(partial)
    assert summary['total_tasks'] == 1
    assert set(basket_metadata(data)) == {'name', 'description', 'task_source'}
    assert 'generation_keys' not in json.dumps(basket_metadata(data))
    assert json_bytes(data) == before
    source = data['tasks'][0]
    task = {**source, **source['change_request'], **source['revisions']}
    report = {'basket': {'partial': partial, 'total_tasks': 1, 'omitted_failed_count': 0}, 'tasks': [task]}
    validate_basket_report(data, report)
    report['basket']['omitted_failed_count'] = 1
    with pytest.raises(ViewerError):
        validate_basket_report(data, report)


@pytest.mark.parametrize(
    'mutate',
    [
        lambda d: d['meta'].update(schema_version=True),
        lambda d: d['meta'].update(schema_version=2.0),
        lambda d: d['meta'].update(schema_version=1),
        lambda d: d['meta']['coverage']['collect'].update(input=2),
        lambda d: d['meta']['coverage']['filter'].update(pending=1),
        lambda d: d['meta']['selection'].update(fingerprint='f' * 64),
        lambda d: d['meta']['selection'].update(generation_keys={}),
        lambda d: d['tasks'][0]['provenance']['generation'].update(input_sha256='f' * 64),
        lambda d: d['tasks'][0].update(agent_task='altered'),
        lambda d: d['tasks'][0]['provenance']['retrieval'].update(before='f' * 40),
    ],
)
def test_v2_semantic_tampering_is_rejected(mutate: Callable[[dict], object]) -> None:
    """A known format never substitutes for consistent records and provenance."""
    data = payload()
    mutate(data)
    with pytest.raises(ViewerError):
        validate_basket(data)


def test_v1_projection_leaves_stage_coverage_unknown() -> None:
    """Legacy exports remain valid without inventing upstream counts."""
    data = json.loads((Path(__file__).parent / 'fixtures/basket.json').read_text())
    validate_basket(data)
    assert basket_summary(data)['coverage'] is None
    assert basket_summary(data)['schema_version'] == 1


def test_v2_contract_snapshot_matches_producer() -> None:
    """The wheel ships the exact public producer schema without importing producer code."""
    root = Path(__file__).resolve().parents[2]
    assert (root / 'viewer/src/schemas/basket-v2.json').read_bytes() == (
        root / 'basket_collector/src/schemas/basket-v2.schema.json'
    ).read_bytes()
    for provider in ('github', 'gitlab'):
        assert (root / f'viewer/tests/fixtures/basket-v2/{provider}.json').read_bytes() == (
            root / f'basket_collector/tests/fixtures/basket-contract/{provider}.json'
        ).read_bytes()


@pytest.mark.parametrize('field', ['workspace', 'agent_task', 'source', 'before', 'partial'])
def test_stored_semantic_damage_is_quarantined(tmp_path: Path, field: str) -> None:
    """Recomputed outer digests cannot hide malformed basket or mismatched report data."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    prefix = svc.runs[rid]['prefix']
    marker = parse_json(svc.store.get(prefix + 'publication.json', svc.config.limits.json_bytes))
    if field in ('workspace', 'source'):
        basket = parse_json(svc.store.get(prefix + 'data/basket.json', svc.config.limits.json_bytes))
        if field == 'workspace':
            basket['tasks'][0]['workspace'] = '../escape'
        else:
            basket['tasks'][0]['source']['fetch_refs'] = ['refs/a..b']
        raw = json_bytes(basket)
        svc.store.put(prefix + 'data/basket.json', raw)
        marker['basket_sha256'] = digest(raw)
        report['basket']['sha256'] = digest(raw)
    elif field == 'partial':
        report['basket']['partial'] = True
    else:
        report['tasks'][0][field] = 'wrong' if field == 'agent_task' else 'f' * 40
    raw = json_bytes(report)
    svc.store.put(prefix + 'data/report.json', raw)
    marker['report_sha256'] = digest(raw)
    svc.store.put(prefix + 'publication.json', json_bytes(marker))
    assert svc.refresh()['quarantined'] == 1
    assert rid not in svc.runs
    assert svc.db.owner(rid) is not None
    svc.db.close()


def prepare_v2(report: dict, basket: dict) -> None:
    """Freeze a synthetic v2 archive without changing its runnable task or evidence."""
    contract = payload()
    meta = basket['meta']
    meta.pop('omitted_failed_ids')
    meta['schema_version'] = 2
    for key in ('coverage', 'selection', 'omissions'):
        meta[key] = contract['meta'][key]
    row = basket['tasks'][0]
    key = 'a' * 64
    row['provenance']['generation'].update(input_sha256=key, reused_from=None)
    meta['selection']['selected_ids'] = [row['id']]
    meta['selection']['generation_keys'] = {row['id']: key}
    meta['selection']['fingerprint'] = digest(json_bytes({'tasks': [[row['id'], key]]}))
    add_rejected(basket)
    report['basket'].update(partial=True, omitted_failed_count=0)


def test_mixed_store_preserves_frozen_archives_and_partial_counts(tmp_path: Path) -> None:
    """V1 and rejected-only v2 coexist across refresh without archive conversion."""
    svc = service(tmp_path)
    legacy, _, _ = publish(svc, tmp_path)
    path = tmp_path / 'v2.tar.gz'
    report, _ = archive_fixture(path, run_id=str(uuid.uuid4()), prepare=prepare_v2)
    imported = validate_archive(path, tmp_path / 'v2-data', svc.config.limits)
    receipt = svc.publish(imported, 'alice', 'v2-test')
    rid = report['run']['id']
    row = svc.run(rid)
    summary = basket_summary(imported.basket)
    assert summary['partial'] is True
    assert summary['failed_count'] == 0
    assert summary['rejected_count'] == 1
    assert set(row['basket']) == set(svc.run(legacy['run']['id'])['basket'])
    svc.patch_metadata(rid, 'alice', {'display_name': 'Preserved', 'tags': ['custom']})
    svc.publish(imported, 'alice', 'v2-overwrite', overwrite=True)
    assert svc.refresh()['runs'] == 2
    assert svc.run(rid)['basket'] == row['basket']
    assert svc.run(rid)['display_name'] == 'Preserved'
    assert receipt['archive_sha256'] == digest(path.read_bytes())
    prefix = svc.runs[rid]['prefix']
    assert svc.store.get(prefix + 'archive.tar.gz', svc.config.limits.archive_bytes) == path.read_bytes()
    conflict = tmp_path / 'replacement.tar.gz'
    archive_fixture(conflict, run_id=legacy['run']['id'], prepare=prepare_v2)
    replacement = validate_archive(conflict, tmp_path / 'replacement-data', svc.config.limits)
    with pytest.raises(ViewerError):
        svc.publish(replacement, 'alice', 'wrong-basket', overwrite=True)
    assert svc.run(legacy['run']['id'])['basket_hash'] == legacy['basket']['sha256']
    svc.db.close()


def test_v2_diagnostic_names_field_without_echoing_values() -> None:
    """Schema and semantic errors locate the mismatch without exposing submitted data."""
    data = payload()
    data['meta']['selection']['fingerprint'] = 'f' * 64
    with pytest.raises(ViewerError, match=r'v2.*meta.selection.fingerprint'):
        validate_basket(data)
    data = payload()
    data['meta']['selection']['schema_version'] = 'private-value-not-to-log'
    with pytest.raises(ViewerError, match=r'v2.*meta.selection.schema_version') as error:
        validate_basket(data)
    assert 'private-value-not-to-log' not in error.value.message


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_import_warns_once_but_refresh_and_reads_do_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, version: int
) -> None:
    """Deprecated input generates one server diagnostic and keeps the public UI unchanged."""
    svc = service(tmp_path)
    path = tmp_path / 'import.tar.gz'
    report, _ = archive_fixture(path, prepare=prepare_v2 if version == 2 else None)
    imported = validate_archive(path, tmp_path / 'data', svc.config.limits)
    svc.publish(imported, 'alice', 'warning-test')
    svc.refresh()
    svc.run(report['run']['id'])
    svc.run(report['run']['id'])
    messages = [record for record in caplog.records if 'Basket schema v1 is deprecated' in record.message]
    assert len(messages) == (version == 1)
    svc.db.close()


@pytest.mark.parametrize(('name', 'count'), [('opensearch-10.json', 10), ('pandas-25.json', 25)])
def test_shipped_real_v1_baskets_are_still_valid(name: str, count: int) -> None:
    """Validate all rows of the actual published examples, without a producer import."""
    path = Path(__file__).parents[2] / 'benchmark/examples/baskets/legacy' / name
    original = path.read_bytes()
    data = json.loads(original)
    validate_basket(data)
    assert basket_summary(data)['total_tasks'] == count
    assert set(basket_metadata(data)) == {'name', 'description', 'task_source'}
    assert path.read_bytes() == original
