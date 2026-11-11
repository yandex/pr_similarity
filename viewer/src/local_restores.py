"""Bound cold restoration work and detach readers without retaining executor queue entries."""

import logging
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from viewer.errors import ViewerError

type RestoreIdentity = tuple[str, int, str]


@dataclass(eq=False)
class _RestoreJob[Payload]:
    """Keep one shared result and its reader count for a selected immutable generation."""

    identity: RestoreIdentity
    payload: Payload
    result: Future[None] = field(default_factory=Future)
    waiters: int = 0
    executing: bool = False


@dataclass(eq=False)
class RestoreTicket[Payload]:
    """Let a caller release its registration even before background preparation returns."""

    owner: 'RestoreQueue[Payload]'
    job: _RestoreJob[Payload] | None = None
    closed: bool = False


class RestoreQueue[Payload]:
    """Admit a bounded set of active jobs and a physically removable external FIFO."""

    def __init__(self, workers: int, queue_limit: int, execute: Callable[[Payload], None]) -> None:
        """Reserve executor submissions for active slots and keep all extra work in the FIFO."""
        self.workers, self.queue_limit, self.execute = workers, queue_limit, execute
        self._lock = threading.Lock()
        self._jobs: dict[RestoreIdentity, _RestoreJob[Payload]] = {}
        self._active: dict[RestoreIdentity, _RestoreJob[Payload]] = {}
        self._queued: OrderedDict[RestoreIdentity, _RestoreJob[Payload]] = OrderedDict()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='viewer-restore')

    def ticket(self) -> RestoreTicket[Payload]:
        """Create caller-owned registration before any cancellable preparation await."""
        return RestoreTicket(self)

    def join(self, ticket: RestoreTicket[Payload], identity: RestoreIdentity, payload: Payload) -> Future[None]:
        """Join one generation or reject new work when every admission slot is occupied."""
        launch: list[_RestoreJob[Payload]] = []
        with self._lock:
            self._check_ticket(ticket)
            if self._closed:
                raise ViewerError('service_stopping', 'Restoration is stopping.', 503)
            job = self._jobs.get(identity)
            if job is None:
                if len(self._jobs) >= self.workers + self.queue_limit:
                    raise ViewerError('restore_busy', 'Cold restoration is busy; retry later.', 503)
                job = _RestoreJob(identity, payload)
                self._jobs[identity] = job
                if len(self._active) < self.workers:
                    self._active[identity] = job
                    launch.append(job)
                else:
                    self._queued[identity] = job
            job.waiters += 1
            ticket.job = job
        self._dispatch(launch)
        return job.result

    def _check_ticket(self, ticket: RestoreTicket[Payload]) -> None:
        """Reject closed or previously attached registrations while holding the state lock."""
        if ticket.owner is not self:
            raise ValueError('Restore ticket belongs to another queue.')
        if ticket.closed:
            raise ViewerError('read_cancelled', 'The reader no longer waits for restoration.', 503)
        if ticket.job is not None:
            raise ValueError('Restore ticket is already registered.')

    def release(self, ticket: RestoreTicket[Payload]) -> None:
        """Remove abandoned queued work while leaving dispatched work counted until completion."""
        cancel: Future[None] | None = None
        with self._lock:
            if ticket.owner is not self:
                raise ValueError('Restore ticket belongs to another queue.')
            if ticket.closed:
                return
            ticket.closed = True
            job, ticket.job = ticket.job, None
            if job is None:
                return
            job.waiters -= 1
            if job.waiters == 0 and self._queued.get(job.identity) is job:
                del self._queued[job.identity]
                del self._jobs[job.identity]
                cancel = job.result
        if cancel is not None:
            cancel.cancel()

    def _dispatch(self, jobs: list[_RestoreJob[Payload]]) -> None:
        """Submit only jobs already counted in active slots, without holding queue locks."""
        for job in jobs:
            try:
                self._executor.submit(self._run, job)
            except RuntimeError as error:
                logging.getLogger('viewer.restore').error(
                    'restore_dispatch_failed error_type=%s',
                    type(error).__name__,
                )
                self.close()
                self._finish(
                    job,
                    ViewerError('restore_unavailable', 'Restoration is unavailable.', 503),
                    only_if_unstarted=True,
                )

    def _detach(self, job: _RestoreJob[Payload]) -> list[_RestoreJob[Payload]]:
        """Remove an owned active job and reserve its available slots for queued successors."""
        del self._jobs[job.identity]
        del self._active[job.identity]
        launch: list[_RestoreJob[Payload]] = []
        while not self._closed and self._queued and len(self._active) < self.workers:
            identity, following = self._queued.popitem(last=False)
            self._active[identity] = following
            launch.append(following)
        return launch

    def _run(self, job: _RestoreJob[Payload]) -> None:
        """Skip abandoned dispatches and complete every started job outside application locks."""
        with self._lock:
            if self._jobs.get(job.identity) is not job:
                return
            skip = self._closed or job.waiters == 0
            closed = self._closed
            launch = self._detach(job) if skip else []
            if not skip:
                job.executing = True
        if skip:
            self._dispatch(launch)
            error = ViewerError('service_stopping', 'Restoration is stopping.', 503) if closed else None
            self._resolve(job, error, cancel=not closed)
            return
        if not job.result.set_running_or_notify_cancel():
            self._finish(job, None)
            return
        completed = False
        try:
            self.execute(job.payload)
            completed = True
        finally:
            self._finish(job, None if completed else sys.exception())

    def _finish(
        self,
        job: _RestoreJob[Payload],
        error: BaseException | None,
        *,
        only_if_unstarted: bool = False,
    ) -> None:
        """Release admission before waking readers; callbacks cannot invert application locks."""
        with self._lock:
            if self._jobs.get(job.identity) is not job or (only_if_unstarted and job.executing):
                return
            launch = self._detach(job)
        self._dispatch(launch)
        self._resolve(job, error)

    def _resolve(self, job: _RestoreJob[Payload], error: BaseException | None, *, cancel: bool = False) -> None:
        """Resolve completion outside locks without racing an explicit future cancellation."""
        if job.result.cancelled():
            return
        if cancel:
            job.result.cancel()
            return
        if not job.executing and not job.result.set_running_or_notify_cancel():
            return
        if error is None:
            job.result.set_result(None)
        else:
            job.result.set_exception(error)

    def snapshot(self) -> dict[str, int]:
        """Describe admitted work without exposing payloads or retaining completed futures."""
        with self._lock:
            return {
                'active': len(self._active),
                'queued': len(self._queued),
                'waiters': sum(job.waiters for job in self._jobs.values()),
                'capacity': self.workers + self.queue_limit,
            }

    def futures(self) -> tuple[Future[None], ...]:
        """Capture currently admitted results for bounded shutdown and test draining."""
        with self._lock:
            return tuple(job.result for job in self._jobs.values())

    def close(self) -> bool:
        """Reject new admissions and drop the external FIFO while active jobs finish normally."""
        with self._lock:
            self._closed = True
            queued = list(self._queued.values())
            self._queued.clear()
            for job in queued:
                del self._jobs[job.identity]
            idle = not self._active
        for job in queued:
            self._resolve(job, ViewerError('service_stopping', 'Restoration is stopping.', 503))
        self._executor.shutdown(wait=False, cancel_futures=False)
        return idle
