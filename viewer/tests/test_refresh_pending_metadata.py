"""Refresh repairs current archive files without rolling back local metadata or backup progress."""

import gzip
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from local_test_support import ValidatedPublication, build_publication
from viewer.bundles import BundleDescriptor, BundleIdentity, DownloadedBundle
from viewer.config import Config, Limits
from viewer.errors import ViewerError
from viewer.local_repository import LocalRecord
from viewer.local_service import LocalViewerService
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore
from viewer.validation.archive import ImportedRun, validate_archive

type RepairFixture = tuple[LocalViewerService, MemoryStore, str, ValidatedPublication]


def record_for(service: LocalViewerService, run_id: str) -> LocalRecord:
    """Require the live record whose metadata and cache state the test inspects."""
    record = service.repository.get(run_id)
    assert record is not None
    return record


def backup_jobs(service: LocalViewerService, run_id: str) -> list[dict]:
    """Capture durable job fields to detect unintended activation or completion by refresh."""
    return [
        dict(row)
        for row in service.db.connection.execute(
            'SELECT * FROM local_backup_jobs WHERE run_id=? ORDER BY id', (run_id,)
        ).fetchall()
    ]


@pytest.fixture
def pending_metadata(tmp_path: Path) -> Iterator[RepairFixture]:
    """Back up the first revision and leave newer local display metadata awaiting backup."""
    config = Config(
        runtime=tmp_path / 'runtime',
        storage='mock',
        publication_storage='local',
        disk_reserve_bytes=0,
        limits=Limits(unpacked_bytes=1 << 20, staging_free_bytes=0),
    )
    database = Database(config.runtime)
    database.create_user('alice', 'password')
    remote = MemoryStore()
    service = LocalViewerService(config, remote, database)
    publication = build_publication(tmp_path / 'publication')
    run_id = publication.imported.report.run.id
    try:
        service.publish(publication.imported, 'alice', 'publication')
        service.activate_backup(run_id)
        assert service.backups.run_once()
        service.patch_metadata(run_id, 'alice', {'display_name': 'Unbacked name', 'tags': ['new', 'new']})
        assert record_for(service, run_id).metadata_revision == 2
        yield service, remote, run_id, publication
    finally:
        assert service.close()
        database.close()


@pytest.mark.parametrize('pinned', [False, True])
def test_refresh_repairs_corruption_while_preserving_pending_metadata(
    pending_metadata: RepairFixture, pinned: bool
) -> None:
    """Verified revision-one files repair the cache without confirming pending revision two."""
    service, remote, run_id, publication = pending_metadata
    if pinned:
        service.set_pinned(run_id, True)
    before = record_for(service, run_id)
    assert before.local_path is not None
    jobs = backup_jobs(service, run_id)
    remote_before = dict(remote.objects)
    (Path(before.local_path) / 'data/report.json').write_bytes(b'corrupt')
    with pytest.raises(ViewerError) as error:
        service.full_report(run_id)
    assert error.value.code == 'stored_digest_mismatch'

    result = service.refresh()

    assert result['status'] == 'succeeded'
    assert result['updated'] == 1 and result['protected'] == result['errors'] == 0
    after = record_for(service, run_id)
    assert (after.generation, after.archive_sha256, after.metadata_revision) == (
        before.generation,
        before.archive_sha256,
        before.metadata_revision,
    )
    assert after.metadata == before.metadata
    assert after.backup_state == 'pending' and after.backup_metadata_revision == 1
    assert after.backup_manifest_key == before.backup_manifest_key and after.backup_valid
    assert after.local_path is not None and after.local_path != before.local_path
    assert after.last_access == before.last_access
    assert (Path(after.local_path) / 'archive.tar.gz').read_bytes() == publication.imported.archive.read_bytes()
    assert service.full_report(run_id).run.id == run_id
    assert service.run(run_id)['display_name'] == 'Unbacked name'
    assert backup_jobs(service, run_id) == jobs
    assert remote.objects == remote_before


def test_repaired_path_retires_only_after_existing_reader_releases(pending_metadata: RepairFixture) -> None:
    """Repair switches new readers to a new path while preserving the previously pinned tree."""
    service, remote, run_id, publication = pending_metadata
    before = record_for(service, run_id)
    assert before.local_path is not None
    old_path = Path(before.local_path)
    lease = service.acquire_read(run_id)
    try:
        assert service.refresh()['updated'] == 1
        current = record_for(service, run_id)
        assert current.local_path != before.local_path
        assert old_path.is_dir()
        with service.bind_read(lease):
            assert service.full_report(run_id).run.id == run_id
        service.cleanup_cache()
        assert old_path.is_dir()
    finally:
        lease.close(False)
    service.cleanup_cache()
    assert not old_path.exists()
    assert record_for(service, run_id).local_path == current.local_path
    assert current.metadata == before.metadata


@pytest.mark.parametrize('phase', ['download', 'install'])
def test_concurrent_metadata_change_preserves_labels_and_repairs_files(
    pending_metadata: RepairFixture, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Metadata changes during download or installation do not leave the same archive corrupt."""
    service, remote, run_id, publication = pending_metadata
    before = record_for(service, run_id)
    assert before.local_path is not None
    (Path(before.local_path) / 'data/report.json').write_bytes(b'corrupt')
    installed: list[Path] = []
    download = service.bundles.download_bundle
    install = service.files.install

    def change_metadata() -> None:
        """Advance the local metadata head after the audit captures its boundary."""
        service.patch_metadata(run_id, 'alice', {'display_name': 'Concurrent name'})

    def download_then_change(descriptor: BundleDescriptor, destination: Path) -> DownloadedBundle:
        """Change metadata after remote validation but before the audit chooses its merge."""
        result = download(descriptor, destination)
        change_metadata()
        return result

    def install_then_change(
        imported: ImportedRun,
        identity: BundleIdentity,
        marker: dict,
        *,
        replace_existing: bool = False,
        installation_id: uuid.UUID | None = None,
    ) -> Path:
        """Change metadata after staging installation but before the database CAS."""
        path = install(imported, identity, marker, replace_existing=replace_existing, installation_id=installation_id)
        installed.append(path)
        change_metadata()
        return path

    if phase == 'download':
        monkeypatch.setattr(service.bundles, 'download_bundle', download_then_change)
    else:
        monkeypatch.setattr(service.files, 'install', install_then_change)
    result = service.refresh()
    assert result['status'] == 'succeeded' and result['updated'] == 1
    assert result['protected'] == result['deferred'] == result['errors'] == 0
    current = record_for(service, run_id)
    assert current.metadata_revision == 3 and current.metadata['display_name'] == 'Concurrent name'
    assert current.local_path != before.local_path and current.archive_sha256 == before.archive_sha256
    assert service.full_report(run_id).run.id == run_id
    assert all(path.exists() for path in installed)
    assert backup_jobs(service, run_id)[-1]['state'] == 'pending'


def test_newer_local_archive_remains_protected(pending_metadata: RepairFixture, tmp_path: Path) -> None:
    """An older remote generation cannot repair or replace bytes belonging to a newer archive."""
    service, remote, run_id, publication = pending_metadata
    replacement = tmp_path / 'replacement.tar.gz'
    replacement.write_bytes(gzip.compress(gzip.decompress(publication.imported.archive.read_bytes()), mtime=0))
    imported = validate_archive(replacement, tmp_path / 'replacement-data', service.config.limits)
    assert imported.archive_sha256 != publication.imported.archive_sha256
    service.publish(imported, 'alice', 'replacement', overwrite=True)
    before = record_for(service, run_id)
    assert before.generation == 2
    result = service.refresh()
    assert result['protected'] == 1 and result['updated'] == 0
    after = record_for(service, run_id)
    assert after == before
    assert after.local_path is not None
    assert (Path(after.local_path) / 'archive.tar.gz').read_bytes() == replacement.read_bytes()


def test_repair_preserves_backup_completed_during_file_install(
    pending_metadata: RepairFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final merge retains a newer independently confirmed backup revision."""
    service, remote, run_id, publication = pending_metadata
    before = record_for(service, run_id)
    install = service.files.install

    def install_and_finish_backup(
        imported: ImportedRun,
        identity: BundleIdentity,
        marker: dict,
        *,
        replace_existing: bool = False,
        installation_id: uuid.UUID | None = None,
    ) -> Path:
        """Finish the queued metadata backup while the audit is preparing its replacement tree."""
        path = install(imported, identity, marker, replace_existing=replace_existing, installation_id=installation_id)
        assert service.backups.run_once()
        assert record_for(service, run_id).backup_metadata_revision == 2
        return path

    monkeypatch.setattr(service.files, 'install', install_and_finish_backup)
    assert service.refresh()['updated'] == 1
    after = record_for(service, run_id)
    assert after.metadata == before.metadata and after.metadata_revision == 2
    assert after.backup_state == 'backed_up' and after.backup_metadata_revision == 2
    assert after.backup_manifest_key == service._identity(after).closing_key
    assert after.backup_valid
    assert backup_jobs(service, run_id)[-1]['state'] == 'backed_up'
    assert service.full_report(run_id).run.id == run_id


@pytest.mark.parametrize('change', ['overwrite', 'delete'])
def test_content_identity_change_during_download_still_fences_repair(
    pending_metadata: RepairFixture, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Allow label races without reviving or replacing a newer publication generation."""
    service, remote, run_id, publication = pending_metadata
    download = service.bundles.download_bundle

    def download_then_change(descriptor: BundleDescriptor, destination: Path) -> DownloadedBundle:
        """Advance the generation after verification of the older remote archive."""
        result = download(descriptor, destination)
        if change == 'overwrite':
            service.publish(publication.imported, 'alice', 'concurrent-generation', overwrite=True)
        else:
            service.delete(run_id)
        return result

    monkeypatch.setattr(service.bundles, 'download_bundle', download_then_change)
    result = service.refresh()
    assert result['updated'] == 0 and result['protected'] == 1
    current = service.repository.get(run_id, include_deleted=True)
    assert current is not None and current.generation == 2
    assert current.deleted is (change == 'delete')
    assert current.backup_state == 'pending'
