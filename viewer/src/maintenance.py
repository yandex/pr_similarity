"""Single maintenance worker: bounded recovery and coalesced catalogue refresh."""

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from viewer.errors import StorageError, ViewerError
from viewer.util import utc_now

logger = logging.getLogger(__name__)


def _log_failure(operation: str, code: str, attempt: int, error: Exception) -> None:
    """Log failure type and frame locations without exception text, source lines or locals."""
    frames = ' -> '.join(
        f'{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}'
        for frame, line in traceback.walk_tb(error.__traceback__)
    )
    logger.warning(
        '%s_failed code=%s attempt=%s exception_type=%s frames=%s',
        operation,
        code,
        attempt,
        type(error).__name__,
        frames,
    )


class Maintenance:
    """Schedule background work without owning the application's database."""

    def __init__(
        self,
        recover: Callable[[], None],
        retry_seconds: int,
        attempts: int,
        refresh: Callable[[], bool] | None = None,
    ) -> None:
        """Initialize worker state, retry limits and application callbacks."""
        self.recover, self.refresh = recover, refresh
        self.retry_seconds, self.attempts = retry_seconds, attempts
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.stopping = False
        self.pending = False
        self.worker: threading.Thread | None = None
        self.state = {
            'status': 'idle',
            'attempts': 0,
            'error_code': None,
            'started_at': None,
            'finished_at': None,
            'last_success_at': None,
        }
        self.epoch = uuid.uuid4().hex
        self.requested = self.processed = 0
        self.refresh_state = {
            'status': 'idle',
            'request_id': None,
            'completed_id': None,
            'attempts': 0,
            'error_code': None,
            'started_at': None,
            'finished_at': None,
            'last_success_at': None,
        }

    def start(self) -> None:
        """Start the single worker once under the scheduling lock."""
        with self.condition:
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, name='viewer-maintenance', daemon=True)
                self.worker.start()

    def request_recovery(self) -> None:
        """Queue recovery unless shutdown has closed admission."""
        with self.condition:
            if not self.stopping:
                self.pending = True
                self.state = {
                    'status': 'pending',
                    'attempts': 0,
                    'error_code': None,
                    'started_at': None,
                    'finished_at': None,
                    'last_success_at': None,
                }
                self.condition.notify_all()

    def request_refresh(self) -> dict:
        """Queue a refresh and return its request identity and current status."""
        with self.condition:
            if self.stopping:
                raise ViewerError('service_stopping', 'Service is stopping.', 503)
            self.requested += 1
            self.refresh_state.update(status='pending', request_id=f'{self.epoch}:{self.requested}', error_code=None)
            self.condition.notify_all()
            return deepcopy(self.refresh_state)

    def status(self) -> dict:
        """Copy recovery status under the scheduling lock."""
        with self.condition:
            return deepcopy(self.state)

    def refresh_status(self) -> dict:
        """Copy refresh progress without exposing mutable worker state."""
        with self.condition:
            return deepcopy(self.refresh_state)

    def close(self, timeout: float | None = None) -> bool:
        """Request cancellation and report whether the worker joined within timeout."""
        with self.condition:
            self.stopping = True
            self.stop.set()
            self.condition.notify_all()
        if self.worker is not None:
            self.worker.join(timeout=timeout)
            if self.worker.is_alive():
                return False
        with self.condition:
            if self.processed < self.requested:
                self.refresh_state.update(status='cancelled', finished_at=utc_now(time.time()))
        return True

    def _recover(self) -> None:
        """Retry journal recovery within the configured attempt budget."""
        for attempt in range(1, self.attempts + 1):
            with self.condition:
                if self.stopping:
                    return
                self.state.update(
                    status='running',
                    attempts=attempt,
                    error_code=None,
                    started_at=utc_now(time.time()),
                    finished_at=None,
                )
            try:
                self.recover()
            except Exception as error:
                retry = isinstance(error, (StorageError, OSError)) and attempt < self.attempts
                code = getattr(error, 'code', 'recovery_failed')
                with self.condition:
                    self.state.update(
                        status='retrying' if retry else 'failed',
                        error_code=code,
                        finished_at=utc_now(time.time()),
                    )
                    self.condition.notify_all()
                    _log_failure('recovery', code, attempt, error)
                    if retry:
                        self.condition.wait_for(lambda: self.stopping, timeout=self.retry_seconds)
                if not retry:
                    return
            else:
                with self.condition:
                    self.state.update(
                        status='succeeded',
                        error_code=None,
                        finished_at=utc_now(time.time()),
                        last_success_at=utc_now(time.time()),
                    )
                    self.condition.notify_all()
                return

    def _refresh(self, target: int) -> None:
        """Build one refresh generation, retaining invalidated requests for retry."""
        for attempt in range(1, self.attempts + 1):
            with self.condition:
                if self.stopping:
                    return
                self.refresh_state.update(
                    status='running',
                    attempts=attempt,
                    error_code=None,
                    running_id=f'{self.epoch}:{target}',
                    started_at=utc_now(time.time()),
                    finished_at=None,
                )
            try:
                assert self.refresh is not None
                applied = self.refresh()
            except Exception as error:
                code = getattr(error, 'code', 'refresh_failed')
                retry = isinstance(error, (StorageError, OSError)) and attempt < self.attempts
                with self.condition:
                    if self.stopping:
                        return
                    self.refresh_state.update(
                        status='retrying' if retry else 'failed',
                        error_code=code,
                        finished_at=utc_now(time.time()),
                    )
                    _log_failure('refresh', code, attempt, error)
                    if retry:
                        self.condition.wait_for(lambda: self.stopping, timeout=self.retry_seconds)
                    else:
                        self.processed = target
                    self.condition.notify_all()
                if not retry:
                    return
            else:
                with self.condition:
                    if self.stopping:
                        return
                    if applied:
                        self.processed = target
                        self.refresh_state.update(
                            status='succeeded' if self.requested == target else 'pending',
                            completed_id=f'{self.epoch}:{target}',
                            error_code=None,
                            finished_at=utc_now(time.time()),
                            last_success_at=utc_now(time.time()),
                        )
                    else:
                        # Keep the request outstanding; writes invalidate only this attempt.
                        self.refresh_state.update(status='waiting_recovery' if self.pending else 'pending')
                        self.condition.wait_for(lambda: self.stopping or self.pending, timeout=self.retry_seconds)
                    self.condition.notify_all()
                return

    def _work(self) -> None:
        """Serialize recovery and refresh work until shutdown is requested."""
        while True:
            with self.condition:
                self.condition.wait_for(
                    lambda: self.stopping
                    or self.pending
                    or (
                        self.refresh is not None
                        and self.requested > self.processed
                        and self.state['status'] != 'failed'
                    )
                )
                if self.stopping:
                    return
                recovery = self.pending
                self.pending = False
                target = self.requested
                if self.state['status'] == 'failed' and target > self.processed:
                    self.refresh_state.update(status='waiting_recovery')
            if recovery:
                self._recover()
                with self.condition:
                    if self.state['status'] == 'failed' and self.requested > self.processed:
                        self.refresh_state.update(status='waiting_recovery')
            else:
                self._refresh(target)
