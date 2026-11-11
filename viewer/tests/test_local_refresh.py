"""Full refresh validates closed backups and persists resumable progress."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Never

import pytest
from local_test_support import FaultStore, build_bundle_fixture
from viewer.bundles import (
    REGISTRY_HEAD,
    BundleDescriptor,
    BundleRepository,
    DownloadedBundle,
)
from viewer.errors import StorageError, ViewerError
from viewer.local_refresh import RefreshAudit
from viewer.local_repository import LocalRecord, LocalRepository
from viewer.persistence.database import Database
from viewer.persistence.storage import StoredBody
from viewer.publication import CatalogueEntry

type AuditInputs = tuple[BundleRepository, FaultStore, BundleDescriptor, LocalRepository, CatalogueEntry]


@pytest.fixture
def audit_inputs(tmp_path: Path) -> Iterator[AuditInputs]:
    """Publish one bundle and keep its local database open for the audit."""
    bundles, store, identity, imported, marker, metadata, entry = build_bundle_fixture(tmp_path)
    descriptor = bundles.publish_bundle(identity, imported, marker, metadata)
    database = Database(tmp_path / 'db')
    repository = LocalRepository(database)
    yield bundles, store, descriptor, repository, entry
    database.close()


def test_fresh_refresh_downloads_every_time_without_remote_writes(audit_inputs: AuditInputs) -> None:
    """Each fresh audit rereads every registered archive without modifying remote storage."""
    bundles, store, descriptor, repository, entry = audit_inputs
    applied = []

    def apply(downloaded: DownloadedBundle, expected: LocalRecord | None) -> str:
        """Record the restored bytes after the audit validates the archive."""
        applied.append(downloaded.imported.archive.read_bytes())
        return 'updated'

    audit = RefreshAudit(bundles.config, repository, bundles, apply, busy=lambda _: False, start_worker=False)
    store.writes.clear()
    first = audit.request()['request_id']
    audit.run_once()
    status = audit.status()
    assert status['status'] == 'succeeded'
    assert status['checked'] == status['updated'] == 1
    assert status['completed_id'] == first
    assert audit.request()['request_id'] != first
    audit.run_once()
    assert len(applied) == 2
    assert store.writes == []
    audit.close()


def test_bad_closure_does_not_cancel_other_runs(audit_inputs: AuditInputs) -> None:
    """An invalid closing manifest does not prevent verification of other registered runs."""
    bundles, store, descriptor, repository, entry = audit_inputs
    bad = bundles.publish_tombstone(
        replace(descriptor.identity, run_id='ffffffff-ffff-ffff-ffff-ffffffffffff'), descriptor.metadata
    )
    store.objects[bad.manifest_key] = b'{}'
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    status = audit.status()
    assert status['status'] == 'partial'
    assert status['errors'] == 1
    assert status['updated'] == 1
    assert status['last_success_at'] is None


def test_partial_list_cannot_hide_registry_entries(audit_inputs: AuditInputs) -> None:
    """A truncated object listing cannot hide entries from the authoritative registry."""
    bundles, store, descriptor, repository, entry = audit_inputs

    def partial(prefix: str) -> Iterator[str]:
        """Raise after yielding one key to prove refresh uses registry references."""
        yield descriptor.manifest_key
        raise StorageError()

    store.list = partial
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'succeeded'
    assert audit.status()['checked'] == 1
    saved_refresh = repository.load_refresh('active')
    assert saved_refresh is not None
    assert saved_refresh['plan']


def test_corrupt_remote_revokes_eviction_but_transient_failure_does_not(audit_inputs: AuditInputs) -> None:
    """Confirmed archive corruption protects the matching local copy from eviction."""
    bundles, store, descriptor, repository, entry = audit_inputs
    identity = descriptor.identity
    record = LocalRecord(
        identity.run_id,
        identity.owner,
        identity.generation,
        identity.metadata_revision,
        entry,
        descriptor.metadata,
        '/local',
        backup_state='verified',
        backup_metadata_revision=1,
        backup_manifest_key=descriptor.manifest_key,
    )
    repository.commit(record, expected_generation=None, actor='alice', idempotency_key='one', receipt={})
    store.objects[identity.archive_key] = b'broken'
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    stored_record = repository.get(identity.run_id)
    assert stored_record is not None
    assert not stored_record.backup_valid


def test_restart_resumes_plan_without_rechecking_completed_identity(audit_inputs: AuditInputs) -> None:
    """Restart resumes the saved boundary without reapplying a completed identity."""
    bundles, store, descriptor, repository, entry = audit_inputs
    bundles.publish_tombstone(
        replace(descriptor.identity, run_id='ffffffff-ffff-ffff-ffff-ffffffffffff', generation=2),
        descriptor.metadata,
    )

    def crash(descriptor: BundleDescriptor, expected: LocalRecord | None) -> Never:
        """Interrupt the merge before its successful checkpoint can be persisted."""
        raise KeyboardInterrupt()

    first = RefreshAudit(
        bundles.config,
        repository,
        bundles,
        lambda *_: 'updated',
        busy=lambda _: False,
        apply_tombstone=crash,
        start_worker=False,
    )
    first.request()
    with pytest.raises(KeyboardInterrupt):
        first.run_once()
    assert first.status()['checked'] == 1
    first.close()

    def already_checked(downloaded: DownloadedBundle, expected: LocalRecord | None) -> Never:
        """Fail if a resumed audit reapplies a completed archive."""
        pytest.fail('completed archive was downloaded/applied again')

    second = RefreshAudit(
        bundles.config,
        repository,
        bundles,
        already_checked,
        busy=lambda _: False,
        apply_tombstone=lambda *_: 'updated',
        start_worker=False,
    )
    second.run_once()
    assert second.status()['status'] == 'succeeded'
    assert second.status()['checked'] == 2


def test_newer_local_generation_is_protected_after_remote_verification(audit_inputs: AuditInputs) -> None:
    """Audit verifies remote bytes but preserves a newer local generation."""
    bundles, store, descriptor, repository, entry = audit_inputs
    identity = descriptor.identity
    record = LocalRecord(identity.run_id, identity.owner, 3, 1, entry, descriptor.metadata, '/local')
    for generation in range(1, 4):
        repository.commit(
            replace(record, generation=generation), expected_generation=generation - 1 or None, actor='alice'
        )

    def forbidden(downloaded: DownloadedBundle, expected: LocalRecord | None) -> Never:
        """Fail if an older remote version reaches the merge callback."""
        pytest.fail('newer local record must not be replaced')

    audit = RefreshAudit(bundles.config, repository, bundles, forbidden, busy=lambda _: False, start_worker=False)
    audit.request()
    audit.run_once()
    assert audit.status()['protected'] == 1
    assert audit.status()['checked'] == 1
    stored_record = repository.get(identity.run_id)
    assert stored_record is not None
    assert stored_record.generation == 3


def test_transient_storage_failure_preserves_backup_validity(audit_inputs: AuditInputs) -> None:
    """Transient storage failures do not revoke previously confirmed backup validity."""
    bundles, store, descriptor, repository, entry = audit_inputs
    identity = descriptor.identity
    record = LocalRecord(
        identity.run_id,
        identity.owner,
        identity.generation,
        identity.metadata_revision,
        entry,
        descriptor.metadata,
        '/local',
        backup_state='verified',
        backup_metadata_revision=1,
        backup_manifest_key=descriptor.manifest_key,
    )
    repository.commit(record, expected_generation=None, actor='alice', idempotency_key='one', receipt={})
    original = store.open

    def fail_archive(key: str) -> StoredBody:
        """Reject archive reads while leaving registry and metadata reads available."""
        if key == identity.archive_key:
            raise StorageError()
        return original(key)

    store.open = fail_archive
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    stored_record = repository.get(identity.run_id)
    assert stored_record is not None
    assert stored_record.backup_valid


def test_busy_run_is_deferred_without_success_timestamp(audit_inputs: AuditInputs) -> None:
    """A busy run defers its merge and prevents a full-success timestamp."""
    bundles, store, descriptor, repository, entry = audit_inputs
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: True, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    assert audit.status()['deferred'] == 1
    assert audit.status()['last_success_at'] is None


@pytest.mark.parametrize('damage', ['closure_missing', 'metadata_corrupt'])
def test_missing_or_corrupt_closure_protects_backed_local_copy(audit_inputs: AuditInputs, damage: str) -> None:
    """Missing or corrupted backup metadata protects the matching local publication."""
    bundles, store, descriptor, repository, entry = audit_inputs
    identity = descriptor.identity
    record = LocalRecord(
        identity.run_id,
        identity.owner,
        identity.generation,
        identity.metadata_revision,
        entry,
        descriptor.metadata,
        '/local',
        backup_state='verified',
        backup_metadata_revision=1,
        backup_manifest_key=descriptor.manifest_key,
    )
    repository.commit(record, expected_generation=None, actor='alice', idempotency_key='one', receipt={})
    if damage == 'closure_missing':
        del store.objects[descriptor.manifest_key]
    else:
        store.objects[identity.metadata_key] = b'{}'
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    stored_record = repository.get(identity.run_id)
    assert stored_record is not None
    assert not stored_record.backup_valid


def test_apply_error_does_not_invalidate_verified_remote(audit_inputs: AuditInputs) -> None:
    """A local merge failure cannot invalidate already verified remote evidence."""
    bundles, store, descriptor, repository, entry = audit_inputs
    identity = descriptor.identity
    record = LocalRecord(
        identity.run_id,
        identity.owner,
        identity.generation,
        identity.metadata_revision,
        entry,
        descriptor.metadata,
        '/local',
        backup_state='verified',
        backup_metadata_revision=1,
        backup_manifest_key=descriptor.manifest_key,
    )
    repository.commit(record, expected_generation=None, actor='alice', idempotency_key='one', receipt={})

    def fail(downloaded: DownloadedBundle, expected: LocalRecord | None) -> Never:
        """Reject local installation after remote verification succeeds."""
        raise ViewerError('invalid_contract', 'Local apply rejected.')

    audit = RefreshAudit(bundles.config, repository, bundles, fail, busy=lambda _: False, start_worker=False)
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    stored_record = repository.get(identity.run_id)
    assert stored_record is not None
    assert stored_record.backup_valid


@pytest.mark.parametrize('damage', ['archive', 'metadata'])
def test_newer_metadata_revision_does_not_hide_shared_archive_corruption(
    audit_inputs: AuditInputs, damage: str
) -> None:
    """Shared archive corruption invalidates older revisions while metadata damage stays isolated."""
    bundles, store, descriptor, repository, entry = audit_inputs
    identity = descriptor.identity
    record = LocalRecord(
        identity.run_id,
        identity.owner,
        identity.generation,
        identity.metadata_revision,
        entry,
        descriptor.metadata,
        '/local',
        backup_state='verified',
        backup_metadata_revision=1,
        backup_manifest_key=descriptor.manifest_key,
    )
    repository.commit(record, expected_generation=None, actor='alice', idempotency_key='one', receipt={})
    newer = replace(identity, metadata_revision=2)
    bundles.publish_bundle(newer, None, descriptor.marker, dict(descriptor.metadata, display_name='newer'))
    if damage == 'archive':
        store.objects[identity.archive_key] = b'broken'
    else:
        store.objects[newer.metadata_key] = b'{}'
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    stored_record = repository.get(identity.run_id)
    assert stored_record is not None
    assert stored_record.backup_valid is (damage == 'metadata')


def test_silent_list_omission_cannot_hide_corrupt_registered_archive(audit_inputs: AuditInputs) -> None:
    """Silent LIST omissions cannot conceal corruption in a registered archive."""
    bundles, store, descriptor, repository, entry = audit_inputs
    store.list = lambda prefix: iter(())
    store.objects[descriptor.identity.archive_key] = b'corrupt'
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    assert audit.status()['total'] == audit.status()['errors'] == 1
    assert audit.status()['last_success_at'] is None


def test_missing_registry_head_fails_refresh_without_remote_writes(audit_inputs: AuditInputs) -> None:
    """A missing registry head fails the audit without rebuilding remote state."""
    bundles, store, descriptor, repository, entry = audit_inputs
    del store.objects[REGISTRY_HEAD]
    store.writes.clear()
    audit = RefreshAudit(
        bundles.config, repository, bundles, lambda *_: 'updated', busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'failed'
    assert audit.status()['last_success_at'] is None
    assert store.writes == []


def test_explicit_partial_resume_reuses_boundary_and_completed_checkpoints(audit_inputs: AuditInputs) -> None:
    """Explicit resume preserves the boundary and retries only unfinished items."""
    bundles, store, descriptor, repository, entry = audit_inputs
    other = bundles.publish_tombstone(
        replace(descriptor.identity, run_id='ffffffff-ffff-ffff-ffff-ffffffffffff'), descriptor.metadata
    )
    original = store.objects[other.manifest_key]
    store.objects[other.manifest_key] = b'{}'
    applied = []

    def apply(downloaded: DownloadedBundle, expected: LocalRecord | None) -> str:
        """Count archive applications to detect repeated work during resume."""
        applied.append(downloaded.descriptor.identity.run_id)
        return 'updated'

    audit = RefreshAudit(
        bundles.config,
        repository,
        bundles,
        apply,
        apply_tombstone=lambda *_: 'updated',
        busy=lambda _: False,
        start_worker=False,
    )
    request_id = audit.request()['request_id']
    audit.run_once()
    boundary = audit.status()['boundary']
    assert audit.status()['status'] == 'partial'
    assert len(applied) == 1
    store.objects[other.manifest_key] = original
    audit.resume(request_id)
    audit.run_once()
    assert audit.status()['status'] == 'succeeded'
    assert audit.status()['boundary'] == boundary
    assert audit.status()['checked'] == 2
    assert len(applied) == 1
    audit.request()
    audit.run_once()
    assert len(applied) == 2


@pytest.mark.parametrize('failure', ['download', 'apply'])
def test_download_reservation_is_released_after_failure(audit_inputs: AuditInputs, failure: str) -> None:
    """Download and merge failures release disk reservations after staging cleanup."""
    bundles, store, descriptor, repository, entry = audit_inputs
    events = []

    @contextmanager
    def reserve(value: BundleDescriptor) -> Iterator[None]:
        """Hold the reservation until temporary files have been cleaned up."""
        assert value == descriptor
        events.append('reserved')
        try:
            yield
        finally:
            assert not list((bundles.config.runtime / 'refresh-staging').glob('audit-*'))
            events.append('released')

    original = bundles.download_bundle

    def download(descriptor: BundleDescriptor, destination: Path) -> DownloadedBundle:
        """Require an active disk reservation before reading archive bytes."""
        assert events == ['reserved']
        if failure == 'download':
            raise StorageError()
        return original(descriptor, destination)

    bundles.download_bundle = download

    def apply(downloaded: DownloadedBundle, expected: LocalRecord | None) -> str:
        """Fail local installation while its disk reservation is still held."""
        assert events == ['reserved']
        raise ViewerError('local_apply_failed', 'Local apply failed.')

    audit = RefreshAudit(
        bundles.config, repository, bundles, apply, reserve_download=reserve, busy=lambda _: False, start_worker=False
    )
    audit.request()
    audit.run_once()
    assert audit.status()['status'] == 'partial'
    assert events == ['reserved', 'released']


def test_zero_close_budget_reports_worker_still_using_database(audit_inputs: AuditInputs) -> None:
    """A zero shutdown budget reports the active worker before database closure."""
    bundles, store, descriptor, repository, entry = audit_inputs
    entered, release = Event(), Event()

    def apply(downloaded: DownloadedBundle, expected: LocalRecord | None) -> str:
        """Block the merge until the test releases the worker."""
        entered.set()
        assert release.wait(5)
        return 'updated'

    audit = RefreshAudit(bundles.config, repository, bundles, apply, busy=lambda _: False)
    try:
        audit.request()
        assert entered.wait(5)
        with pytest.raises(ViewerError) as error:
            audit.close(timeout=0)
        assert error.value.code == 'refresh_busy'
        assert repository.connection.execute('SELECT 1').fetchone()[0] == 1
    finally:
        release.set()
        audit.close(timeout=5)


def test_progress_callback_failure_is_logged_without_interrupting_audit(
    audit_inputs: AuditInputs, caplog: pytest.LogCaptureFixture
) -> None:
    """Optional progress failures emit safe diagnostics while durable verification completes."""
    bundles, store, descriptor, repository, entry = audit_inputs

    def fail_progress(progress: dict) -> Never:
        """Raise an observer failure containing text that must not enter service logs."""
        raise RuntimeError('private-error-body')

    audit = RefreshAudit(
        bundles.config,
        repository,
        bundles,
        lambda downloaded, expected: 'updated',
        busy=lambda run_id: False,
        on_progress=fail_progress,
        start_worker=False,
    )
    with caplog.at_level(logging.WARNING, logger='viewer.refresh'):
        audit.request()
        audit.run_once()
    saved = repository.load_refresh('active')
    assert saved is not None
    assert saved['status'] == 'succeeded'
    assert audit.status()['checked'] == 1
    assert 'refresh_progress_callback_failed exception_type=RuntimeError' in caplog.text
    assert 'private-error-body' not in caplog.text
