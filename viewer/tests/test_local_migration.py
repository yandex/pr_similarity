"""Offline legacy import preserves frozen archives and existing account state."""

import fcntl
import json
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from local_test_support import build_legacy_service, publish_fixture
from viewer.cli import administer
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_migration import import_legacy
from viewer.local_repository import LocalRepository
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore, Store, StoredBody
from viewer.service import ViewerService


class CountingStore(MemoryStore):
    """Track original-archive reads and inject a selected migration read failure."""

    def __init__(self) -> None:
        """Initialize archive read counters and disable injected read failures."""
        super().__init__()
        self.archives: list[str] = []
        self.fail_archive: str | None = None

    def open(self, key: str) -> StoredBody:
        """Count archive downloads and fail the configured archive read."""
        if key.endswith('/archive.tar.gz'):
            self.archives.append(key)
            if self.fail_archive == key:
                raise ViewerError('injected', 'injected archive failure', 503)
        return super().open(key)


type LegacyFixture = tuple[ViewerService, CountingStore, Config, str, dict]


@pytest.fixture
def legacy(tmp_path: Path) -> LegacyFixture:
    """Prepare a legacy publication with custom metadata and an existing owner."""
    store = CountingStore()
    svc = build_legacy_service(tmp_path, store)
    report, receipt, imported = publish_fixture(svc, tmp_path)
    rid = report['run']['id']
    svc.patch_metadata(rid, 'alice', {'display_name': 'Custom name', 'tags': ['custom']})
    svc.set_pinned(rid, True)
    store.archives.clear()
    config = replace(svc.config, publication_storage='local')
    return svc, store, config, rid, receipt


def test_import_preserves_disabled_owner_receipts_and_exact_archive(legacy: LegacyFixture) -> None:
    """Migration preserves disabled accounts, publication receipts and original archive bytes."""
    svc, store, config, rid, receipt = legacy
    svc.db.update_user('alice', enabled=False)
    before = svc.db.connection.execute('SELECT login,idem_key,fingerprint,data FROM receipts').fetchall()
    result = import_legacy(config, store, svc.db)
    assert result['status'] == 'completed'
    repository = LocalRepository(svc.db)
    row = repository.get(rid)
    assert row is not None
    assert row.owner == 'alice'
    assert row.metadata['display_name'] == 'Custom name'
    assert row.metadata['tags'] == ['custom'] and row.metadata['pinned']
    assert row.archive_sha256 == receipt['archive_sha256']
    assert svc.db.actor('alice') is None
    assert svc.db.connection.execute('SELECT login,idem_key,fingerprint,data FROM receipts').fetchall() == before
    assert store.archives == [f'runs/alice/{rid}/archive.tar.gz']
    assert repository.claim_backup('worker') is None
    assert import_legacy(config, store, svc.db)['status'] == 'completed'
    assert len(store.archives) == 1


def test_pending_legacy_journal_refuses_before_upgrade(legacy: LegacyFixture) -> None:
    """An unfinished legacy journal prevents migration before any schema upgrade."""
    svc, store, config, rid, receipt = legacy
    svc.db.save_operation('pending', {'phase': 'installing'})
    assert svc.db.connection.execute('PRAGMA user_version').fetchone()[0] == 2
    with pytest.raises(ViewerError, match='recovery'):
        import_legacy(config, store, svc.db)
    assert svc.db.connection.execute('PRAGMA user_version').fetchone()[0] == 2
    assert not store.archives


def test_failed_import_resumes_without_overwriting_local_metadata(legacy: LegacyFixture, tmp_path: Path) -> None:
    """Migration resume keeps local metadata already committed by a prior import attempt."""
    svc, store, config, rid, receipt = legacy
    second_report, _, _ = publish_fixture(svc, tmp_path, run_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
    second = second_report['run']['id']
    # Fail the later UUID in the frozen, sorted plan.
    failing = sorted((rid, second))[1]
    first = sorted((rid, second))[0]
    store.archives.clear()
    store.fail_archive = f'runs/alice/{failing}/archive.tar.gz'
    with pytest.raises(ViewerError, match='injected'):
        import_legacy(config, store, svc.db)
    repository = LocalRepository(svc.db)
    row = repository.get(first)
    assert row is not None
    repository.mutate_metadata(
        first, {**row.metadata, 'display_name': 'Local edit'}, expected_generation=1, expected_metadata_revision=1
    )
    store.fail_archive = None
    import_legacy(config, store, svc.db)
    stored_record = repository.get(first)
    assert stored_record is not None
    assert stored_record.metadata['display_name'] == 'Local edit'
    assert store.archives.count(f'runs/alice/{first}/archive.tar.gz') == 1
    saved_refresh = repository.load_refresh('legacy-import')
    assert saved_refresh is not None
    assert saved_refresh['status'] == 'completed'


def test_bad_original_archive_leaves_progress_incomplete(legacy: LegacyFixture) -> None:
    """A corrupt source archive keeps migration progress incomplete."""
    svc, store, config, rid, receipt = legacy
    store.objects[f'runs/alice/{rid}/archive.tar.gz'] = b'corrupt'
    with pytest.raises(ViewerError):
        import_legacy(config, store, svc.db)
    repo = LocalRepository(svc.db)
    assert repo.get(rid) is None
    saved_refresh = repo.load_refresh('legacy-import')
    assert saved_refresh is not None
    assert saved_refresh['status'] == 'failed'


def test_bounded_listing_fails_without_success_flag(legacy: LegacyFixture) -> None:
    """Migration does not mark completion after exceeding its listing budget."""
    svc, store, config, rid, receipt = legacy
    with pytest.raises(ViewerError):
        import_legacy(config, store, svc.db, max_objects=1)
    saved_refresh = LocalRepository(svc.db).load_refresh('legacy-import')
    assert saved_refresh is not None
    assert saved_refresh['status'] == 'failed'


def test_offline_cli_refuses_running_service_without_upgrading(
    legacy: LegacyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration command refuses a locked runtime before upgrading its database."""
    svc, store, config, rid, receipt = legacy
    with (config.runtime / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ViewerError, match='Stop the viewer'):
            administer('admin-local-import', config)
    assert svc.db.connection.execute('PRAGMA user_version').fetchone()[0] == 2


def test_cli_import_uses_same_database_and_no_http(legacy: LegacyFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline migration uses the existing local database without requiring HTTP."""
    svc, store, config, rid, receipt = legacy
    monkeypatch.setattr('viewer.cli.MockFileStore', lambda path: store)
    monkeypatch.setattr('viewer.cli._request', lambda *a, **kw: pytest.fail('offline command attempted HTTP'))
    result = administer('admin-local-import', config)
    assert result == {'status': 'completed', 'imported_runs': 1}
    imported_record = LocalRepository(svc.db).get(rid)
    assert imported_record is not None
    assert imported_record.owner == 'alice'


def test_cli_restore_requires_explicit_key_and_target(legacy: LegacyFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """The restore command requires both an explicit checkpoint key and target directory."""
    svc, store, config, rid, receipt = legacy
    monkeypatch.delenv('PRSIM_VIEWER_CHECKPOINT_KEY', raising=False)
    monkeypatch.delenv('PRSIM_VIEWER_TARGET_RUNTIME', raising=False)
    with pytest.raises(ViewerError, match='CHECKPOINT_KEY'):
        administer('admin-checkpoint-restore', config)


def test_migration_upgrade_records_pending_flag_before_service_construction(
    legacy: LegacyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration records durable pending state before constructing the legacy reader."""
    svc, store, config, rid, receipt = legacy

    def fail_constructor(
        config: Config, store: Store, database: Database, *, initialize: bool, allow_legacy_import: bool
    ) -> Never:
        """Stop service construction after the migration intent has been persisted."""
        raise OSError('injected initialization failure')

    monkeypatch.setattr('viewer.local_migration.LocalViewerService', fail_constructor)
    with pytest.raises(OSError, match='injected'):
        import_legacy(config, store, svc.db)
    assert svc.db.connection.execute('PRAGMA user_version').fetchone()[0] == 3
    saved_refresh = LocalRepository(svc.db).load_refresh('legacy-import')
    assert saved_refresh is not None
    assert saved_refresh['status'] == 'planning'


def test_import_refuses_missing_owner_without_creating_accounts(legacy: LegacyFixture) -> None:
    """Migration cannot create an owner account from publication metadata."""
    svc, store, config, rid, receipt = legacy
    with svc.db.connection:
        svc.db.connection.execute('DELETE FROM users WHERE login=?', ('alice',))
    with pytest.raises(ViewerError):
        import_legacy(config, store, svc.db)
    assert svc.db.list_users() == []
    assert not store.archives


def test_frozen_plan_rejects_changed_source_on_resume(legacy: LegacyFixture) -> None:
    """Migration resume rejects source bytes changed after its plan was frozen."""
    svc, store, config, rid, receipt = legacy
    archive_key = f'runs/alice/{rid}/archive.tar.gz'
    store.fail_archive = archive_key
    with pytest.raises(ViewerError):
        import_legacy(config, store, svc.db)
    marker_key = f'runs/alice/{rid}/publication.json'
    marker = json.loads(store.objects[marker_key])
    marker['operation_id'] = 'changed-after-plan'
    store.objects[marker_key] = json.dumps(marker).encode()
    store.fail_archive = None
    with pytest.raises(ViewerError) as caught:
        import_legacy(config, store, svc.db)
    assert caught.value.code == 'migration_source_changed'
    assert store.archives == [archive_key]
