"""Catalogue transactions preserve complete projections and fence stale workers."""

import hashlib
import sqlite3
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from local_test_support import RUN_ID, build_publication
from viewer.errors import ViewerError
from viewer.local_repository import (
    LocalRecord,
    LocalRepository,
    decode_entry,
    encode_entry,
)
from viewer.persistence.database import Database
from viewer.presentation.measurements import Measurement
from viewer.presentation.task_measurements import TaskMeasurements


@pytest.fixture
def record(tmp_path: Path) -> LocalRecord:
    """Build a complete catalogue record from validated publication evidence."""
    publication = build_publication(tmp_path / 'fixture', owner='owner', run_id=RUN_ID)
    entry = publication.entry
    return LocalRecord(RUN_ID, 'owner', 1, 1, entry, publication.metadata, str(publication.imported.directory))


def test_codec_preserves_decimal_measurements_and_integer_keys(record: LocalRecord) -> None:
    """Projection serialization retains Decimal values, typed measurements and integer keys."""
    entry = deepcopy(record.entry)
    entry['aggregates'] = {1: {'amount': Decimal('0.1234567890123456789')}}
    entry['measurements'] = {
        't': TaskMeasurements(
            {'input': Measurement(3, True, False)}, Measurement(Decimal('0.01'), True, True), 'opencode'
        )
    }
    assert decode_entry(encode_entry(entry)) == entry


def test_commit_snapshot_restart_receipt_and_response_activation(tmp_path: Path, record: LocalRecord) -> None:
    """Restart restores the catalogue and receipt before activating durable backup intent."""
    db = Database(tmp_path)
    repo = LocalRepository(db)
    row = record
    repo.commit(
        row, expected_generation=None, actor='admin', idempotency_key='key', fingerprint='fp', receipt={'ok': True}
    )
    saved_owner = db.owner(RUN_ID)
    assert saved_owner is not None
    assert saved_owner['login'] == 'owner'
    saved_receipt = db.receipt('admin', 'key')
    assert saved_receipt is not None
    assert saved_receipt['receipt'] == {'ok': True}
    assert db.receipt('owner', 'key') is None
    assert repo.claim_backup('w') is None
    db.close()
    db = Database(tmp_path)
    repo = LocalRepository(db)
    assert repo.load_snapshot().runs[RUN_ID] == row.entry
    repo.recover_jobs()
    assert repo.claim_backup('w') is not None


def test_atomic_rollback(tmp_path: Path, record: LocalRecord) -> None:
    """A failed backup-intent insert rolls back ownership, receipt and visible publication."""
    db = Database(tmp_path)
    repo = LocalRepository(db)
    db.connection.execute(
        '''CREATE TRIGGER fail_backup BEFORE INSERT ON local_backup_jobs BEGIN SELECT RAISE(ABORT, 'injected'); END'''
    )
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        repo.commit(record, expected_generation=None, actor='admin', idempotency_key='k', receipt={'ok': True})
    assert repo.get(RUN_ID) is None
    assert db.owner(RUN_ID) is None
    assert db.receipt('admin', 'k') is None


def test_cas_and_metadata_fencing(tmp_path: Path, record: LocalRecord) -> None:
    """A stale backup completion cannot confirm a newer metadata revision."""
    repo = LocalRepository(Database(tmp_path))
    row = record
    repo.commit(row, expected_generation=None, actor='owner', activate_backup=True)
    old = repo.claim_backup('w')
    assert old is not None
    updated = repo.mutate_metadata(RUN_ID, {'display_name': 'new'}, expected_generation=1, expected_metadata_revision=1)
    assert updated.generation == 1 and updated.metadata_revision == 2
    assert repo.finish_backup(old, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.backup_state != 'backed_up'
    job = repo.claim_backup('w')
    assert job is not None
    assert job.kind == 'metadata'
    with pytest.raises(ViewerError):
        repo.commit(replace(row, generation=2), expected_generation=None, actor='owner')


def test_expired_worker_fence(tmp_path: Path, record: LocalRecord) -> None:
    """A replacement lease prevents the expired worker from completing the same job."""
    now = [0.0]
    repo = LocalRepository(Database(tmp_path, clock=lambda: now[0]))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    first = repo.claim_backup('first', lease_seconds=10)
    assert first is not None
    now[0] = 11
    second = repo.claim_backup('second')
    assert second is not None
    assert second is not None
    assert not repo.finish_backup(first, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    assert repo.finish_backup(second, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')


def test_real_cached_report_roundtrip(tmp_path: Path) -> None:
    """A complete publication projection retains typed report models after serialization."""
    entry = build_publication(tmp_path / 'fixture').entry
    restored = decode_entry(encode_entry(entry))
    assert restored == entry
    assert type(restored['report']) is type(entry['report'])
    assert restored['report'].run.to_wire() == entry['report'].run.to_wire()


def test_v2_backup_and_version_survive_reopen(tmp_path: Path) -> None:
    """Database upgrade preserves the prior schema backup and does not repeat it on reopen."""
    db = Database(tmp_path)
    db.create_user('owner', 'password')
    LocalRepository(db)
    backups = list(tmp_path.glob('viewer-v2-backup-*.sqlite'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 2
        assert backup.execute('SELECT login FROM users').fetchone()[0] == 'owner'
    db.close()
    db = Database(tmp_path)
    assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 3
    LocalRepository(db)
    assert len(list(tmp_path.glob('viewer-v2-backup-*.sqlite'))) == 1


def test_metadata_completion_retains_older_backup_and_corruption_protection(
    tmp_path: Path, record: LocalRecord
) -> None:
    """Older confirmed metadata remains recorded while corruption blocks local eviction."""
    repo = LocalRepository(Database(tmp_path))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    job = repo.claim_backup('w')
    assert job is not None
    repo.mutate_metadata(RUN_ID, {'display_name': 'new'}, expected_generation=1, expected_metadata_revision=1)
    assert repo.finish_backup(job, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    row = repo.get(RUN_ID)
    assert row is not None
    assert row.backup_metadata_revision == 1
    assert row.backup_manifest_key == f'bundle-index/v1/owner/{RUN_ID}/1/1.json'
    assert row.backup_state == 'pending'
    assert repo.invalidate_backup(RUN_ID, 1, 'bad source')
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert not stored_record.backup_valid
    assert not repo.invalidate_backup(RUN_ID, 2, 'wrong generation')


def test_refresh_state_and_operations_survive_restart(tmp_path: Path) -> None:
    """Durable operation identity and partial refresh progress survive database reopen."""
    db = Database(tmp_path)
    repo = LocalRepository(db)
    repo.set_operation('actor', 'key', 'fingerprint', {'phase': 'extracting'})
    with pytest.raises(ViewerError):
        repo.set_operation('actor', 'key', 'other', {'phase': 'validating'})
    repo.save_refresh('refresh', {'status': 'partial', 'plan': ['bundle1', 'bundle2'], 'completed': ['bundle1']})
    db.close()
    repo = LocalRepository(Database(tmp_path))
    saved_operation = repo.operation('actor', 'key')
    assert saved_operation is not None
    assert saved_operation['phase'] == 'extracting'
    assert repo.operation('owner', 'key') is None
    saved_refresh = repo.load_refresh('refresh')
    assert saved_refresh is not None
    assert saved_refresh['completed'] == ['bundle1']
    assert repo.load_snapshot().last_refresh is None


def test_owner_and_basket_binding_and_operation_conflict_rollback(tmp_path: Path, record: LocalRecord) -> None:
    """Conflicting actors and ownership cannot replace the existing publication."""
    db = Database(tmp_path)
    repo = LocalRepository(db)
    repo.set_operation('admin', 'key', 'original', {'phase': 'committing'})
    with pytest.raises(ViewerError, match='different content'):
        repo.commit(
            record,
            expected_generation=None,
            actor='admin',
            idempotency_key='key',
            fingerprint='different',
            receipt={},
        )
    assert repo.get(RUN_ID) is None
    repo.commit(record, expected_generation=None, actor='owner')
    changed = replace(record, owner='foreign', generation=2)
    changed_entry = deepcopy(changed.entry)
    changed_entry['marker']['owner'] = 'foreign'
    changed = replace(changed, entry=changed_entry)
    with pytest.raises(ViewerError):
        repo.commit(changed, expected_generation=1, actor='admin')
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.owner == 'owner'
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.generation == 1


def test_tombstone_rejects_stale_restore_and_retains_owner(tmp_path: Path, record: LocalRecord) -> None:
    """Deletion hides the run while retaining ownership and fencing stale restoration."""
    db = Database(tmp_path)
    repo = LocalRepository(db)
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    deleted = repo.tombstone(RUN_ID, expected_generation=1, actor='owner')
    assert deleted.generation == 2
    assert repo.get(RUN_ID) is None
    stored_record = repo.get(RUN_ID, include_deleted=True)
    assert stored_record is not None
    assert stored_record.deleted
    assert not repo.load_snapshot().runs
    assert repo.update_local_state(RUN_ID, generation=1, local_path='old', cache_state='ready')
    stored_record = repo.get(RUN_ID, include_deleted=True)
    assert stored_record is not None
    assert stored_record.generation == 2
    saved_owner = db.owner(RUN_ID)
    assert saved_owner is not None
    assert saved_owner['login'] == 'owner'


def test_restart_fences_old_worker(tmp_path: Path, record: LocalRecord) -> None:
    """Recovery replaces worker leases before accepting another backup completion."""
    repo = LocalRepository(Database(tmp_path))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    first = repo.claim_backup('worker')
    assert first is not None
    repo.recover_jobs()
    second = repo.claim_backup('worker')
    assert second is not None
    assert first.lease_id != second.lease_id
    assert not repo.finish_backup(first, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    assert repo.finish_backup(second, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')


def test_remote_merge_generation_jump_cas_and_hash_conflict(tmp_path: Path, record: LocalRecord) -> None:
    """Remote merge accepts a newer generation but rejects stale CAS and conflicting hashes."""
    repo = LocalRepository(Database(tmp_path))
    remote = replace(record, generation=8, backup_state='backed_up')
    assert repo.merge_remote(remote, expected_generation=None, expected_metadata_revision=None)
    assert repo.claim_backup('w') is None
    assert not repo.merge_remote(replace(remote, generation=9), expected_generation=7, expected_metadata_revision=1)
    changed_entry = deepcopy(remote.entry)
    changed_entry['marker']['archive_sha256'] = 'different'
    changed = replace(remote, entry=changed_entry)
    with pytest.raises(ViewerError):
        repo.merge_remote(changed, expected_generation=8, expected_metadata_revision=1)
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.archive_sha256 == remote.archive_sha256
    assert repo.merge_remote(
        replace(remote, metadata_revision=2, metadata={'display_name': 'remote'}),
        expected_generation=8,
        expected_metadata_revision=1,
    )


def test_old_generation_cache_bookkeeping_reaches_job_snapshots(tmp_path: Path, record: LocalRecord) -> None:
    """Cache eviction updates historical job inputs without changing the catalogue revision."""
    repo = LocalRepository(Database(tmp_path))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    repo.mutate_metadata(RUN_ID, {'display_name': 'v2'}, expected_generation=1, expected_metadata_revision=1)
    repo.commit(replace(record, generation=2), expected_generation=1, actor='owner')
    revision = repo.load_snapshot().revision
    assert repo.update_local_state(RUN_ID, generation=1, local_path=None, cache_state='cold', cache_bytes=0)
    claimed_job = repo.claim_backup('w')
    assert claimed_job is not None
    assert claimed_job.record.local_path is None
    assert [(r.generation, r.metadata_revision) for r in repo.versions()] == [(1, 2), (2, 1)]
    assert repo.load_snapshot().revision == revision


def test_commit_consumes_grant_atomically(tmp_path: Path, record: LocalRecord) -> None:
    """Publication consumes its grant only when the database transaction commits."""
    db = Database(tmp_path)
    db.create_user('owner', 'password')
    repo = LocalRepository(db)
    token = db.issue('owner', 'password', 'publish')['token']
    db.connection.execute(
        '''CREATE TRIGGER fail_backup BEFORE INSERT ON local_backup_jobs BEGIN SELECT RAISE(ABORT, 'injected'); END'''
    )
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        repo.commit(record, expected_generation=None, actor='owner', grant_token=token)
    assert db.authorize(token, 'publish') == 'owner'
    db.connection.execute('DROP TRIGGER fail_backup')
    repo.commit(record, expected_generation=None, actor='owner', grant_token=token)
    with pytest.raises(ViewerError):
        db.authorize(token, 'publish')


def test_commit_preserves_admission_for_expired_grant_status(tmp_path: Path, record: LocalRecord) -> None:
    """Saved admission still identifies a completed operation after its grant expires."""
    db = Database(tmp_path, clock=lambda: 10)
    db.create_user('owner', 'password')
    token = db.issue('owner', 'password', 'publish', ttl_seconds=900)['token']
    assert db.connection.execute('SELECT expires FROM grants').fetchone()[0] == 910
    grant_hash = hashlib.sha256(token.encode()).hexdigest()
    repo = LocalRepository(db)
    repo.set_operation(
        'owner', 'key', 'fp', {'phase': 'committing', 'grant_hash': grant_hash, 'run_id': RUN_ID, 'admin': False}
    )
    repo.commit(
        record,
        expected_generation=None,
        actor='owner',
        idempotency_key='key',
        fingerprint='fp',
        receipt={'operation_id': 'operation', 'message': 'Published'},
        grant_token=token,
    )
    with db.connection:
        db.connection.execute('DELETE FROM grants')
    saved_admission = repo.operation_by_grant('key', grant_hash)
    assert saved_admission is not None
    actor, operation = saved_admission
    assert actor == 'owner'
    assert operation['run_id'] == RUN_ID
    assert operation['phase'] == 'available'
    assert operation['operation_id'] == 'operation'
    assert repo.operation_by_grant('key', 'wrong') is None


def test_old_generation_backup_propagates_to_its_newer_metadata(tmp_path: Path, record: LocalRecord) -> None:
    """Historical backup completion updates the later metadata of the same generation."""
    repo = LocalRepository(Database(tmp_path))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    job = repo.claim_backup('worker')
    assert job is not None
    repo.mutate_metadata(RUN_ID, {'display_name': 'new'}, expected_generation=1, expected_metadata_revision=1)
    repo.commit(replace(record, generation=2), expected_generation=1, actor='owner')
    assert repo.finish_backup(job, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    historical = repo.versions()[0]
    assert historical.metadata_revision == 2
    assert historical.backup_metadata_revision == 1
    assert historical.backup_manifest_key == f'bundle-index/v1/owner/{RUN_ID}/1/1.json'
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.backup_metadata_revision == 0


def test_permanent_backup_failure_and_attempts_survive_restart(tmp_path: Path, record: LocalRecord) -> None:
    """Retry counts and permanent backup failure survive recovery."""
    now = [0.0]
    db = Database(tmp_path, clock=lambda: now[0])
    repo = LocalRepository(db)
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    job = repo.claim_backup('first')
    assert job is not None
    assert job.attempts == 1
    assert repo.finish_backup(job, error='temporary', retry_seconds=1)
    now[0] = 2
    job = repo.claim_backup('second')
    assert job is not None
    assert job.attempts == 2
    assert repo.finish_backup(job, error='corrupt archive', blocked=True)
    repo.recover_jobs()
    assert repo.claim_backup('third') is None
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.backup_state == 'blocked'
    assert db.connection.execute('SELECT active FROM local_backup_jobs').fetchone()[0] == 1


@pytest.mark.parametrize(
    'manifest_key',
    [
        None,
        '',
        'manifest-1',
        f'bundle-index/v1/foreign/{RUN_ID}/1/1.json',
        f'bundle-index/v1/owner/{RUN_ID}/2/1.json',
        f'bundle-index/v1/owner/{RUN_ID}/1/2.json',
    ],
)
def test_successful_backup_requires_exact_closing_manifest(
    tmp_path: Path, manifest_key: str | None, record: LocalRecord
) -> None:
    """Backup completion requires the exact owner, generation and metadata manifest key."""
    repo = LocalRepository(Database(tmp_path))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    job = repo.claim_backup('worker')
    assert job is not None
    before = repo.get(RUN_ID)
    assert before is not None
    with pytest.raises(ViewerError, match='manifest'):
        repo.finish_backup(job, manifest_key=manifest_key)
    assert repo.get(RUN_ID) == before
    saved = repo.connection.execute('SELECT state,token FROM local_backup_jobs WHERE id=?', (job.id,)).fetchone()
    assert saved['state'] == 'uploading' and saved['token'] == job.token
    assert repo.finish_backup(job, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')


def test_blocked_generation_and_its_pending_metadata_do_not_starve_replacement(
    tmp_path: Path, record: LocalRecord
) -> None:
    """A blocked historical generation cannot prevent backup of its replacement."""
    repo = LocalRepository(Database(tmp_path))
    original = record
    repo.commit(original, expected_generation=None, actor='owner', activate_backup=True)
    old_job = repo.claim_backup('old-worker')
    assert old_job is not None
    repo.mutate_metadata(
        RUN_ID, {'display_name': 'pending old metadata'}, expected_generation=1, expected_metadata_revision=1
    )
    assert repo.finish_backup(old_job, error='missing original archive', blocked=True)
    assert repo.claim_backup('metadata-worker') is None
    repo.commit(replace(original, generation=2), expected_generation=1, actor='owner', activate_backup=True)
    replacement = repo.claim_backup('new-worker')
    assert replacement is not None
    assert replacement is not None
    assert replacement.generation == 2 and replacement.metadata_revision == 1
    assert repo.finish_backup(replacement, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/2/1.json')
    assert not repo.finish_backup(old_job, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.generation == 2
    stored_record = repo.get(RUN_ID)
    assert stored_record is not None
    assert stored_record.backup_manifest_key == f'bundle-index/v1/owner/{RUN_ID}/2/1.json'
    assert repo.claim_backup('metadata-worker') is None


def test_retrying_or_leased_generation_still_orders_replacement(tmp_path: Path, record: LocalRecord) -> None:
    """An active historical retry retains ordering until that generation is confirmed."""
    now = [0.0]
    repo = LocalRepository(Database(tmp_path, clock=lambda: now[0]))
    repo.commit(record, expected_generation=None, actor='owner', activate_backup=True)
    old_job = repo.claim_backup('old-worker')
    assert old_job is not None
    repo.commit(replace(record, generation=2), expected_generation=1, actor='owner', activate_backup=True)
    assert repo.claim_backup('new-worker') is None
    assert repo.finish_backup(old_job, error='temporary', retry_seconds=10)
    assert repo.claim_backup('new-worker') is None
    now[0] = 11
    retried = repo.claim_backup('retry-worker')
    assert retried is not None
    assert retried.generation == 1
    assert repo.finish_backup(retried, manifest_key=f'bundle-index/v1/owner/{RUN_ID}/1/1.json')
    claimed_job = repo.claim_backup('new-worker')
    assert claimed_job is not None
    assert claimed_job.generation == 2
