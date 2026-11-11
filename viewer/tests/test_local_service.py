"""Local commits, offline restart and exact cold restoration."""

import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest
import viewer.local_service as module
from helpers import archive_fixture
from test_service import publish
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_repository import LocalRecord
from viewer.local_service import LocalViewerService
from viewer.models import Report
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore, Store
from viewer.util import digest


def require_record(svc: LocalViewerService, rid: str) -> LocalRecord:
    """Return a fixture publication after asserting that its durable record exists."""
    record = svc.repository.get(rid)
    assert record is not None
    return record


def require_operation(svc: LocalViewerService, actor: str, key: str) -> dict:
    """Return the fixture operation after checking its expected durable admission."""
    operation = svc.repository.operation(actor, key)
    assert operation is not None
    return operation


def service(tmp_path: Path, store: Store | None = None, clock: Callable[[], float] | None = None) -> LocalViewerService:
    """Create an isolated local viewer with its fixture owner and cache TTL."""
    config = Config(
        runtime=tmp_path / 'runtime',
        storage='mock',
        publication_storage='local',
        disk_reserve_bytes=0,
        cache_idle_ttl_seconds=10,
    )
    db = Database(config.runtime, clock=clock) if clock else Database(config.runtime)
    if db.actor('alice') is None:
        db.create_user('alice', 'secret')
    return LocalViewerService(config, store or MemoryStore(), db)


def test_local_publication_returns_before_remote_backup_and_survives_restart(tmp_path: Path) -> None:
    """Verify local commit and restart succeed before any remote backup writes."""
    remote = MemoryStore()
    svc = service(tmp_path, remote)
    report, receipt, imported = publish(svc, tmp_path)
    assert receipt['durability'] == 'local_durable'
    assert receipt['author_login'] == 'alice'
    assert remote.objects == {}
    rid = report['run']['id']
    assert svc.run(rid)['publication_sha256'] == imported.archive_sha256
    assert svc.close()
    svc.db.close()
    restarted = service(tmp_path, remote)
    assert restarted.run(rid)['publication_sha256'] == imported.archive_sha256
    assert remote.objects == {}
    restarted.close()
    restarted.db.close()


def test_backup_ttl_and_cold_read_preserve_exact_bytes(tmp_path: Path) -> None:
    """Restore the original backed-up archive after its idle cache entry expires."""
    now = [100.0]
    remote = MemoryStore()
    svc = service(tmp_path, remote, lambda: now[0])
    report, _, imported = publish(svc, tmp_path)
    rid = report['run']['id']
    svc.activate_backup(rid)
    assert svc.backups.run_once()
    assert require_record(svc, rid).backup_state == 'backed_up'
    now[0] = 111.0
    assert svc.cleanup_cache() == 1
    assert require_record(svc, rid).local_path is None
    lease = svc.acquire_read(rid)
    with svc.bind_read(lease):
        assert svc.full_report(rid).run.id == rid
        with svc.download(rid, 'alice') as stream:
            assert stream.read() == imported.archive.read_bytes()
    lease.close(True)
    assert require_record(svc, rid).last_access == 111
    svc.close()
    svc.db.close()


def test_report_cache_reuses_parse_and_keeps_pinned_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a pinned report stable while newer publications enter the bounded parse cache."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    lease = svc.acquire_read(rid)
    original = module.validate_report
    calls = []

    def validate(value: dict) -> Report:
        """Pause validation until unrelated HTTP operations have been checked."""
        calls.append(value)
        return original(value)

    monkeypatch.setattr(module, 'validate_report', validate)
    with svc.bind_read(lease):
        old = svc.full_report(rid)
        assert svc.full_report(rid) is old
    assert len(calls) == 1
    publish(svc, tmp_path, run_id=rid, score=0.5)
    with svc.bind_read(lease):
        assert svc.full_report(rid) is old
    current = svc.full_report(rid)
    assert current.tasks[0].scoring.Mf_mlp == 0.5
    assert old.tasks[0].scoring.Mf_mlp == 1
    assert len(calls) == 2
    lease.close()
    svc.close()
    svc.db.close()


def test_report_cache_evicts_by_entry_budget(tmp_path: Path) -> None:
    """Evict the oldest parsed report when the configured entry budget is exceeded."""
    svc = service(tmp_path)
    svc.config = replace(svc.config, report_cache_entries=1)
    first, _, _ = publish(svc, tmp_path)
    second, _, _ = publish(svc, tmp_path, run_id='22222222-2222-4222-8222-222222222222')
    rid = first['run']['id']
    original = svc.full_report(rid)
    svc.full_report(second['run']['id'])
    assert len(svc._reports) == 1
    assert svc.full_report(rid) is not original
    assert svc._report_bytes <= svc.config.report_cache_bytes
    svc.close()
    svc.db.close()


def test_restart_keeps_accepted_overwrite_busy_until_recovery(tmp_path: Path) -> None:
    """Reject readers and competing writers until the accepted overwrite is recovered."""
    remote = MemoryStore()
    svc = service(tmp_path, remote)
    report, _, original = publish(svc, tmp_path)
    rid = report['run']['id']
    archive = tmp_path / 'replacement.tar.gz'
    _, manifest = archive_fixture(archive, run_id=rid)
    archive_sha256 = digest(archive.read_bytes())
    key = 'accepted-overwrite'
    svc.admit_publication('alice', key, rid, archive_sha256, manifest['report_sha256'], overwrite=True)
    svc.publication_phase('alice', key, 'extracting', archive_path=str(archive))
    assert svc.close()
    svc.db.close()

    restarted = service(tmp_path, remote)
    try:
        assert restarted.busy_runs() == {rid}
        with pytest.raises(ViewerError) as read_error:
            restarted.run(rid)
        assert read_error.value.code == 'run_updating'
        with pytest.raises(ViewerError) as write_error:
            restarted.admit_publication(
                'alice',
                'competing',
                rid,
                'a' * 64,
                'b' * 64,
                overwrite=True,
            )
        assert write_error.value.code == 'run_updating'
        restarted._recover_uploads()
        assert restarted.busy_runs() == set()
        assert restarted.run(rid)['publication_sha256'] == archive_sha256
        assert restarted.run(rid)['publication_sha256'] != original.archive_sha256
    finally:
        restarted.close()
        restarted.db.close()


def test_durable_upload_fsyncs_ancestors_and_rejection_removes_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist accepted upload directories and remove their bytes after rejection."""
    svc = service(tmp_path)
    archive = tmp_path / 'uploaded.tar.gz'
    archive.write_bytes(b'accepted exact bytes')
    key = 'upload-boundary'
    rid = '22222222-2222-4222-8222-222222222222'
    svc.admit_publication('alice', key, rid, digest(archive.read_bytes()), 'b' * 64)
    synced = []
    original_fsync = module.fsync_ancestors

    def fsync(path: Path, boundary: Path) -> None:
        """Record durable ancestor synchronization before invoking the real filesystem barrier."""
        synced.append((Path(path), Path(boundary)))
        original_fsync(path, boundary)

    monkeypatch.setattr(module, 'fsync_ancestors', fsync)
    try:
        svc.publication_phase('alice', key, 'extracting', archive_path=str(archive))
        operation = require_operation(svc, 'alice', key)
        saved = Path(operation['archive_path'])
        assert synced == [(saved.parent, svc.config.runtime)]
        svc.publication_failed('alice', key, ViewerError('invalid_archive', 'Rejected archive.'))
        assert require_operation(svc, 'alice', key)['phase'] == 'rejected'
        assert not saved.exists()
    finally:
        svc.close()
        svc.db.close()


def test_retry_waits_for_rejected_upload_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a rejected operation busy until its staged bytes have been removed."""
    svc = service(tmp_path)
    archive = tmp_path / 'retry.tar.gz'
    archive.write_bytes(b'accepted exact bytes')
    key = 'retry-cleanup'
    rid = '22222222-2222-4222-8222-222222222222'
    archive_sha256 = digest(archive.read_bytes())
    svc.admit_publication('alice', key, rid, archive_sha256, 'b' * 64)
    svc.publication_phase('alice', key, 'extracting', archive_path=str(archive))
    entered, release = threading.Event(), threading.Event()
    original_cleanup = svc._cleanup_upload

    def blocked_cleanup(operation: dict) -> None:
        """Hold rejected-upload cleanup while a competing admission is attempted."""
        entered.set()
        assert release.wait(15)
        original_cleanup(operation)

    monkeypatch.setattr(svc, '_cleanup_upload', blocked_cleanup)
    worker = threading.Thread(
        target=svc.publication_failed,
        args=('alice', key, ViewerError('invalid_archive', 'Rejected archive.')),
    )
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ViewerError) as error:
            svc.admit_publication('alice', key, rid, archive_sha256, 'b' * 64)
        assert error.value.code == 'publication_in_progress'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert svc.admit_publication('alice', key, rid, archive_sha256, 'b' * 64)['phase'] == 'receiving'
    svc.publication_failed('alice', key, ViewerError('invalid_archive', 'Rejected archive.'))
    svc.close()
    svc.db.close()


def test_failed_commit_removes_installed_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove a newly installed version when durable catalogue commit fails."""
    svc = service(tmp_path)

    def fail(*args: object, **kwargs: object) -> NoReturn:
        """Inject a durable commit failure before catalogue state becomes visible."""
        raise OSError('injected commit failure')

    monkeypatch.setattr(svc.repository, 'commit', fail)
    try:
        with pytest.raises(OSError, match='injected commit failure'):
            publish(svc, tmp_path)
        assert svc.repository.records(include_deleted=True) == []
        assert not list(svc.files.root.rglob('archive.tar.gz'))
    finally:
        svc.close()
        svc.db.close()


def test_failed_commit_preserves_rejection_when_first_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the rejected operation and retry removal after a failed first rollback."""
    svc = service(tmp_path)
    original_evict = svc.files.evict
    attempts = []

    def fail_commit(*args: object, **kwargs: object) -> NoReturn:
        """Reject catalogue commit to exercise rollback of installed files."""
        raise OSError('injected commit failure')

    def fail_first(path: str | Path) -> bool:
        """Fail the first eviction and allow the retained orphan to be cleaned later."""
        attempts.append(Path(path))
        if len(attempts) == 1:
            raise OSError('injected rollback failure')
        return original_evict(path)

    monkeypatch.setattr(svc.repository, 'commit', fail_commit)
    monkeypatch.setattr(svc.files, 'evict', fail_first)
    try:
        with pytest.raises(OSError, match='injected commit failure'):
            publish(svc, tmp_path, key='failed-commit')
        operation = require_operation(svc, 'alice', 'failed-commit')
        assert operation['phase'] == 'rejected'
        assert svc.busy_runs() == set()
        assert svc._orphan_paths == {(operation['run_id'], attempts[0])}
        svc._cleanup_orphan_paths()
        assert svc._orphan_paths == set()
        assert not list(svc.files.root.rglob('archive.tar.gz'))
    finally:
        svc.close()
        svc.db.close()


def test_restart_cleans_durable_rejected_installed_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recover and remove a rejected installed path after process restart."""
    svc = service(tmp_path)

    def fail_commit(*args: object, **kwargs: object) -> NoReturn:
        """Reject catalogue commit to exercise rollback of installed files."""
        raise OSError('injected commit failure')

    def fail_evict(path: str | Path) -> NoReturn:
        """Reject rollback eviction so its orphan path must survive restart."""
        raise OSError('injected rollback failure')

    monkeypatch.setattr(svc.repository, 'commit', fail_commit)
    monkeypatch.setattr(svc.files, 'evict', fail_evict)
    with pytest.raises(OSError, match='injected commit failure'):
        publish(svc, tmp_path, key='durable-orphan')
    operation = require_operation(svc, 'alice', 'durable-orphan')
    installed = Path(operation['installed_path'])
    assert operation['phase'] == 'rejected' and installed.is_dir()
    assert svc.close()
    svc.db.close()

    restarted = service(tmp_path)
    try:
        restarted._cleanup_terminal_uploads()
        assert not installed.exists()
        assert require_operation(restarted, 'alice', 'durable-orphan')['phase'] == 'rejected'
    finally:
        restarted.close()
        restarted.db.close()


def test_refresh_persists_absent_remote_tombstone_and_owner_floor(tmp_path: Path) -> None:
    """Import remote deletion intent without losing its original ownership binding."""
    remote = MemoryStore()
    source = service(tmp_path / 'source', remote)
    report, receipt, _ = publish(source, tmp_path / 'source')
    rid = report['run']['id']
    source.activate_backup(rid)
    assert source.backups.run_once()
    source.delete(
        rid,
        actor={'login': 'alice', 'role': 'user'},
        publication=receipt['archive_sha256'],
    )
    assert source.backups.run_once()
    source.close()
    source.db.close()

    restored = service(tmp_path / 'restored', remote)
    restored.db.create_user('bob', 'secret')
    try:
        result = restored.refresh()
        tombstone = restored.repository.get(rid, include_deleted=True)
        assert result['status'] == 'succeeded'
        assert result['updated'] == 1
        assert tombstone is not None and tombstone.deleted
        assert tombstone.generation == 2 and tombstone.owner == 'alice'
        owner = restored.db.owner(rid)
        assert owner is not None and owner['login'] == 'alice'
        with pytest.raises(ViewerError) as error:
            restored.admit_publication('bob', 'foreign-reuse', rid, 'a' * 64, 'b' * 64)
        assert error.value.code == 'foreign_run'
    finally:
        restored.close()
        restored.db.close()


def test_old_reader_completion_does_not_restore_refreshed_path(tmp_path: Path) -> None:
    """Touch access time without restoring an obsolete path after a reader finishes."""
    now = [100.0]
    svc = service(tmp_path, clock=lambda: now[0])
    report, _, imported = publish(svc, tmp_path)
    rid = report['run']['id']
    original = require_record(svc, rid)
    lease = svc.acquire_read(rid)
    identity = svc._identity(original)
    marker = svc.files.marker(identity)
    refreshed = svc.files.install(imported, identity, marker, replace_existing=True)
    assert original.local_path is not None
    assert refreshed != Path(original.local_path)
    assert svc.repository.update_local_state(
        rid,
        generation=original.generation,
        local_path=str(refreshed),
        cache_state='ready',
        cache_bytes=svc.files.size(refreshed),
    )
    now[0] = 200.0
    lease.close(True)
    current = require_record(svc, rid)
    assert current.local_path == str(refreshed)
    assert current.last_access == 200.0
    svc.close()
    svc.db.close()
