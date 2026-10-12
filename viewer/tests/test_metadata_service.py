"""Publication and quarantine preserve owner state independently from inference evidence."""

import uuid
from pathlib import Path

import pytest
from helpers import archive_fixture, inference
from test_service import publish, service
from viewer.archive import ImportedRun, validate_archive
from viewer.errors import ViewerError
from viewer.util import digest, json_bytes, parse_json


def test_refresh_quarantines_old_report_without_pruning_metadata_order_accounts(tmp_path: Path) -> None:
    """One incompatible schema-1 object cannot disable valid runs or erase server state."""
    svc = service(tmp_path)
    valid, _, _ = publish(svc, tmp_path)
    invalid, _, _ = publish(svc, tmp_path, run_id=str(uuid.uuid4()))
    rid = invalid['run']['id']
    svc.patch_metadata(rid, 'alice', {'display_name': 'Preserved', 'tags': ['model:custom', 'Baseline', 'Baseline']})
    owner_before = svc.db.owner(rid)
    prefix = svc.runs[rid]['prefix']
    old_report = {'schema_version': 1, 'run': {'id': rid}}
    raw = json_bytes(old_report)
    svc.store.put(prefix + 'data/report.json', raw)
    marker = parse_json(svc.store.get(prefix + 'publication.json', svc.config.limits.json_bytes))
    marker['report_sha256'] = digest(raw)
    basket = parse_json(svc.store.get(prefix + 'data/basket.json', svc.config.limits.json_bytes))
    basket['meta']['name'] = 'Quarantined basket'
    basket_raw = json_bytes(basket)
    invalid_hash = digest(basket_raw)
    svc.store.put(prefix + 'data/basket.json', basket_raw)
    marker['basket_sha256'] = invalid_hash
    svc.store.put(prefix + 'publication.json', json_bytes(marker))
    order = [invalid_hash, valid['basket']['sha256']]
    svc.store.put('settings/basket-order.json', json_bytes(order))
    flags = {invalid_hash: {'hidden': True, 'collapsed': True}}
    svc.store.put('settings/basket-display.json', json_bytes({'version': 1, 'items': flags}))
    before_metadata = svc.store.get('settings/viewer-metadata.json', svc.config.limits.json_bytes)
    result = svc.refresh()
    assert result['quarantined'] == 1
    assert set(svc.runs) == {valid['run']['id']}
    assert svc.run(valid['run']['id'])['score'] == 1
    assert svc.db.owner(rid) == owner_before
    assert svc.store.get('settings/viewer-metadata.json', svc.config.limits.json_bytes) == before_metadata
    assert parse_json(svc.store.get('settings/basket-order.json', svc.config.limits.json_bytes)) == order
    assert (
        parse_json(svc.store.get('settings/basket-display.json', svc.config.limits.json_bytes))['items'][invalid_hash]
        == flags[invalid_hash]
    )
    svc.set_order(svc.order)
    assert parse_json(svc.store.get('settings/basket-order.json', svc.config.limits.json_bytes)) == order
    assert (
        parse_json(svc.store.get('settings/basket-display.json', svc.config.limits.json_bytes))['items'][invalid_hash]
        == flags[invalid_hash]
    )
    svc.db.close()


def test_overwrite_updates_generated_groups_and_keeps_exact_server_setup(tmp_path: Path) -> None:
    """Model/effort refresh independently from owner name, duplicates and pin state."""
    svc = service(tmp_path)

    def imported(name: str, effort: str, rid: str | None = None) -> tuple[dict, ImportedRun]:
        """Build model-specific archived evidence for a publication transition."""

        def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
            """Freeze the selected model and effort independently from user labels."""
            report['agent']['name'] = name
            report['run']['tags'] = ['New local label']
            report['inference'] = inference('openai/' + name, effort)
            identity = {
                'identity_version': 1,
                'native_config': {
                    'model': 'openai/' + name,
                    'provider': {'openai': {'models': {name: {'options': {'reasoningEffort': effort}}}}},
                },
            }
            files['config_identity.json'] = json_bytes(identity)
            report['config']['identity_sha256'] = digest(json_bytes(identity))

        path = tmp_path / (name + '.tar.gz')
        report, _ = archive_fixture(path, run_id=rid, configure=change)
        return report, validate_archive(path, tmp_path / name, svc.config.limits)

    first, archive = imported('First', 'medium')
    svc.publish(archive, 'alice', 'first')
    rid = first['run']['id']
    labels = ['model:custom', 'effort:high', 'Baseline', 'Baseline']
    svc.patch_metadata(rid, 'alice', {'display_name': 'Server label', 'tags': labels})
    svc.set_pinned(rid, True)
    _, replacement = imported('Second', 'low', rid)
    svc.publish(replacement, 'alice', 'second', overwrite=True)
    row = svc.run(rid)
    assert row['display_name'] == 'Server label' and row['tags'] == labels and row['pinned'] is True
    assert row['tag_groups'] == {'model': ['openai/Second [low]'], 'effort': ['low'], 'setup': labels}
    svc.refresh()
    assert svc.run(rid)['tag_groups'] == row['tag_groups']
    svc.db.close()


def test_old_report_shape_is_rejected_before_publication_writes(tmp_path: Path) -> None:
    """An old schema-1 report cannot yield a receipt while absent from the catalogue."""
    svc = service(tmp_path)
    path = tmp_path / 'old-report.tar.gz'
    archive_fixture(path)
    imported = validate_archive(path, tmp_path / 'old-data', svc.config.limits)
    imported.report.pop('inference')
    before = list(svc.store.list(''))
    with pytest.raises(ViewerError) as error:
        svc.publish(imported, 'alice', 'old-report')
    assert error.value.code == 'invalid_report'
    assert list(svc.store.list('')) == before
    assert svc.db.operations() == {}
    assert svc.db.owner(imported.report['run']['id']) is None
    svc.db.close()
