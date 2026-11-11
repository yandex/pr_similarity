"""Bounded after-response backup workers with durable claims and shutdown fencing."""

import logging
import threading
import time
import uuid
from collections.abc import Callable

from viewer.config import Config
from viewer.errors import StorageError
from viewer.local_repository import BackupJob, LocalRepository


class BackupWorkers:
    """Run independent backup jobs without occupying HTTP or the audit worker."""

    def __init__(self, config: Config, repository: LocalRepository, execute: Callable[[BackupJob], str]) -> None:
        """Prepare bounded workers and a process-specific owner for durable job leases."""
        self.config, self.repository, self.execute = config, repository, execute
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.threads: list[threading.Thread] = []
        self.epoch = str(uuid.uuid4())

    def start(self) -> None:
        """Start each configured worker once and wake it to inspect eligible intents."""
        if self.threads:
            return
        for index in range(self.config.backup_workers):
            thread = threading.Thread(target=self._work, args=(index,), daemon=True, name=f'viewer-backup-{index}')
            self.threads.append(thread)
            thread.start()
        self.wake.set()

    def activate(self, run_id: str) -> None:
        """Activate committed intents after the response and wake waiting workers."""
        self.repository.activate_backup(run_id)
        self.wake.set()

    def run_once(self, worker: str | None = None) -> bool:
        """Claim one eligible intent; all completion paths retain its identity."""
        if self.stop.is_set():
            return False
        job = self.repository.claim_backup(worker or self.epoch, lease_seconds=60)
        if job is None:
            return False
        done = threading.Event()
        lost = threading.Event()

        def heartbeat() -> None:
            """Renew the exact lease until completion, recording ownership loss for fencing."""
            while not done.wait(10):
                if not self.repository.renew_backup(job, lease_seconds=60):
                    lost.set()
                    return

        renewal = threading.Thread(target=heartbeat, daemon=True, name='viewer-backup-lease')
        renewal.start()
        try:
            key = self.execute(job)
            if not lost.is_set():
                self.repository.finish_backup(job, manifest_key=key)
        except Exception as error:
            # Persist adapter failures against the fenced lease so the intent remains recoverable.
            code = getattr(error, 'code', 'backup_failed')
            retryable = (
                (isinstance(error, StorageError) and error.retryable)
                or isinstance(error, OSError)
                or code in ('transfer_timeout',)
            )
            logging.getLogger('viewer.backup').warning(
                'backup_failed run_id=%s generation=%s code=%s exception_type=%s',
                job.run_id,
                job.generation,
                code,
                type(error).__name__,
            )
            if not lost.is_set():
                self.repository.finish_backup(
                    job,
                    error=code,
                    retry_seconds=min(
                        3600, self.config.backup_retry_seconds * 2 ** min(getattr(job, 'attempts', 1), 7)
                    ),
                    blocked=not retryable,
                )
        finally:
            done.set()
            renewal.join(timeout=2)
        return True

    def _work(self, index: int) -> None:
        """Run claimed jobs until shutdown while retaining diagnostics for worker failures."""
        while not self.stop.is_set():
            try:
                worked = self.run_once(f'{self.epoch}:{index}')
            except Exception as error:
                # The durable lease remains for recovery after an unexpected worker failure.
                logging.getLogger('viewer.backup').error('backup_worker_failed exception_type=%s', type(error).__name__)
                worked = False
            if not worked:
                self.wake.wait(min(self.config.backup_retry_seconds, 1))
                self.wake.clear()

    def close(self, timeout: float) -> bool:
        """Wake stopped workers and join them within the caller's remaining shutdown budget."""
        self.stop.set()
        self.wake.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads)
