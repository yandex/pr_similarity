"""Transient recovery failures preserve pending uploads and maintenance progress."""

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import archive_fixture
from test_local_service import require_operation, service
from viewer.util import digest


@pytest.mark.parametrize('after_commit', [False, True])
def test_startup_recovery_retries_failed_rejection_and_keeps_housekeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    """A failed rejection does not strand its UUID or another interrupted upload."""
    svc = service(tmp_path)
    run_ids = []
    for number in range(2):
        archive = tmp_path / f'upload-{number}.tar.gz'
        rid = f'00000000-0000-4000-8000-{number:012d}'
        _, manifest = archive_fixture(archive, run_id=rid)
        svc.admit_publication(
            'alice', str(number), rid, digest(archive.read_bytes()), manifest['report_sha256'], overwrite=False
        )
        run_ids.append(rid)
    svc.initialize()
    svc.config = replace(svc.config, cache_cleanup_interval_seconds=1, backup_retry_seconds=1)
    original = svc.repository.set_operation
    failed = False
    cleaned = threading.Event()
    passes = []

    def fail_once(actor: str, key: str, fingerprint: str, operation: dict) -> None:
        """Fail the first durable rejection after startup has loaded pending work."""
        nonlocal failed
        if key == '0' and not failed:
            failed = True
            if after_commit:
                original(actor, key, fingerprint, operation)
            raise sqlite3.OperationalError('database or disk is full')
        original(actor, key, fingerprint, operation)

    def observe_cleanup() -> int:
        """Observe servicing while retry is pending and after it completes."""
        passes.append([require_operation(svc, 'alice', str(number))['phase'] for number in range(2)])
        if passes[-1] == ['rejected', 'rejected'] and not set(run_ids) & svc.busy_runs():
            cleaned.set()
        return 0

    monkeypatch.setattr(svc.repository, 'set_operation', fail_once)
    monkeypatch.setattr(svc, 'cleanup_cache', observe_cleanup)
    worker = threading.Thread(target=svc._maintain_local)
    try:
        worker.start()
        assert cleaned.wait(5), 'Maintenance must retry recovery after the database becomes writable'
        if not after_commit:
            assert ['receiving', 'rejected'] in passes
        assert not set(run_ids) & svc.busy_runs()
        assert not svc._recovery_operations
    finally:
        svc._stop.set()
        worker.join(timeout=5)
        svc.close()
        svc.db.close()
