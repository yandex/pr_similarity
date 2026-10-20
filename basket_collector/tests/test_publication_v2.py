"""Keep immutable stage revisions and the last complete basket across interrupted updates."""

from pathlib import Path

import pytest
from basket_collector import artifact_io
from basket_collector.contracts import BasketDocument, CollectDocument, FilterDocument, TasksDocument
from basket_collector.publication import PublicationStore
from basket_collector.storage import StorageError, receipt
from test_contracts import _basket, _collect, _filter, _tasks
from test_contracts_v2 import selection


def document(title: str = 'original') -> CollectDocument:
    """Build a completed source snapshot with a configurable semantic change."""
    data = _collect()
    data['meta']['schema_version'] = 2
    data['tasks'][0]['change_request']['title'] = title
    return CollectDocument.model_validate(data)


def test_publication_keeps_old_revision_and_moves_one_authoritative_pointer(tmp_path: Path) -> None:
    """Preserve exact old bytes when a newer completed stage becomes current."""
    store = PublicationStore(tmp_path)
    store.commit(document(), {})
    previous = (tmp_path / '.current').resolve()
    original = (previous / 'working/collected.json').read_bytes()
    store.commit(document('changed'), {})
    assert (tmp_path / '.current').resolve() != previous
    assert (previous / 'working/collected.json').read_bytes() == original
    loaded = store.load('collect')
    assert loaded is not None and loaded['tasks'][0]['change_request']['title'] == 'changed'
    assert (tmp_path / 'collected.json').read_bytes() != original


def test_crash_before_pointer_switch_preserves_previous_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpublished revision never becomes a mixed or missing current result."""
    store = PublicationStore(tmp_path)
    store.commit(document(), {})
    previous = (tmp_path / '.current').readlink()

    def fail(target: Path) -> None:
        """Inject an interruption after the complete new revision is on disk."""
        raise OSError('fixture interrupted pointer switch')

    monkeypatch.setattr(store, '_switch', fail)
    with pytest.raises(OSError):
        store.commit(document('changed'), {})
    assert (tmp_path / '.current').readlink() == previous
    loaded = store.load('collect')
    assert loaded is not None and loaded['tasks'][0]['change_request']['title'] == 'original'


def test_legacy_output_is_refused_even_before_new_publication(tmp_path: Path) -> None:
    """Do not bootstrap, import or overwrite a version-one working directory."""
    path = tmp_path / 'collected.json'
    path.write_text('{"meta":{"schema_version":1}}')
    original = path.read_bytes()
    with pytest.raises(StorageError, match='version 2'):
        PublicationStore(tmp_path).commit(document(), {})
    assert path.read_bytes() == original and not (tmp_path / '.current').exists()


def test_editing_an_immutable_object_is_detected(tmp_path: Path) -> None:
    """Never trust a root alias whose object bytes no longer match the manifest."""
    store = PublicationStore(tmp_path)
    store.commit(document(), {})
    target = (tmp_path / 'collected.json').resolve()
    target.write_bytes(target.read_bytes() + b' ')
    with pytest.raises(StorageError, match='digest'):
        store.load('collect')


def test_redirected_public_view_is_rejected(tmp_path: Path) -> None:
    """A valid manifest cannot conceal a changed path read by external consumers."""
    store = PublicationStore(tmp_path)
    store.commit(document(), {})
    view = (tmp_path / '.current').resolve() / 'working/collected.json'
    view.unlink()
    view.symlink_to('../../../.objects/' + '0' * 64 + '.json')
    with pytest.raises(StorageError, match='view differs'):
        store.load('collect')


@pytest.mark.parametrize('symlink', [False, True])
def test_selection_is_checked_before_work(tmp_path: Path, symlink: bool) -> None:
    """Reject an unmanaged selection even when no stage document has been written yet."""
    path = tmp_path / 'selection.json'
    if symlink:
        path.symlink_to('foreign.json')
    else:
        path.write_text('{"schema_version":1}')
    with pytest.raises(StorageError):
        PublicationStore(tmp_path).check_layout()


def test_atomic_writer_syncs_new_directory_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A durable attempt acknowledgement includes newly created parent directory entries."""
    synced = []
    monkeypatch.setattr(artifact_io, '_fsync_directory', synced.append)
    output = tmp_path / 'new-output'
    work = output / '.work'
    artifact_io.atomic_write(work / 'filter.json', {'schema_version': 2})
    assert synced == [tmp_path, output, work]


def test_previous_export_survives_upstream_revision(tmp_path: Path) -> None:
    """Working collect changes cannot replace a previously coherent frozen basket."""
    payloads = [_collect(), _filter(), _tasks(), _basket()]
    identity = payloads[0]['tasks'][0]['id']
    snapshot = selection(identity)
    selected = {
        key: value
        for key, value in snapshot.items()
        if key not in {'settings', 'generation_settings_sha256', 'excluded', 'omissions'}
    }
    payloads[1]['selection'] = snapshot
    payloads[2]['selection'] = selected
    payloads[2]['tasks'][0]['generation']['input_sha256'] = 'd' * 64
    payloads[3]['tasks'][0]['provenance']['generation']['input_sha256'] = 'd' * 64
    counts = {'input': 1, 'succeeded': 1, 'rejected': 0, 'failed': 0, 'pending': 0}
    payloads[3]['meta'].update(
        omissions=[],
        selection=selected,
        coverage={
            'discovery': {'matched': 1, 'selected': 1, 'capped': False},
            'collect': counts,
            'filter': counts,
            'selected': 1,
            'generate': counts,
        },
    )
    store = PublicationStore(tmp_path)
    lineage = []
    for payload, model in zip(payloads, (CollectDocument, FilterDocument, TasksDocument, BasketDocument), strict=True):
        payload['meta'].update(
            schema_version=2,
            lineage=lineage,
            input={'stage': lineage[-1]['stage'], 'sha256': lineage[-1]['sha256']} if lineage else None,
        )
        item = model.model_validate(payload)
        store.commit(item, {})
        lineage = [*lineage, receipt(item).model_dump(mode='json')]
    original = (tmp_path / 'basket.json').read_bytes()
    store.commit(document('new upstream'), {})
    assert (tmp_path / 'basket.json').read_bytes() == original
    assert (tmp_path / 'collected.json').read_bytes() != original
    BasketDocument.model_validate_json(original)
