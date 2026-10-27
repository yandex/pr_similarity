"""Catalogue lifecycle and single-failure recovery over a mock S3 store."""

import shutil
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import archive_fixture, inference

from viewer.config import Config, Limits
from viewer.errors import StorageError, ViewerError
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore, Store
from viewer.service import ViewerService
from viewer.util import digest, json_bytes
from viewer.validation.archive import ImportedRun, validate_archive


def service(tmp_path: Path, store: Store | None = None) -> ViewerService:
    """Create a local viewer service with Alice and the requested storage backend."""
    config = Config(runtime=tmp_path / 'runtime', storage='mock')
    db = Database(config.runtime)
    db.create_user('alice', 'secret')
    return ViewerService(config, store or MemoryStore(), db)


def publish(
    svc: ViewerService, tmp_path: Path, *, run_id: str | None = None, key: str | None = None, score: float = 1
) -> tuple[dict, dict, ImportedRun]:
    """Publish or overwrite a fixture run and return its report, receipt and archive."""
    op = str(uuid.uuid4())
    path = tmp_path / (op + '.tar.gz')

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Set fixture scores while retaining explicit inference availability."""
        report.setdefault('inference', inference(None, None))
        report['tasks'][0]['scoring'].update(Mf_mlp=score, LCf_mlp=score)
        report['tasks'][0]['scoring']['files'][0]['mlp'] = score

    report, manifest = archive_fixture(path, run_id=run_id, configure=change)
    imported = validate_archive(path, tmp_path / (op + '-data'), Limits())
    receipt = svc.publish(imported, 'alice', key or op, overwrite=bool(run_id))
    return report, receipt, imported


def test_overwrite_delete_restore_preserve_exact_data_and_order(tmp_path: Path) -> None:
    """Verify overwrite delete restore preserve exact data and order."""
    svc = service(tmp_path)
    report, receipt, archive = publish(svc, tmp_path)
    rid = report['run']['id']
    basket = report['basket']['sha256']
    svc.patch_metadata(rid, 'alice', {'display_name': 'custom', 'tags': ['model: X', 'sys:pinned']})
    svc.set_pinned(rid, True)
    original = svc.download(rid, 'alice').read()
    assert original == archive.archive.read_bytes()
    second, _, _ = publish(svc, tmp_path, run_id=rid, score=0.5)
    row = svc.run(rid)
    assert row['display_name'] == 'custom' and row['pinned'] is True
    assert row['score'] == 0.5 and svc.order == [basket]
    assert len([k for k in svc.store.list('trash/') if k.endswith('/publication.json')]) == 1
    svc.delete(rid)
    assert svc.order == [] and svc.metadata == {} and svc.runs == {}
    owner = svc.db.owner(rid)
    assert owner is not None and owner['login'] == 'alice'
    latest = sorted(k for k in svc.store.list('trash/') if k.endswith('/publication.json'))[-1]
    prefix = latest.removesuffix('publication.json')
    for key in list(svc.store.list(prefix)):
        svc.store.copy(key, 'runs/alice/' + rid + '/' + key[len(prefix) :])
    svc.refresh()
    assert svc.run(rid)['display_name'] == 'baseline'
    assert svc.run(rid)['pinned'] is False
    assert svc.order == [basket]
    svc.db.close()


def test_receipt_replay_and_foreign_owner(tmp_path: Path) -> None:
    """Verify receipt replay and foreign owner."""
    svc = service(tmp_path)
    report, receipt, archive = publish(svc, tmp_path)
    assert svc.publish(archive, 'alice', receipt['idempotency_key']) == receipt
    assert list(svc.store.list('trash/')) == []
    with pytest.raises(ViewerError) as e:
        svc.publish(archive, 'bob', 'key', overwrite=True)
    assert e.value.status == 403
    with pytest.raises(ViewerError):
        svc.download(report['run']['id'], 'bob')
    svc.db.close()


class FaultStore(MemoryStore):
    """Inject one storage failure at the armed write or deletion boundary."""

    armed: str | None = None

    def put(self, key: str, data: bytes) -> None:
        """Fail once when the object key matches the armed write boundary."""
        if self.armed and self.armed in key:
            self.armed = None
            raise StorageError()
        return super().put(key, data)

    def delete(self, key: str) -> None:
        """Fail once when deletion is armed, then resume normal storage behavior."""
        if self.armed == 'DELETE':
            self.armed = None
            raise StorageError()
        return super().delete(key)


@pytest.mark.parametrize('failure', ['trash/', 'DELETE', 'publication.json', 'settings/'])
def test_restart_completes_one_backup(tmp_path: Path, failure: str) -> None:
    """Verify restart completes one backup."""
    store = FaultStore()
    svc = service(tmp_path, store)
    report, receipt, archive = publish(svc, tmp_path)
    rid = report['run']['id']
    store.armed = ('runs/alice/' + rid + '/publication.json') if failure == 'publication.json' else failure
    with pytest.raises(StorageError):
        publish(svc, tmp_path, run_id=rid, score=0.3)
    assert svc.db.operations()
    svc.db.close()
    db = Database(svc.config.runtime)
    restarted = ViewerService(svc.config, store, db)
    assert restarted.run(rid)['score'] == 0.3
    assert len([k for k in store.list('trash/') if k.endswith('/publication.json')]) == 1
    assert db.operations() == {}
    db.close()


def test_refresh_is_atomic_when_settings_corrupt(tmp_path: Path) -> None:
    """Verify refresh is atomic when settings corrupt."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    before = svc.run(report['run']['id'])
    svc.store.put('settings/viewer-metadata.json', b'{"bad":true}')
    with pytest.raises(ViewerError):
        svc.refresh()
    assert svc.run(report['run']['id']) == before
    svc.db.close()


def test_lost_staging_rolls_back_from_complete_trash(tmp_path: Path) -> None:
    """Verify lost staging rolls back from complete trash."""
    store = FaultStore()
    svc = service(tmp_path, store)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    svc.set_pinned(rid, True)
    store.armed = 'runs/alice/' + rid + '/publication.json'
    with pytest.raises(StorageError):
        publish(svc, tmp_path, run_id=rid, score=0.3)
    op = next(iter(svc.db.operations().values()))
    shutil.rmtree(op['staging'])
    svc.db.close()
    db = Database(svc.config.runtime)
    restarted = ViewerService(svc.config, store, db)
    assert restarted.run(rid)['score'] == 1
    assert restarted.run(rid)['pinned'] is True
    assert db.operations() == {}
    db.close()


def test_receipt_commit_failure_recovers_without_duplicate_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify receipt commit failure recovers without duplicate backup."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    original = svc.db.finish_operation

    def fail(*args: object, **kwargs: object) -> None:
        """Interrupt receipt persistence with a simulated disk failure."""
        raise OSError('disk fault')

    monkeypatch.setattr(svc.db, 'finish_operation', fail)
    with pytest.raises(OSError):
        publish(svc, tmp_path, run_id=report['run']['id'], score=0.6)
    assert svc.db.operations()
    monkeypatch.setattr(svc.db, 'finish_operation', original)
    svc.recover()
    assert svc.run(report['run']['id'])['score'] == 0.6
    assert len([k for k in svc.store.list('trash/') if k.endswith('/publication.json')]) == 1
    svc.db.close()


def test_catalogue_keeps_aggregates_and_loads_details_on_demand(tmp_path: Path) -> None:
    """Verify catalogue keeps aggregates and loads details on demand."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    row = svc.runs[rid]
    assert not hasattr(row['report'], 'tasks') and 'inventory' not in row['marker']
    assert 'tasks' not in row['basket']
    assert svc.full_report(rid).to_wire() == report
    assert svc.run(rid)['score'] == 1
    svc.db.close()


def test_orphan_staging_cleanup_is_scoped_and_begin_failure_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify orphan staging cleanup is scoped and begin failure cleans."""
    svc = service(tmp_path)
    op = svc.staging / str(uuid.uuid4())
    op.mkdir()
    (op / 'archive.tar.gz').write_bytes(b'partial')
    unknown = svc.staging / 'operator-notes'
    unknown.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    link = svc.staging / str(uuid.uuid4())
    link.symlink_to(outside, target_is_directory=True)
    svc.cleanup_staging()
    assert not op.exists() and unknown.exists() and (outside / 'keep').exists()

    def fail(*args: object, **kwargs: object) -> None:
        """Reject publication creation with a simulated database write failure."""
        raise sqlite3.OperationalError('write fault')

    monkeypatch.setattr(svc.db, 'begin_publication', fail)
    with pytest.raises(sqlite3.OperationalError):
        publish(svc, tmp_path)
    assert sorted(p.name for p in svc.staging.iterdir()) == sorted([unknown.name, link.name])
    svc.db.close()


def test_publish_deadline_keeps_progress_and_resumes_without_reupload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify publish deadline keeps progress and resumes without reupload."""
    clock = [0.0]

    class Slow(MemoryStore):
        """Record uploaded keys and advance the clock after each upload."""

        def __init__(self) -> None:
            """Initialize the fixture state used by the test."""
            super().__init__()
            self.uploads = []

        def put_file(self, key: str, path: Path, *, deadline: float | None = None) -> None:
            """Complete the upload and consume four seconds of the publication deadline."""
            super().put_file(key, path)
            self.uploads.append(key)
            clock[0] += 4

    store = Slow()
    svc = service(tmp_path, store)
    path = tmp_path / 'run.tar.gz'
    report, _ = archive_fixture(path)
    imported = validate_archive(path, tmp_path / 'data', Limits())
    monkeypatch.setattr('viewer.service.time', SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(StorageError):
        svc.publish(imported, 'alice', 'key', deadline=5)
    op_id, op = next(iter(svc.db.operations().items()))
    assert op['phase'] == 'installing'
    assert svc.db.progress(op_id, 'archive') == 1
    assert svc.db.progress(op_id, 'files') == 1
    completed = list(store.uploads)
    svc.recover()
    svc.refresh()
    assert svc.db.operations() == {}
    assert svc.run(report['run']['id'])['score'] == 1
    assert all(store.uploads.count(key) == 1 for key in completed)
    svc.db.close()


def test_run_basket_source_and_wall_time_are_stable_across_reprice_overwrite(tmp_path: Path) -> None:
    """Verify run basket source and wall time are stable across reprice overwrite."""
    svc = service(tmp_path)
    rid = str(uuid.uuid4())

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Separate completion and update times while adding generation provenance."""
        report['run'].update(
            created_at='2026-09-30T15:00:00Z', finished_at='2026-09-30T15:02:00Z', updated_at='2026-10-06T10:00:00Z'
        )
        basket['meta']['description'] = 'A public basket description'
        generation = next(item for item in basket['meta']['lineage'] if item['stage'] == 'generate')
        generation['settings'] = {'source': 'llm', 'context': 'diff+issue'}
        generation['settings_hash'] = digest(json_bytes(generation['settings']))
        files['basket.json'] = json_bytes(basket)
        report['basket']['sha256'] = __import__('hashlib').sha256(files['basket.json']).hexdigest()

    path = tmp_path / 'timing.tar.gz'
    report, _ = archive_fixture(path, run_id=rid, configure=change)
    imported = validate_archive(path, tmp_path / 'timing-data', Limits())
    svc.publish(imported, 'alice', 'first')
    row = svc.run(rid)
    assert row['wall_time_seconds'] == 120
    assert row['basket']['description'] == 'A public basket description'
    assert row['basket']['task_source'] == {'source': 'llm', 'context': 'diff+issue'}
    assert svc.run(rid, statuses=[])['wall_time_seconds'] == 120
    svc.publish(imported, 'alice', 'reprice-overwrite', overwrite=True)
    svc.refresh()
    assert svc.run(rid)['wall_time_seconds'] == 120
    assert svc.run(rid)['basket'] == row['basket']
    svc.db.close()


@pytest.mark.parametrize('value', [None, False, 7, 'scalar', []])
@pytest.mark.parametrize(
    'key,code',
    [
        ('settings/viewer-metadata.json', 'invalid_metadata'),
        ('settings/basket-display.json', 'invalid_basket_display'),
        ('runs/alice/invalid/publication.json', 'invalid_publication'),
    ],
)
def test_stored_object_boundaries_keep_existing_errors(tmp_path: Path, key: str, code: str, value: object) -> None:
    """Reject scalar/list storage documents before projecting their fields."""
    svc = service(tmp_path)
    try:
        svc.store.put(key, json_bytes(value))
        assert svc._json(key) == value
        with pytest.raises(ViewerError) as caught:
            svc.refresh()
        assert caught.value.code == code
        assert caught.value.status == 422
        assert caught.value.message == 'Uploaded documents or artifacts are inconsistent.'
    finally:
        svc.db.close()


@pytest.mark.parametrize('marker', [True, 1, 1.5, 'corrupt', ['corrupt']])
def test_recovery_stops_before_writes_for_truthy_non_object_marker(tmp_path: Path, marker: object) -> None:
    """Preserve the recovery failure instead of restoring over corrupt marker data."""
    svc = service(tmp_path)
    try:
        prefix = 'runs/alice/corrupt/'
        key = prefix + 'publication.json'
        svc.store.put(key, json_bytes(marker))
        before = {name: svc.store.get(name) for name in svc.store.list('')}
        operation = {'kind': 'publish', 'phase': 'installing', 'prefix': prefix}
        with pytest.raises(AttributeError) as caught:
            svc._execute('operation', operation)
        assert str(caught.value) == f"'{type(marker).__name__}' object has no attribute 'get'"
        assert {name: svc.store.get(name) for name in svc.store.list('')} == before
        assert svc.db.operations() == {}
    finally:
        svc.db.close()
