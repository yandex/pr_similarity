"""Refresh progress and HTTP callbacks share one reentrant state lock."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_local_service import service
from viewer.local_service import LocalViewerService


def test_refresh_progress_and_locked_stats_finish_together(tmp_path: Path) -> None:
    """Exercise the API lock boundary while an audit persists progress."""
    svc: LocalViewerService = service(tmp_path)
    assert svc.audit._lock is svc.db.lock

    def progress() -> None:
        """Persist repeated audit progress under the audit state lock."""
        svc.audit.request()
        for _ in range(100):
            svc.audit._save()

    def stats() -> None:
        """Use the same outer service lock as API.perform for admin stats."""
        for _ in range(100):
            with svc.lock:
                assert svc.cache_stats()['refresh_job']['status'] in ('idle', 'pending')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(progress), pool.submit(stats)]
        for future in futures:
            future.result(timeout=5)
    svc.close()
    svc.db.close()
