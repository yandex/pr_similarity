"""Corruption protection and cache eviction serialize per publication."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from test_local_service import require_record, service
from test_service import publish
from viewer.errors import ViewerError


def test_corruption_protection_prevents_concurrent_eviction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit protecting the last good copy finishes before eviction can inspect it."""
    now = [100.0]
    svc = service(tmp_path, clock=lambda: now[0])
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    svc.activate_backup(rid)
    assert svc.backups.run_once()
    row = require_record(svc, rid)
    assert row.local_path is not None
    descriptor = svc.bundles.read_bundle(svc._identity(row))

    entered, release, cleanup_started = Event(), Event(), Event()
    invalidate = svc.repository.invalidate_backup

    def paused_invalidation(run_id: str, generation: int, error: str) -> bool:
        """Pause after audit chose protection but before it changes the row."""
        entered.set()
        assert release.wait(5)
        return invalidate(run_id, generation, error)

    def cleanup() -> int:
        """Expose that the concurrent cleanup was scheduled."""
        cleanup_started.set()
        return svc.cleanup_cache()

    monkeypatch.setattr(svc.repository, 'invalidate_backup', paused_invalidation)
    now[0] = 111
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            audit = pool.submit(svc.audit._protect_corruption, descriptor, ViewerError('stored_digest_mismatch', 'bad'))
            try:
                assert entered.wait(5)
                eviction = pool.submit(cleanup)
                assert cleanup_started.wait(5)
                with pytest.raises(TimeoutError):
                    eviction.result(timeout=0.1)
            finally:
                release.set()
            audit.result(timeout=5)
            assert eviction.result(timeout=5) == 0
        assert Path(row.local_path).is_dir()
        assert not require_record(svc, rid).backup_valid
    finally:
        svc.close()
        svc.db.close()
