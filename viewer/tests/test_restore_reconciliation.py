"""Checkpoint restore fences mutations until current remote revisions are audited."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from helpers import archive_fixture
from test_local_service import service
from test_service import publish
from viewer.bundles import RESERVATIONS, BundleRegistrySnapshot
from viewer.errors import ViewerError
from viewer.local_checkpoint import CheckpointRepository, restore
from viewer.local_service import LocalViewerService
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore, StoredBody
from viewer.validation.archive import validate_archive


@contextmanager
def restored_service(
    tmp_path: Path, *, metadata_only: bool = False
) -> Iterator[tuple[LocalViewerService, LocalViewerService, MemoryStore, str]]:
    """Restore a checkpoint whose remote generation or metadata has advanced."""
    remote = MemoryStore()
    original: LocalViewerService = service(tmp_path, remote)
    report, _, _ = publish(original, tmp_path)
    rid = report['run']['id']
    original.activate_backup(rid)
    assert original.backups.run_once()
    if metadata_only:
        original.patch_metadata(rid, 'alice', {'display_name': 'checkpoint pending name'})
    assert original.refresh()['status'] == 'succeeded'
    original.repository.set_operation(
        'alice',
        'old-upload',
        'old-fingerprint',
        {'phase': 'receiving', 'run_id': rid, 'archive_path': str(tmp_path / 'old-upload.tar.gz')},
    )
    key = CheckpointRepository(original.config, original.db, original.repository, original.bundles).create()
    if metadata_only:
        assert original.backups.run_once()
        original.patch_metadata(rid, 'alice', {'display_name': 'newer remote name'})
    else:
        publish(original, tmp_path, run_id=rid, score=0.5)
        original.activate_backup(rid)
    assert original.backups.run_once()
    target = tmp_path / 'restored'
    restore(original.config, remote, key, target)
    config = replace(original.config, runtime=target, admin_token_file=target / 'admin-token')
    restored = LocalViewerService(config, remote, Database(target))
    try:
        yield original, restored, remote, rid
    finally:
        restored.close()
        restored.db.close()
        original.close()
        original.db.close()


def test_restored_old_generation_rejects_overwrite_until_full_refresh(tmp_path: Path) -> None:
    """A restored generation one cannot reuse remote generation two's identity."""
    with restored_service(tmp_path) as (_, restored, _, rid):
        assert restored.audit.status()['status'] == 'idle'
        old_upload = restored.repository.operation('alice', 'old-upload')
        assert old_upload is not None and old_upload['phase'] == 'rejected'
        assert 'archive_path' not in old_upload
        with pytest.raises(ViewerError) as error:
            publish(restored, tmp_path, run_id=rid, score=0.25)
        assert error.value.code == 'restore_reconciliation_required'
        assert restored.refresh()['status'] == 'succeeded'
        current = restored.repository.get(rid)
        assert current is not None and current.generation == 2
        publish(restored, tmp_path, run_id=rid, score=0.25)
        restored.activate_backup(rid)
        assert restored.backups.run_once()
        current = restored.repository.get(rid)
        assert current is not None and current.generation == 3 and current.backup_state == 'backed_up'


def test_restore_fences_metadata_delete_and_backup_then_reconciles_metadata(tmp_path: Path) -> None:
    """Same-generation metadata advances are reconciled before allocating a revision."""
    with restored_service(tmp_path, metadata_only=True) as (_, restored, _, rid):
        with pytest.raises(ViewerError, match='reconciliation'):
            restored.patch_metadata(rid, 'alice', {'display_name': 'premature'})
        with pytest.raises(ViewerError, match='reconciliation'):
            restored.delete(rid)
        restored.repository.recover_jobs()
        restored.activate_backup(rid)
        assert restored.repository.claim_backup('old-intent') is None
        assert restored.refresh()['status'] == 'succeeded'
        current = restored.repository.get(rid)
        assert current is not None and current.metadata_revision == 3
        assert current.metadata['display_name'] == 'newer remote name'
        restored.patch_metadata(rid, 'alice', {'display_name': 'after recovery'})
        assert restored.backups.run_once()
        current = restored.repository.get(rid)
        assert current is not None and current.metadata_revision == 4 and current.backup_state == 'backed_up'


def test_restore_reads_and_failed_reconciliation_survive_offline_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup and warm reads stay local while failed S3 reconciliation preserves the barrier."""
    with restored_service(tmp_path) as (_, restored, remote, rid):
        lease = restored.acquire_read(rid)
        with restored.bind_read(lease):
            assert restored.full_report(rid).run.id == rid
        lease.close()
        original_objects = dict(remote.objects)

        def unavailable(key: str) -> StoredBody:
            """Reject remote reads after the old checkpoint data has been warmed."""
            raise ViewerError('remote_offline', 'Remote storage is unavailable.', 503)

        monkeypatch.setattr(remote, 'open', unavailable)
        assert restored.refresh()['status'] == 'failed'
        restored.close()
        restored.db.close()
        restarted = LocalViewerService(restored.config, remote, Database(restored.config.runtime))
        try:
            assert restarted.startup['status'] == 'ready'
            assert restarted.run(rid)['run_id'] == rid
            lease = restarted.acquire_read(rid)
            with restarted.bind_read(lease):
                assert restarted.full_report(rid).run.id == rid
            lease.close()
            restarted.start_background()
            restarted._recover_uploads()
            assert not restarted.backups.run_once()
            with pytest.raises(ViewerError, match='reconciliation'):
                restarted.patch_metadata(rid, 'alice', {'display_name': 'still blocked'})
            assert remote.objects == original_objects
            assert restarted.repository.restore_reconciliation()['status'] == 'required'
        finally:
            restarted.close()
            restarted.db.close()


def test_changed_registry_keeps_barrier_until_another_complete_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry change between discovery and completion cannot unlock writes."""
    with restored_service(tmp_path) as (original, restored, _, rid):
        snapshot = restored.bundles.registry_snapshot
        reads = 0

        def advance_before_final_read() -> BundleRegistrySnapshot:
            """Advance remote metadata after the audit has read its frozen boundary."""
            nonlocal reads
            reads += 1
            if reads == 2:
                with pytest.raises(ViewerError, match='reconciliation'):
                    restored.patch_metadata(rid, 'alice', {'display_name': 'during final read'})
                original.patch_metadata(rid, 'alice', {'display_name': 'remote advanced during refresh'})
                assert original.backups.run_once()
            return snapshot()

        monkeypatch.setattr(restored.bundles, 'registry_snapshot', advance_before_final_read)
        assert restored.refresh()['status'] == 'partial'
        assert restored.repository.restore_reconciliation()['status'] == 'required'
        with pytest.raises(ViewerError, match='reconciliation'):
            restored.delete(rid)
        assert restored.refresh()['status'] == 'succeeded'
        assert restored.repository.restore_reconciliation()['status'] == 'reconciled'
        current = restored.repository.get(rid)
        assert current is not None and current.metadata_revision == 2


def test_corrupt_bundle_partial_refresh_does_not_unlock(tmp_path: Path) -> None:
    """A complete registry listing alone cannot stand in for verified bundles."""
    with restored_service(tmp_path) as (original, restored, remote, rid):
        current = original.repository.get(rid)
        assert current is not None
        identity = original._identity(current, confirmed=True)
        remote.objects[identity.archive_key] = b'corrupted'
        assert restored.refresh()['status'] == 'partial'
        assert restored.repository.restore_reconciliation()['status'] == 'required'
        assert restored.repository.claim_backup('retry') is None
        with pytest.raises(ViewerError, match='reconciliation'):
            restored.repository.mutate_metadata(
                rid, current.metadata, expected_generation=1, expected_metadata_revision=1
            )


def test_missing_remote_owner_can_be_provisioned_during_reconciliation(tmp_path: Path) -> None:
    """Administrators can restore a missing account before retrying the full audit."""
    with restored_service(tmp_path) as (original, restored, _, _):
        original.db.create_user('bob', 'secret')
        path = tmp_path / 'bob.tar.gz'
        report, _ = archive_fixture(path, run_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
        imported = validate_archive(path, tmp_path / 'bob-data', original.config.limits)
        original.publish(imported, 'bob', 'bob-publication')
        original.activate_backup(report['run']['id'])
        assert original.backups.run_once()
        assert restored.refresh()['status'] == 'partial'
        assert restored.repository.restore_reconciliation()['status'] == 'required'
        restored.db.create_user('bob', 'secret')
        assert restored.refresh()['status'] == 'succeeded'
        current = restored.repository.get(report['run']['id'])
        assert current is not None and current.owner == 'bob'


def test_final_registry_read_does_not_hold_catalogue_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The asynchronous recovery audit leaves catalogue reads free during final S3 I/O."""
    with restored_service(tmp_path) as (_, restored, _, rid):
        snapshot = restored.bundles.registry_snapshot
        entered, release = Event(), Event()
        reads = 0

        def paused_final_read() -> BundleRegistrySnapshot:
            """Pause only the final registry recheck, after archive verification."""
            nonlocal reads
            reads += 1
            if reads == 2:
                entered.set()
                assert release.wait(5)
            return snapshot()

        monkeypatch.setattr(restored.bundles, 'registry_snapshot', paused_final_read)
        restored.request_refresh()
        try:
            assert entered.wait(5)
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(restored.run, rid).result(timeout=1)['run_id'] == rid
            assert restored.repository.restore_reconciliation()['status'] == 'required'
        finally:
            release.set()
            worker = restored._audit_thread
            assert worker is not None
            worker.join(timeout=5)
        assert restored.repository.restore_reconciliation()['status'] == 'reconciled'


@pytest.mark.parametrize('kind', ['archive', 'metadata', 'tombstone'])
@pytest.mark.parametrize('boundary', ['metadata', 'register'])
def test_restore_skips_unregistered_backup_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, boundary: str
) -> None:
    """An interrupted remote backup cannot poison writes after restoring an older checkpoint."""
    remote = MemoryStore()
    original = service(tmp_path, remote)
    restored = None
    try:
        report, _, _ = publish(original, tmp_path)
        rid = report['run']['id']
        original.activate_backup(rid)
        assert original.backups.run_once()
        key = CheckpointRepository(original.config, original.db, original.repository, original.bundles).create()
        if kind == 'archive':
            publish(original, tmp_path, run_id=rid, score=0.5)
            original.activate_backup(rid)
        elif kind == 'metadata':
            original.patch_metadata(rid, 'alice', {'display_name': 'interrupted remote name'})
        else:
            original.delete(rid)
        current = original.repository.get(rid, include_deleted=True)
        assert current is not None
        identity = original._identity(current)

        def fail(*args: object, **kwargs: object) -> None:
            """Interrupt the backup after reservation but before registration."""
            raise ViewerError('remote_offline', 'Remote storage is unavailable.', 503)

        with monkeypatch.context() as patch:
            patch.setattr(original.bundles, '_register' if boundary == 'register' else '_immutable', fail)
            assert original.backups.run_once()
        if boundary == 'register':
            assert identity.closing_key in remote.objects
        remote_before = dict(remote.objects)
        target = tmp_path / 'restored'
        restore(original.config, remote, key, target)
        config = replace(original.config, runtime=target, admin_token_file=target / 'admin-token')
        restored = LocalViewerService(config, remote, Database(target))
        assert restored.refresh()['status'] == 'succeeded'
        assert restored.repository.restore_reconciliation()['status'] == 'reconciled'
        # Reopen after reconciliation to prove the allocation floor is durable.
        restored.close()
        restored.db.close()
        restored = LocalViewerService(config, remote, Database(target))
        if kind == 'metadata':
            restored.patch_metadata(rid, 'alice', {'display_name': 'new name after restore'})
        elif kind == 'tombstone':
            restored.delete(rid)
        else:
            publish(restored, tmp_path, run_id=rid, score=0.25)
            restored.activate_backup(rid)
        assert restored.backups.run_once()
        current = restored.repository.get(rid, include_deleted=True)
        assert current is not None and current.backup_state == 'backed_up'
        assert (current.generation, current.metadata_revision) > (identity.generation, identity.metadata_revision)
        for object_key, data in remote_before.items():
            if object_key.startswith(('bundle-data/', 'bundle-index/')):
                assert remote.objects[object_key] == data
    finally:
        if restored is not None:
            restored.close()
            restored.db.close()
        original.close()
        original.db.close()


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'changed'])
def test_restore_reservation_failure_keeps_write_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Missing, malformed or concurrently changed allocation floors cannot unlock writes."""
    with restored_service(tmp_path) as (original, restored, remote, rid):
        if damage == 'missing':
            remote.objects.pop(RESERVATIONS)
        elif damage == 'corrupt':
            remote.objects[RESERVATIONS] = b'{}'
        else:
            original_read = restored.bundles.reservation_floors
            reads = 0

            def changed() -> tuple[bytes, dict[str, tuple[int, int]]]:
                """Reserve another revision between the final reconciliation reads."""
                nonlocal reads
                reads += 1
                if reads == 2:
                    current = original.repository.get(rid)
                    assert current is not None
                    original.bundles._reserve(replace(original._identity(current), metadata_revision=7))
                return original_read()

            monkeypatch.setattr(restored.bundles, 'reservation_floors', changed)
        assert restored.refresh()['status'] == 'partial'
        assert restored.repository.restore_reconciliation()['status'] == 'required'
        with pytest.raises(ViewerError, match='reconciliation'):
            restored.patch_metadata(rid, 'alice', {'display_name': 'blocked'})
