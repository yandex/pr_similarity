"""Metadata backups survive replacement of their snapshotted local source by refresh."""

from pathlib import Path

import pytest
import test_refresh_pending_metadata
from test_refresh_pending_metadata import RepairFixture, backup_jobs, record_for
from viewer.errors import ViewerError

pending_metadata = test_refresh_pending_metadata.pending_metadata


@pytest.mark.parametrize('damage', [None, 'missing', 'corrupt'])
def test_refresh_between_backup_source_selection_and_pin(
    pending_metadata: RepairFixture, monkeypatch: pytest.MonkeyPatch, damage: str | None
) -> None:
    """A retired source falls back to verified remote bytes without blocking later revisions."""
    service, remote, rid, publication = pending_metadata
    before = record_for(service, rid)
    assert before.local_path is not None
    old_path = Path(before.local_path)
    pin = service.files.pin
    refreshed = False

    def replace_before_pin(path: str | Path) -> None:
        """Run the full audit exactly after backup selected its old local copy."""
        nonlocal refreshed
        if Path(path) == old_path and not refreshed:
            refreshed = True
            result = service.refresh()
            assert result['status'] == 'succeeded'
            assert not old_path.exists()
            identity = service._identity(before)
            if damage == 'missing':
                remote.delete(identity.archive_key)
            elif damage == 'corrupt':
                remote.put(identity.archive_key, b'corrupt')
        pin(path)

    monkeypatch.setattr(service.files, 'pin', replace_before_pin)
    assert service.backups.run_once()
    assert refreshed
    after = record_for(service, rid)
    if damage is not None:
        assert after.backup_state == 'blocked'
        assert after.backup_metadata_revision == 1
        assert after.backup_manifest_key == before.backup_manifest_key
        assert service._identity(before).closing_key not in remote.objects
        assert not service.files.readers
        return
    assert after.backup_state == 'backed_up'
    assert after.backup_metadata_revision == 2
    assert not service.files.readers
    service.patch_metadata(rid, 'alice', {'display_name': 'Next revision'})
    assert service.backups.run_once()
    final = record_for(service, rid)
    assert final.backup_state == 'backed_up' and final.backup_metadata_revision == 3
    assert all(job['state'] == 'backed_up' for job in backup_jobs(service, rid))


def test_backup_does_not_hide_unrelated_pin_errors(
    pending_metadata: RepairFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a missing cache copy permits fallback; other pin failures stay visible."""
    service, remote, rid, publication = pending_metadata

    def denied(path: str | Path) -> None:
        """Inject a failure unrelated to cache retirement."""
        raise ViewerError('unsafe_path', 'Invalid local version path.')

    monkeypatch.setattr(service.files, 'pin', denied)
    assert service.backups.run_once()
    job = backup_jobs(service, rid)[-1]
    assert job['state'] == 'blocked'
    assert job['error'] == 'unsafe_path'
