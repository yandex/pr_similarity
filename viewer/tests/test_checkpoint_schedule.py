"""Checkpoint deadlines survive restarts, including before the first attempt."""

from pathlib import Path

import pytest
from test_local_service import service
from viewer.persistence.storage import MemoryStore


@pytest.mark.parametrize('completed', [False, True])
def test_restart_keeps_checkpoint_deadline(tmp_path: Path, completed: bool) -> None:
    """Restarts neither postpone initial backup nor forget a completed attempt."""
    now = [100.0]
    remote = MemoryStore()
    svc = service(tmp_path, remote, lambda: now[0])
    interval = svc.config.checkpoint_interval_seconds
    if completed:
        svc._create_checkpoint()
        assert svc.checkpoint_job['status'] == 'succeeded'
    svc.close()
    svc.db.close()
    for offset in (interval / 2, interval + 1):
        now[0] = 100 + offset
        svc = service(tmp_path, remote, lambda: now[0])
        try:
            assert svc._last_checkpoint == 100.0
            assert (now[0] - svc._last_checkpoint >= interval) == (offset >= interval)
        finally:
            svc.close()
            svc.db.close()


def test_overdue_checkpoint_runs_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Maintenance executes an overdue checkpoint on the first pass after restart."""
    now = [100.0]
    remote = MemoryStore()
    svc = service(tmp_path, remote, lambda: now[0])
    svc._create_checkpoint()
    svc.checkpoint_job.pop('schedule_origin', None)
    svc.repository.save_refresh('checkpoint-active', svc.checkpoint_job)
    svc.close()
    svc.db.close()
    now[0] += 86401
    svc = service(tmp_path, remote, lambda: now[0])
    waits = iter((False, True))
    monkeypatch.setattr(svc._stop, 'wait', lambda timeout: next(waits))
    monkeypatch.setattr(svc, 'request_checkpoint', svc._create_checkpoint)
    try:
        svc._maintain_local()
        assert svc.checkpoint_job['status'] == 'succeeded'
        assert svc._last_checkpoint == now[0]
        assert len(list(remote.list('checkpoints/v1/'))) == 2
    finally:
        svc.close()
        svc.db.close()
