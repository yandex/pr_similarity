"""Administrative progress projects only active, safe operation data."""

from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_local_service import service
from test_service import publish
from viewer.config import Config
from viewer.local_service import LocalViewerService
from viewer.monitoring import ActivityTracker, activity_snapshot
from viewer.persistence.storage import MemoryStore
from viewer.web.api import create_app


def test_transfer_counts_unknown_length_and_releases_terminal_rows() -> None:
    """Track actual bytes without inventing a total and remove completed transfers."""
    now = [100.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    tracker.start('operation', run_id='run', actor='alice')
    tracker.update('operation', received_bytes=17, phase='validating')
    now[0] += 3
    row = tracker.snapshot()['operation']
    assert row['received_bytes'] == 17
    assert row['total_bytes'] is None
    assert row['elapsed_seconds'] == 3
    assert row['phase'] == 'validating'
    tracker.finish('operation')
    assert tracker.snapshot() == {}


def test_backup_activity_is_bounded_and_does_not_expose_job_credentials(tmp_path: Path) -> None:
    """Bound active rows while retaining backup identity and excluding credentials."""
    svc = service(tmp_path)
    try:
        report, _, _ = publish(svc, tmp_path)
        svc.config = replace(svc.config, admin_activity_limit=1)
        transfers = ActivityTracker()
        transfers.start('active-upload', run_id='new-run', actor='alice', total_bytes=100)
        transfers.update('active-upload', received_bytes=25)
        value = activity_snapshot(svc, transfers)
        assert value['total'] == 2 and value['limit'] == 1
        assert len(value['items']) == 1
        assert value['items'][0]['kind'] == 'upload'
        transfers.finish('active-upload')
        backup = activity_snapshot(svc, transfers)['items'][0]
        assert backup['run_id'] == report['run']['id']
        assert backup['kind'] == 'backup' and backup['status'] == 'pending'
        assert backup['archive_bytes'] > 0
        assert backup['received_bytes'] is None
        assert backup['elapsed_seconds'] is None
        assert not {'token', 'worker', 'local_path', 'grant_hash'} & backup.keys()
        svc.activate_backup(report['run']['id'])
        assert svc.backups.run_once()
        assert activity_snapshot(svc, transfers)['items'] == []
    finally:
        svc.close()
        svc.db.close()


def test_terminal_refresh_has_no_running_clock_and_retains_safe_active_detail(tmp_path: Path) -> None:
    """Omit terminal running time and retain only allowlisted active refresh fields."""
    svc = service(tmp_path)
    try:
        refresh = {
            'status': 'partial',
            'started_at': '2000-01-01T00:00:00Z',
            'active': [{'run_id': 'test', 'stage': 'download', 'bytes': 17, 'grant_hash': 'private'}],
        }
        row = activity_snapshot(svc, ActivityTracker(), {'refresh_job': refresh})['items'][0]
        assert row['elapsed_seconds'] is None
        assert row['active'] == [{'run_id': 'test', 'stage': 'download', 'bytes': 17}]
    finally:
        svc.close()
        svc.db.close()


def test_deleted_run_backup_uses_its_durable_identity(tmp_path: Path) -> None:
    """Describe a tombstone backup after its run disappears from the live catalogue."""
    svc = service(tmp_path)
    try:
        report, _, _ = publish(svc, tmp_path)
        rid = report['run']['id']
        svc.patch_metadata(rid, 'alice', {'display_name': 'Research run'})
        svc.activate_backup(rid)
        while svc.backups.run_once():
            pass
        basket = svc.runs[rid]['basket']['meta']['name']
        svc.delete(rid)
        rows = activity_snapshot(svc, ActivityTracker())['items']
        tombstone = next(row for row in rows if row.get('backup_kind') == 'tombstone')
        assert tombstone['name'] == 'Research run'
        assert tombstone['owner'] == 'alice' and tombstone['basket'] == basket
        assert tombstone['run_available'] is False
        assert tombstone['archive_bytes'] is None
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('requeue', [False, True])
def test_stats_after_refresh_of_unconfirmed_deletion_backup(tmp_path: Path, requeue: bool) -> None:
    """Keep stats readable when refresh reduces an unconfirmed deletion to its marker."""
    token = tmp_path / 'admin-token'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(
        runtime=tmp_path / 'runtime',
        admin_token_file=token,
        storage='mock',
        publication_storage='local',
        disk_reserve_bytes=0,
    )
    app = create_app(config, MemoryStore())
    svc = app.state.service
    assert isinstance(svc, LocalViewerService)
    svc.db.create_user('alice', 'secret')
    # Omit lifespan startup so the queued retry cannot race the synchronous audit.
    client = TestClient(app, raise_server_exceptions=False)
    try:
        report, _, _ = publish(svc, tmp_path)
        rid = report['run']['id']
        svc.patch_metadata(rid, 'alice', {'display_name': 'Research run'})
        svc.activate_backup(rid)
        while svc.backups.run_once():
            pass
        svc.delete(rid)
        job = svc.repository.claim_backup('test-worker')
        assert job is not None and job.kind == 'tombstone'
        svc._backup(job)
        assert svc.repository.finish_backup(job, error='storage_unavailable', retry_seconds=60)
        if requeue:
            assert svc.retry_backup(rid)['scheduled'] == 1
        assert svc.refresh()['status'] == 'succeeded'
        record = svc.repository.get(rid, include_deleted=True)
        assert record is not None and set(record.entry) == {'marker'}
        response = client.get('/api/v1/admin/stats', headers={'Authorization': 'Bearer ' + 'A' * 32})
        assert response.status_code == 200, response.text
        row = next(item for item in response.json()['activity']['items'] if item.get('backup_kind') == 'tombstone')
        assert row['status'] == ('pending' if requeue else 'retry_wait')
        assert row['name'] == 'Research run' and row['owner'] == 'alice'
        assert row['basket'] is None and row['archive_bytes'] is None
        assert row['run_available'] is False
    finally:
        client.close()
        svc.close()
        svc.db.close()
