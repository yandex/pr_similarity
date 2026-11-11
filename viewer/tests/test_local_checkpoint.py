"""Closed account checkpoints restore without the source host filesystem."""

import fcntl
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
import viewer.local_checkpoint as module
from local_test_support import FaultStore, build_bundle_fixture
from viewer.bundles import BundleRepository
from viewer.cli import administer
from viewer.errors import StorageError, ViewerError
from viewer.local_checkpoint import CheckpointRepository, restore
from viewer.local_files import LocalFiles
from viewer.local_repository import LocalRecord, LocalRepository
from viewer.local_retention import LocalRetention
from viewer.persistence.database import Database
from viewer.util import digest

type CheckpointFixture = tuple[
    CheckpointRepository, BundleRepository, FaultStore, Database, LocalRepository, LocalRecord
]


@pytest.fixture
def checkpoint(tmp_path: Path) -> CheckpointFixture:
    """Prepare an account checkpoint with one confirmed publication and receipt."""
    bundles, store, identity, imported, marker, metadata, entry = build_bundle_fixture(tmp_path)
    descriptor = bundles.publish_bundle(identity, imported, marker, metadata)
    db = Database(tmp_path / 'accounts')
    db.create_user('alice', 'password')
    repository = LocalRepository(db)
    row = LocalRecord(identity.run_id, 'alice', 1, 1, entry, metadata, str(imported.directory))
    repository.commit(
        row,
        expected_generation=None,
        actor='alice',
        idempotency_key='receipt',
        receipt={'run_id': identity.run_id},
        activate_backup=True,
    )
    claimed_job = repository.claim_backup('worker')
    assert claimed_job is not None
    repository.finish_backup(claimed_job, manifest_key=descriptor.manifest_key)
    checkpoints = CheckpointRepository(bundles.config, db, repository, bundles)
    return checkpoints, bundles, store, db, repository, row


def test_restore_preserves_accounts_receipts_without_source_files(
    checkpoint: CheckpointFixture, tmp_path: Path
) -> None:
    """Checkpoint restoration retains accounts and receipts after source files disappear."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    db.close()
    shutil.rmtree(tmp_path / 'accounts')
    shutil.rmtree(tmp_path / 'source')
    (tmp_path / 'run.tar.gz').unlink()
    target = tmp_path / 'restored'
    result = restore(bundles.config, store, key, target)
    assert result.lost_runs == []
    reopened = Database(target)
    assert reopened.authenticate('alice', 'password')['login'] == 'alice'
    saved_receipt = reopened.receipt('alice', 'receipt')
    assert saved_receipt is not None
    assert saved_receipt['receipt']['run_id'] == row.run_id
    restored = LocalRepository(reopened)
    stored_record = restored.get(row.run_id)
    assert stored_record is not None
    assert stored_record.local_path is None
    stored_record = restored.get(row.run_id)
    assert stored_record is not None
    assert stored_record.cache_state == 'cold'
    stored_record = restored.get(row.run_id)
    assert stored_record is not None
    assert stored_record.backup_manifest_key
    assert row.run_id in restored.load_snapshot().runs


def test_lost_new_head_is_quarantined_without_older_fallback(checkpoint: CheckpointFixture, tmp_path: Path) -> None:
    """Restore quarantines a missing current generation instead of exposing an older version."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    repository.commit(replace(row, generation=2), expected_generation=1, actor='alice')
    result = restore(bundles.config, store, checkpoints.create(), tmp_path / 'restored')
    assert result.lost_runs == [row.run_id]
    restored = LocalRepository(Database(result.runtime))
    stored_record = restored.get(row.run_id)
    assert stored_record is not None
    assert stored_record.generation == 2
    stored_record = restored.get(row.run_id)
    assert stored_record is not None
    assert stored_record.cache_state == 'unavailable'
    assert row.run_id not in restored.load_snapshot().runs
    assert row.run_id in restored.load_snapshot().quarantined
    assert len(restored.versions()) == 2
    restored.recover_jobs()
    assert restored.claim_backup('worker') is None


def test_partial_checkpoint_upload_has_no_closing_manifest(checkpoint: CheckpointFixture) -> None:
    """A failed checkpoint upload never creates a readable closing manifest."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    store.fail = 'viewer.sqlite'
    with pytest.raises(StorageError):
        checkpoints.create()
    assert not list(store.list('checkpoints/v1/'))


@pytest.mark.parametrize('kind', ['snapshot', 'manifest', 'dependency'])
def test_corruption_refuses_target(checkpoint: CheckpointFixture, tmp_path: Path, kind: str) -> None:
    """Corrupt checkpoint components prevent installation of the restore target."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    manifest = json.loads(store.get(key))
    if kind == 'manifest':
        store.objects[key] = b'{"version":999}'
    elif kind == 'snapshot':
        store.objects[manifest['database']['key']] = b'bad db'
    else:
        store.objects[next(key for key in store.objects if key.endswith('archive.tar.gz'))] = b'bad archive'
    target = tmp_path / 'restored'
    with pytest.raises(ViewerError):
        restore(bundles.config, store, key, target)
    assert not target.exists()


def test_existing_and_symlink_targets_refused(checkpoint: CheckpointFixture, tmp_path: Path) -> None:
    """Restore cannot overwrite existing directories or follow symlink targets."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    existing = tmp_path / 'existing'
    existing.mkdir()
    for target in [existing, tmp_path / 'link']:
        if target.name == 'link':
            target.symlink_to(existing, target_is_directory=True)
        with pytest.raises(ViewerError):
            restore(bundles.config, store, key, target)
    link_parent = tmp_path / 'link-parent'
    link_parent.symlink_to(existing, target_is_directory=True)
    with pytest.raises(ViewerError):
        restore(bundles.config, store, key, link_parent / 'new-runtime')
    assert not list(existing.iterdir())


def test_missing_declared_dependency_refused(checkpoint: CheckpointFixture, tmp_path: Path) -> None:
    """Restore rejects a checkpoint whose declared bundle dependency is missing."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    manifest = json.loads(store.get(key))
    manifest['dependencies'] = []
    manifest['unavailable'] = [{'run_id': row.run_id, 'generation': 1, 'archive_sha256': row.archive_sha256}]
    store.objects[key] = json.dumps(manifest).encode()
    with pytest.raises(ViewerError):
        restore(bundles.config, store, key, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_checkpoint_refuses_corrupt_confirmed_archive(checkpoint: CheckpointFixture) -> None:
    """Checkpoint creation verifies confirmed archive bytes before closing its manifest."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    store.objects[next(key for key in store.objects if key.endswith('archive.tar.gz'))] = b'corrupt'
    with pytest.raises(ViewerError):
        checkpoints.create()
    assert not list(store.list('checkpoints/v1/'))


def test_partial_install_cleans_only_owned_target(
    checkpoint: CheckpointFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed installation removes only the restore target owned by that attempt."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    target = tmp_path / 'restored'
    preserved = tmp_path / 'unrelated'
    preserved.write_text('keep')

    def fail_link(source: Path, target: Path) -> Never:
        """Interrupt the atomic target installation without touching unrelated files."""
        raise OSError('injected link failure')

    monkeypatch.setattr(module.os, 'link', fail_link)
    with pytest.raises(OSError, match='injected'):
        restore(bundles.config, store, key, target)
    assert not target.exists()
    assert preserved.read_text() == 'keep'


def test_restore_rejects_newer_database_schema(checkpoint: CheckpointFixture, tmp_path: Path) -> None:
    """Restore refuses a database schema newer than the reader supports."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    manifest = json.loads(store.get(key))
    path = tmp_path / 'newer.sqlite'
    path.write_bytes(store.get(manifest['database']['key']))
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA user_version=4')
    connection.close()
    payload = path.read_bytes()
    store.objects[manifest['database']['key']] = payload
    manifest['database'].update(size=len(payload), sha256=digest(payload))
    store.objects[key] = json.dumps(manifest).encode()
    with pytest.raises(ViewerError):
        restore(bundles.config, store, key, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_metadata_only_pending_restore_keeps_latest_metadata(checkpoint: CheckpointFixture, tmp_path: Path) -> None:
    """Pending metadata survives restore while confirmed archive bytes remain reusable."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    metadata = {**row.metadata, 'display_name': 'Latest name'}
    repository.mutate_metadata(row.run_id, metadata, expected_generation=1, expected_metadata_revision=1)
    result = restore(bundles.config, store, checkpoints.create(), tmp_path / 'restored')
    assert result.lost_runs == []
    restored = LocalRepository(Database(result.runtime))
    current = restored.get(row.run_id)
    assert current is not None
    assert current.metadata['display_name'] == 'Latest name'
    assert current.metadata_revision == 2
    assert current.backup_metadata_revision == 1
    assert current.cache_state == 'cold'
    restored.recover_jobs()
    assert restored.claim_backup('worker') is None


def test_lost_head_preserves_saved_basket_settings(checkpoint: CheckpointFixture, tmp_path: Path) -> None:
    """Quarantining a lost publication does not discard saved basket presentation settings."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    basket = row.entry['marker']['basket_sha256']
    repository.save_settings(order=[basket], display={basket: {'hidden': True, 'collapsed': True}})
    repository.commit(replace(row, generation=2), expected_generation=1, actor='alice')
    result = restore(bundles.config, store, checkpoints.create(), tmp_path / 'restored')
    snapshot = LocalRepository(Database(result.runtime)).load_snapshot()
    assert snapshot.order == []
    assert snapshot.persisted_order == [basket]
    assert snapshot.persisted_display[basket] == {'hidden': True, 'collapsed': True}


def test_offline_restore_cli_works_without_source_runtime(
    checkpoint: CheckpointFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore command uses checkpoint data without the original runtime directory."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    target = tmp_path / 'cli-restored'
    assert not bundles.config.runtime.exists()
    monkeypatch.setenv('PRSIM_VIEWER_CHECKPOINT_KEY', key)
    monkeypatch.setenv('PRSIM_VIEWER_TARGET_RUNTIME', str(target))
    monkeypatch.setattr('viewer.cli.MockFileStore', lambda path: store)
    result = administer('admin-checkpoint-restore', bundles.config)
    assert result == {'runtime': str(target), 'restored_runs': [row.run_id], 'lost_runs': []}
    assert not bundles.config.runtime.exists()


def test_restore_cli_locks_existing_runtime(
    checkpoint: CheckpointFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore command refuses to operate while the target runtime is locked."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    bundles.config.runtime.mkdir()
    monkeypatch.setenv('PRSIM_VIEWER_CHECKPOINT_KEY', key)
    monkeypatch.setenv('PRSIM_VIEWER_TARGET_RUNTIME', str(tmp_path / 'restored'))
    with (bundles.config.runtime / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ViewerError, match='Stop the viewer'):
            administer('admin-checkpoint-restore', bundles.config)
    assert not (tmp_path / 'restored').exists()


def test_closed_checkpoint_remains_restorable_after_local_history_retention(
    checkpoint: CheckpointFixture, tmp_path: Path
) -> None:
    """Local history pruning does not invalidate an already closed remote checkpoint."""
    checkpoints, bundles, store, db, repository, row = checkpoint
    key = checkpoints.create()
    remote_before = dict(store.objects)
    repository.update_local_state(row.run_id, generation=1, local_path=None, cache_state='absent', cache_bytes=0)
    repository.commit(replace(row, generation=2, local_path=None), expected_generation=1, actor='alice')
    current_time = db.clock()
    db.clock = lambda: current_time + bundles.config.version_retention_seconds + 1
    result = LocalRetention(bundles.config, repository, LocalFiles(bundles.config)).run()
    assert result['removed_versions'] == 1
    assert [record.generation for record in repository.versions()] == [2]
    assert store.objects == remote_before
    restored = restore(bundles.config, store, key, tmp_path / 'after-retention')
    assert restored.lost_runs == []
    restored_record = LocalRepository(Database(restored.runtime)).get(row.run_id)
    assert restored_record is not None
    assert restored_record.generation == 1
