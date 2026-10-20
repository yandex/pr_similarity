"""Bind export accounting to frozen upstream artifacts rather than mutable heads."""

import json
from pathlib import Path

import pytest
from basket_collector.contracts import BasketDocument, CollectDocument
from basket_collector.remote import RemoteError
from basket_collector.storage import StageStore, StorageError, read_document
from test_collect_rejections import RejectingGitHub, pipeline
from test_pipeline import _config, _filter_one


@pytest.mark.parametrize('direct', [False, True])
def test_export_upstream_omission_edits_preserve_old_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct: bool
) -> None:
    """Both checkpoint resume and direct publication reject a modified upstream omission."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    config.collect.change_ids = [1, 3]
    client = RejectingGitHub(failure=RemoteError('fixture unavailable'))
    assert pipeline(config, client).run('basket', allow_partial=True) == 0
    output = Path(config.output_dir)
    original = (output / 'basket.json').read_bytes()
    payload = json.loads(original)
    payload['meta']['omissions'][0]['code'] = 'edited_upstream_failure'
    basket = BasketDocument.model_validate(payload)
    store = StageStore(output, 'export')
    if direct:
        with pytest.raises(StorageError, match='omissions'):
            store.publication.commit(basket, {})
    else:
        payload['meta'].update(status='interrupted', completed_at=None)
        payload['checkpoint'] = {'phase': 'ready'}
        store.work_path.parent.mkdir(exist_ok=True)
        store.work_path.write_text(json.dumps(payload))
        with pytest.raises(StorageError, match='omissions'):
            pipeline(config, client).run('export', allow_partial=True)
    assert (output / 'basket.json').read_bytes() == original


def test_saved_export_accounting_uses_historical_not_current_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New upstream heads cannot invalidate the accounting of an intact older frozen basket."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    runner = pipeline(config, RejectingGitHub())
    assert runner.run('basket') == 0
    output = Path(config.output_dir)
    original = (output / 'basket.json').read_bytes()
    collected = read_document(output / 'collected.json', CollectDocument)
    collected.tasks[0].change_request.title = 'new upstream title'
    store = StageStore(output, 'export')
    store.publication.commit(collected, {})
    basket = BasketDocument.model_validate_json(original)
    old, checkpoint = runner._prior_stage(store, BasketDocument, basket.meta.settings, False)
    assert old == basket and checkpoint is None
    assert (output / 'basket.json').read_bytes() == original
