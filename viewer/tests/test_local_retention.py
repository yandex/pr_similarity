"""Local history retention keeps active, unverified and pinned generations."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from local_test_support import RUN_ID, build_publication
from test_restore_reconciliation import restored_service
from test_service import publish
from viewer.config import Config
from viewer.local_files import LocalFiles
from viewer.local_repository import LocalRecord, LocalRepository
from viewer.local_retention import LocalRetention
from viewer.persistence.database import Database

type RetentionFixture = tuple[Config, LocalRepository, LocalFiles, list[float], LocalRecord]


@pytest.fixture
def history(tmp_path: Path) -> RetentionFixture:
    """Create a confirmed generation superseded at a controlled clock time."""
    now = [100.0]
    config = Config(runtime=tmp_path, storage='mock', publication_storage='local', version_retention_seconds=90)
    repository = LocalRepository(Database(tmp_path, clock=lambda: now[0]))
    files = LocalFiles(config)
    publication = build_publication(tmp_path / 'fixture', owner='owner', run_id=RUN_ID)
    row = LocalRecord(
        RUN_ID,
        'owner',
        1,
        1,
        publication.entry,
        publication.metadata,
        str(files.root / f'owner/{RUN_ID}/1-{publication.imported.archive_sha256}'),
        cache_bytes=10,
    )
    assert row.local_path is not None
    path = Path(row.local_path)
    path.mkdir(parents=True)
    (path / 'archive.tar.gz').write_bytes(publication.imported.archive.read_bytes())
    repository.commit(row, expected_generation=None, actor='owner', activate_backup=True)
    job = repository.claim_backup('worker')
    assert job is not None
    repository.finish_backup(job, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    now[0] = 200
    repository.commit(replace(row, generation=2, local_path=None), expected_generation=1, actor='owner')
    return config, repository, files, now, row


def test_retention_starts_at_supersession_and_keeps_current(history: RetentionFixture) -> None:
    """Retention begins at supersession and never prunes the current generation."""
    config, repository, files, now, row = history
    retention = LocalRetention(config, repository, files)
    now[0] = 289
    assert retention.run()['removed_versions'] == 0
    assert row.local_path is not None
    assert Path(row.local_path).is_dir()
    now[0] = 290
    result = retention.run()
    assert result['removed_versions'] == 1
    assert result['remote_history'] == 'retained'
    assert not Path(row.local_path).exists()
    assert [(r.generation, r.run_id) for r in repository.versions()] == [(2, RUN_ID)]
    saved_owner = repository.database.owner(RUN_ID)
    assert saved_owner is not None
    assert saved_owner['login'] == 'owner'


def test_reader_pin_protects_expired_history(history: RetentionFixture) -> None:
    """A reader lease protects expired history until the reader releases it."""
    config, repository, files, now, row = history
    now[0] = 1000
    assert row.local_path is not None
    files.pin(row.local_path)
    assert LocalRetention(config, repository, files).run()['removed_versions'] == 0
    files.unpin(row.local_path)
    assert LocalRetention(config, repository, files).run()['removed_versions'] == 1


def test_invalid_backup_and_busy_run_protect_history(history: RetentionFixture) -> None:
    """Invalid backups and active run work prevent history removal."""
    config, repository, files, now, row = history
    now[0] = 1000
    assert LocalRetention(config, repository, files, busy=lambda rid: True).run()['removed_versions'] == 0
    repository.update_local_state(
        RUN_ID, generation=1, local_path=row.local_path, cache_state='ready', backup_state='blocked'
    )
    assert LocalRetention(config, repository, files).run()['removed_versions'] == 0


def test_pending_metadata_protects_history(tmp_path: Path) -> None:
    """Unconfirmed metadata prevents pruning of its historical generation."""
    config = Config(runtime=tmp_path, storage='mock', version_retention_seconds=1)
    now = [10.0]
    repository = LocalRepository(Database(tmp_path, clock=lambda: now[0]))
    files = LocalFiles(config)
    publication = build_publication(tmp_path / 'fixture', owner='owner', run_id=RUN_ID)
    row = LocalRecord(RUN_ID, 'owner', 1, 1, publication.entry, publication.metadata, None)
    repository.commit(row, expected_generation=None, actor='owner', activate_backup=True)
    job = repository.claim_backup('worker')
    assert job is not None
    repository.finish_backup(job, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    repository.mutate_metadata(RUN_ID, {'display_name': 'pending'}, expected_generation=1, expected_metadata_revision=1)
    repository.commit(replace(row, generation=2), expected_generation=1, actor='owner')
    now[0] = 1000
    assert LocalRetention(config, repository, files).run()['removed_versions'] == 0
    assert len(repository.versions()) == 2


def test_preexisting_history_without_timestamp_gets_full_retention_window(history: RetentionFixture) -> None:
    """Legacy history without a timestamp receives a complete retention window."""
    config, repository, files, now, row = history
    with repository.connection:
        raw = repository.connection.execute(
            'SELECT data FROM local_records WHERE run_id=? AND generation=1', (RUN_ID,)
        ).fetchone()['data']
        value = json.loads(raw)
        value.pop('superseded_at')
        repository.connection.execute(
            'UPDATE local_records SET data=? WHERE run_id=? AND generation=1', (json.dumps(value), RUN_ID)
        )
    now[0] = 10000
    retention = LocalRetention(config, repository, files)
    assert retention.run()['removed_versions'] == 0
    assert repository.versions()[0].superseded_at == 10000
    now[0] += config.version_retention_seconds
    assert retention.run()['removed_versions'] == 1


def test_pruning_rechecks_record_identity_and_current_head(history: RetentionFixture) -> None:
    """Pruning rechecks metadata identity and refuses the current head."""
    config, repository, files, now, row = history
    now[0] = 1000
    candidate = repository.retention_candidate(RUN_ID, 1, before=900)
    assert candidate is not None
    assert not repository.prune_version(replace(candidate, metadata_revision=999), before=900)
    stored_record = repository.get(RUN_ID)
    assert stored_record is not None
    assert not repository.prune_version(stored_record, before=900)
    assert len(repository.versions()) == 2


def test_restored_superseded_job_does_not_block_retention(tmp_path: Path) -> None:
    """Reconciled metadata jobs allow pruning after a later backed-up overwrite."""
    with restored_service(tmp_path, metadata_only=True) as (_, svc, remote, rid):
        assert svc.refresh()['status'] == 'succeeded'
        publish(svc, tmp_path, run_id=rid, score=0.25)
        svc.activate_backup(rid)
        assert svc.backups.run_once()
        states = [row['state'] for row in svc.db.connection.execute('SELECT state FROM local_backup_jobs')]
        assert 'superseded' in states
        before = svc.db.clock() + 100000000
        candidate = svc.repository.retention_candidate(rid, 1, before=before)
        assert candidate is not None
        assert candidate.backup_valid and candidate.backup_metadata_revision == 3
        remote_before = dict(remote.objects)
        svc.db.clock = lambda: before
        config = replace(svc.config, version_retention_seconds=1)
        assert LocalRetention(config, svc.repository, svc.files).run()['removed_versions'] == 1
        assert all(row.generation == 2 for row in svc.repository.versions())
        assert remote.objects == remote_before
