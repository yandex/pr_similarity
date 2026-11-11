"""Pending deletions do not claim the previous generation's confirmed backup."""

import json
from dataclasses import replace
from pathlib import Path

from test_local_service import service
from test_service import publish
from viewer.local_checkpoint import CheckpointRepository, restore
from viewer.local_service import LocalViewerService
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore


def test_checkpoint_accepts_pending_tombstone_without_old_backup_binding(tmp_path: Path) -> None:
    """Checkpoint accounts and deletion intent before the new tombstone reaches storage."""
    remote = MemoryStore()
    svc: LocalViewerService = service(tmp_path, remote)
    try:
        report, _, _ = publish(svc, tmp_path)
        rid = report['run']['id']
        svc.activate_backup(rid)
        assert svc.backups.run_once()
        checkpoints = CheckpointRepository(svc.config, svc.db, svc.repository, svc.bundles)
        checkpoints.create()
        svc.delete(rid)
        deleted = svc.repository.get(rid, include_deleted=True)
        assert deleted is not None and deleted.deleted and deleted.generation == 2
        assert deleted.backup_manifest_key is None
        assert deleted.backup_metadata_revision == 0
        assert deleted.backup_valid is False
        manifest = json.loads(remote.get(checkpoints.create()))
        assert not any(row['run_id'] == rid and row['generation'] == 2 for row in manifest['dependencies'])
        assert svc.backups.run_once()
        deleted = svc.repository.get(rid, include_deleted=True)
        assert deleted is not None and deleted.backup_state == 'backed_up' and deleted.backup_valid
        assert deleted.backup_metadata_revision == 1
        assert deleted.backup_manifest_key == f'bundle-index/v1/alice/{rid}/2/1.json'
        manifest = json.loads(remote.get(checkpoints.create()))
        assert any(row['run_id'] == rid and row['generation'] == 2 for row in manifest['dependencies'])
    finally:
        svc.close()
        svc.db.close()


def test_restore_refresh_resumes_pending_delete_but_not_lost_archive(tmp_path: Path) -> None:
    """Resume a durable deletion while leaving an unrelated missing payload blocked."""
    remote = MemoryStore()
    original: LocalViewerService = service(tmp_path, remote)
    restored: LocalViewerService | None = None
    try:
        report, _, _ = publish(original, tmp_path)
        rid = report['run']['id']
        other, _, _ = publish(original, tmp_path, run_id='22222222-2222-4222-8222-222222222222')
        other_id = other['run']['id']
        original.activate_backup(rid)
        original.activate_backup(other_id)
        assert original.backups.run_once() and original.backups.run_once()
        original.delete(rid)
        publish(original, tmp_path, run_id=other_id, score=0.25)
        key = CheckpointRepository(original.config, original.db, original.repository, original.bundles).create()
        target = tmp_path / 'restored'
        restore(original.config, remote, key, target)
        restored = LocalViewerService(replace(original.config, runtime=target), remote, Database(target))
        assert restored.repository.claim_backup('before-reconciliation') is None
        assert restored.refresh()['status'] == 'succeeded'
        assert restored.repository.restore_reconciliation()['status'] == 'reconciled'
        assert restored.backups.run_once()
        deleted = restored.repository.get(rid, include_deleted=True)
        assert deleted is not None and deleted.deleted and deleted.backup_state == 'backed_up'
        descriptor = restored.bundles.read_bundle(restored._identity(deleted))
        assert descriptor.tombstone and descriptor.identity.generation == 2
        lost = restored.repository.get(other_id)
        assert lost is not None and lost.cache_state == 'unavailable' and lost.backup_state == 'blocked'
        assert restored.backups.run_once() is False
    finally:
        if restored is not None:
            restored.close()
            restored.db.close()
        original.close()
        original.db.close()
