"""Independent basket validation and current v3 population summaries."""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import archive_fixture
from test_service import publish, service

from viewer.errors import ViewerError
from viewer.presentation.queries import basket_metadata
from viewer.util import digest, json_bytes, parse_json, require_object
from viewer.validation.archive import validate_archive
from viewer.validation.basket import basket_summary, validate_basket, validate_basket_report


def payload(provider: str = 'github') -> dict:
    """Read the independently shipped producer fixture."""
    return json.loads((Path(__file__).parent / 'fixtures/basket-v3' / f'{provider}.json').read_text())


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
def test_v3_population_and_public_projection(provider: str, partial: bool) -> None:
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
    assert set(basket_metadata(validate_basket(data))) == {'name', 'description', 'task_source'}
    assert 'generation_keys' not in json.dumps(basket_metadata(validate_basket(data)))
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
        lambda d: d['meta'].update(schema_version=3.0),
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
def test_v3_semantic_tampering_is_rejected(mutate: Callable[[dict], object]) -> None:
    """A known format never substitutes for consistent records and provenance."""
    data = payload()
    mutate(data)
    with pytest.raises(ViewerError):
        validate_basket(data)


@pytest.mark.parametrize('version', [0, 4])
def test_earlier_basket_versions_are_rejected(version: int) -> None:
    """Earlier formats cannot enter the current viewer through direct validation."""
    data = payload()
    data['meta']['schema_version'] = version
    with pytest.raises(ViewerError) as caught:
        validate_basket(data)
    assert caught.value.code == 'invalid_basket'
    assert '3' in caught.value.message


def test_v3_contract_snapshot_matches_producer() -> None:
    """The wheel ships the exact public producer schema without importing producer code."""
    root = Path(__file__).resolve().parents[2]
    assert (root / 'viewer/src/schemas/basket-v3.json').read_bytes() == (
        root / 'basket_collector/src/schemas/basket-v3.schema.json'
    ).read_bytes()
    for provider in ('github', 'gitlab'):
        assert (root / f'viewer/tests/fixtures/basket-v3/{provider}.json').read_bytes() == (
            root / f'basket_collector/tests/fixtures/basket-contract/{provider}.json'
        ).read_bytes()


@pytest.mark.parametrize('field', ['workspace', 'agent_task', 'source', 'before', 'partial'])
def test_stored_semantic_damage_is_quarantined(tmp_path: Path, field: str) -> None:
    """Recomputed outer digests cannot hide malformed basket or mismatched report data."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    prefix = svc.runs[rid]['prefix']
    marker = require_object(parse_json(svc.store.get(prefix + 'publication.json', svc.config.limits.json_bytes)))
    if field in ('workspace', 'source'):
        basket = require_object(parse_json(svc.store.get(prefix + 'data/basket.json', svc.config.limits.json_bytes)))
        tasks = basket['tasks']
        assert isinstance(tasks, list)
        task = require_object(tasks[0])
        if field == 'workspace':
            task['workspace'] = '../escape'
        else:
            require_object(task['source'])['fetch_refs'] = ['refs/a..b']
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


def prepare_v3(report: dict, basket: dict) -> None:
    """Freeze a synthetic v3 archive without changing its runnable task or evidence."""
    contract = payload()
    meta = basket['meta']
    meta['schema_version'] = 3
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


def test_complete_and_partial_store_preserves_frozen_archives(tmp_path: Path) -> None:
    """Complete and rejected-only v3 baskets coexist with their exact saved bytes."""
    svc = service(tmp_path)
    complete_report, _, _ = publish(svc, tmp_path)
    path = tmp_path / 'v3.tar.gz'
    report, _ = archive_fixture(path, run_id=str(uuid.uuid4()), prepare=prepare_v3)
    imported = validate_archive(path, tmp_path / 'v3-data', svc.config.limits)
    receipt = svc.publish(imported, 'alice', 'v3-test')
    rid = report['run']['id']
    row = svc.run(rid)
    summary = basket_summary(imported.basket)
    assert summary['partial'] is True
    assert summary['failed_count'] == 0
    assert summary['rejected_count'] == 1
    assert set(row['basket']) == set(svc.run(complete_report['run']['id'])['basket'])
    svc.patch_metadata(rid, 'alice', {'display_name': 'Preserved', 'tags': ['custom']})
    svc.publish(imported, 'alice', 'v3-overwrite', overwrite=True)
    assert svc.refresh()['runs'] == 2
    assert svc.run(rid)['basket'] == row['basket']
    assert svc.run(rid)['display_name'] == 'Preserved'
    assert receipt['archive_sha256'] == digest(path.read_bytes())
    prefix = svc.runs[rid]['prefix']
    assert svc.store.get(prefix + 'archive.tar.gz', svc.config.limits.archive_bytes) == path.read_bytes()
    conflict = tmp_path / 'replacement.tar.gz'
    archive_fixture(conflict, run_id=complete_report['run']['id'], prepare=prepare_v3)
    replacement = validate_archive(conflict, tmp_path / 'replacement-data', svc.config.limits)
    with pytest.raises(ViewerError):
        svc.publish(replacement, 'alice', 'wrong-basket', overwrite=True)
    assert svc.run(complete_report['run']['id'])['basket_hash'] == complete_report['basket']['sha256']
    svc.db.close()


def test_v3_diagnostic_names_field_without_echoing_values() -> None:
    """Schema and semantic errors locate the mismatch without exposing submitted data."""
    data = payload()
    data['meta']['selection']['fingerprint'] = 'f' * 64
    with pytest.raises(ViewerError, match=r'v3.*meta.selection.fingerprint'):
        validate_basket(data)
    data = payload()
    data['meta']['selection']['schema_version'] = 'private-value-not-to-log'
    with pytest.raises(ViewerError, match=r'v3.*meta.selection.schema_version') as error:
        validate_basket(data)
    assert 'private-value-not-to-log' not in error.value.message


@pytest.mark.parametrize('version', [0, 4])
def test_unknown_basket_archives_are_rejected(tmp_path: Path, version: int) -> None:
    """Archive ingress rejects unknown basket versions without changing their bytes."""
    svc = service(tmp_path)
    path = tmp_path / 'earlier.tar.gz'

    def prepare(report: dict, basket: dict) -> None:
        """Keep the fixture valid except for its unsupported basket version."""
        basket['meta']['schema_version'] = version

    archive_fixture(path, prepare=prepare)
    original = path.read_bytes()
    with pytest.raises(ViewerError) as caught:
        validate_archive(path, tmp_path / 'data', svc.config.limits)
    assert caught.value.code == 'invalid_basket'
    assert path.read_bytes() == original
    assert not svc.runs
    svc.db.close()


def test_missing_export_stage_keeps_existing_failure() -> None:
    """A model default does not change the current format's missing-stage boundary."""
    data = payload()
    del data['meta']['stage']
    with pytest.raises(KeyError) as raised:
        validate_basket(data)
    assert raised.value.args == ('stage',)
