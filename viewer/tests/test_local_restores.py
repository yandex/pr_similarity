"""Exercise physical queue removal and reader ownership without publication I/O."""

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from viewer.errors import ViewerError
from viewer.local_restores import RestoreIdentity, RestoreQueue, _RestoreJob


def identity(number: int) -> RestoreIdentity:
    """Build a distinct immutable identity for an opaque test payload."""
    return str(number), 1, str(number)


@pytest.fixture
def stalled() -> Iterator[tuple[RestoreQueue[int], threading.Event, list[int]]]:
    """Occupy two real executor workers until each test explicitly releases them."""
    release, entered = threading.Event(), threading.Event()
    started: list[int] = []
    lock = threading.Lock()

    def execute(number: int) -> None:
        """Block only running work and retain an observable execution order."""
        with lock:
            started.append(number)
            if len(started) >= 2:
                entered.set()
        assert release.wait(10)

    queue = RestoreQueue(2, 2, execute)
    tickets = [queue.ticket() for _ in range(2)]
    for number, ticket in enumerate(tickets):
        queue.join(ticket, identity(number), number)
    assert entered.wait(3)
    try:
        yield queue, release, started
    finally:
        release.set()
        for future in queue.futures():
            future.result(timeout=5)
        for ticket in tickets:
            queue.release(ticket)
        assert queue.close()


def test_repeated_cancel_never_submits_abandoned_work(
    stalled: tuple[RestoreQueue[int], threading.Event, list[int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated queued cancellations add no executor WorkItems while workers are stalled."""
    queue, _, started = stalled
    original = queue._executor.submit
    submissions = []

    def submit(function: Callable[[_RestoreJob[int]], None], job: _RestoreJob[int]) -> Future[None]:
        """Record any unexpected submission while the real workers remain occupied."""
        submissions.append(job.identity)
        return original(function, job)

    monkeypatch.setattr(queue._executor, 'submit', submit)
    for number in range(100):
        ticket = queue.ticket()
        future = queue.join(ticket, identity(100), number)
        assert queue.snapshot()['queued'] == 1
        queue.release(ticket)
        queue.release(ticket)
        assert future.cancelled()
        assert queue.snapshot() == {'active': 2, 'queued': 0, 'waiters': 2, 'capacity': 4}
    assert submissions == []
    assert sorted(started) == [0, 1]


def test_bound_and_singleflight(
    stalled: tuple[RestoreQueue[int], threading.Event, list[int]],
) -> None:
    """A full queue rejects another identity but admits readers sharing existing work."""
    queue, release, _ = stalled
    tickets = [queue.ticket(), queue.ticket()]
    futures = [queue.join(ticket, identity(n + 2), n + 2) for n, ticket in enumerate(tickets)]
    with pytest.raises(ViewerError) as busy:
        queue.join(queue.ticket(), identity(4), 4)
    assert busy.value.code == 'restore_busy'
    shared = queue.ticket()
    assert queue.join(shared, identity(2), 2) is futures[0]
    queue.release(tickets[0])
    assert not futures[0].cancelled()
    assert queue.snapshot()['waiters'] == 4
    all_futures = queue.futures()
    release.set()
    for future in all_futures:
        future.result(timeout=5)
    assert queue.snapshot()['active'] == queue.snapshot()['queued'] == 0
    queue.release(shared)
    queue.release(tickets[1])


def test_closed_ticket_never_admits_work() -> None:
    """Preparation finishing after cancellation cannot create a late registration."""
    queue: RestoreQueue[int] = RestoreQueue(1, 0, lambda value: None)
    ticket = queue.ticket()
    queue.release(ticket)
    with pytest.raises(ViewerError) as cancelled:
        queue.join(ticket, identity(1), 1)
    assert cancelled.value.code == 'read_cancelled'
    assert queue.snapshot()['active'] == 0
    assert queue.close()


def test_old_cancel_callback_cannot_block_or_cancel_replacement(
    stalled: tuple[RestoreQueue[int], threading.Event, list[int]],
) -> None:
    """A cancelled completion callback holds no queue lock and cannot affect a newer job."""
    queue, _, _ = stalled
    old_ticket = queue.ticket()
    old = queue.join(old_ticket, identity(3), 3)
    application_lock, entered = threading.Lock(), threading.Event()

    def callback(future: Future[None]) -> None:
        """Simulate a completion callback requiring an unrelated application lock."""
        entered.set()
        with application_lock:
            assert future.cancelled()

    old.add_done_callback(callback)
    fresh_ticket = queue.ticket()
    with ThreadPoolExecutor(max_workers=2) as pool:
        with application_lock:
            releasing = pool.submit(queue.release, old_ticket)
            assert entered.wait(3)
            admitting = pool.submit(queue.join, fresh_ticket, identity(3), 3)
            fresh = admitting.result(timeout=2)
            assert fresh is not old and not fresh.cancelled()
        releasing.result(timeout=3)
    queue.release(fresh_ticket)
    assert fresh.cancelled()


def test_error_releases_admission_and_completed_futures() -> None:
    """Failed work wakes all shared readers and frees its slot for a later attempt."""

    def fail(value: int) -> None:
        """Fail the restore callback without retaining the completed identity."""
        raise ViewerError('restore_failed', 'Injected restore failure.', 503)

    queue = RestoreQueue(1, 1, fail)
    try:
        for _ in range(3):
            ticket = queue.ticket()
            future = queue.join(ticket, identity(1), 1)
            with pytest.raises(ViewerError, match='Injected'):
                future.result(timeout=3)
            queue.release(ticket)
            assert queue.futures() == ()
            assert queue.snapshot()['active'] == queue.snapshot()['queued'] == 0
    finally:
        assert queue.close()


def test_shutdown_drops_queued_work_and_counts_running_work(
    stalled: tuple[RestoreQueue[int], threading.Event, list[int]],
) -> None:
    """Shutdown rejects pending work but reports active I/O until its callback exits."""
    queue, release, started = stalled
    queued = queue.join(queue.ticket(), identity(3), 3)
    assert queue.close() is False
    with pytest.raises(ViewerError, match='stopping'):
        queued.result(timeout=1)
    assert queue.snapshot()['active'] == 2 and queue.snapshot()['queued'] == 0
    with pytest.raises(ViewerError, match='stopping'):
        queue.join(queue.ticket(), identity(4), 4)
    release.set()
    for future in queue.futures():
        future.result(timeout=3)
    assert queue.close()
    assert sorted(started) == [0, 1]


def test_submit_failure_closes_admission_and_finishes_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Executor rejection cannot strand an admitted reader or keep accepting more jobs."""
    queue: RestoreQueue[int] = RestoreQueue(1, 1, lambda value: None)

    def failed_submit(function: Callable[[_RestoreJob[int]], None], job: _RestoreJob[int]) -> Future[None]:
        """Reject executor dispatch before any restore callback starts."""
        raise RuntimeError('Executor is unavailable')

    monkeypatch.setattr(queue._executor, 'submit', failed_submit)
    future = queue.join(queue.ticket(), identity(1), 1)
    with pytest.raises(ViewerError, match='unavailable'):
        future.result(timeout=1)
    assert queue.futures() == ()
    assert queue.close()


def test_orphaned_active_job_keeps_its_slot_until_execution_finishes() -> None:
    """The last reader cannot free capacity still occupied by active storage I/O."""
    entered, release = threading.Event(), threading.Event()

    def execute(value: int) -> None:
        """Hold a running restore after its only reader has left."""
        entered.set()
        assert release.wait(5)

    queue = RestoreQueue(1, 0, execute)
    ticket = queue.ticket()
    future = queue.join(ticket, identity(1), 1)
    try:
        assert entered.wait(3)
        assert future.running()
        assert not future.cancel()
        queue.release(ticket)
        assert queue.snapshot() == {'active': 1, 'queued': 0, 'waiters': 0, 'capacity': 1}
        with pytest.raises(ViewerError) as busy:
            queue.join(queue.ticket(), identity(2), 2)
        assert busy.value.status == 503 and busy.value.code == 'restore_busy'
    finally:
        release.set()
        future.result(timeout=3)
        assert queue.close()


def test_dispatch_error_after_start_cannot_release_running_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ambiguous submit failure keeps already-started storage work tracked until completion."""
    entered, release = threading.Event(), threading.Event()

    def execute(value: int) -> None:
        """Keep storage work active while its dispatch reports a failure."""
        entered.set()
        assert release.wait(5)

    queue = RestoreQueue(1, 0, execute)
    original = queue._executor.submit

    def submit(function: Callable[[_RestoreJob[int]], None], job: _RestoreJob[int]) -> Future[None]:
        """Start the real worker before simulating executor bookkeeping failure."""
        original(function, job)
        assert entered.wait(3)
        raise RuntimeError('Failure after dispatch')

    monkeypatch.setattr(queue._executor, 'submit', submit)
    future = queue.join(queue.ticket(), identity(1), 1)
    try:
        assert future.running() and not future.cancel()
        assert queue.snapshot()['active'] == 1
        assert queue.close() is False
    finally:
        release.set()
        future.result(timeout=3)
        assert queue.close()


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, SystemExit])
def test_process_control_failure_releases_the_job(error_type: type[BaseException]) -> None:
    """Even a process-control exception resolves the shared result and releases its slot."""

    def execute(value: int) -> None:
        """Raise the selected fixture interruption inside a worker."""
        raise error_type('Controlled interruption')

    queue = RestoreQueue(1, 0, execute)
    future = queue.join(queue.ticket(), identity(1), 1)
    try:
        with pytest.raises(error_type):
            future.result(timeout=3)
        assert queue.futures() == ()
        assert queue.snapshot()['active'] == 0
    finally:
        assert queue.close()


@pytest.mark.parametrize('close_first', [False, True])
def test_cancelled_pending_completion_releases_dispatch_slot(
    monkeypatch: pytest.MonkeyPatch,
    close_first: bool,
) -> None:
    """A cancelled pending completion cannot strand its slot or receive a second result."""
    executed: list[int] = []
    submitted: list[tuple[Callable[[_RestoreJob[int]], None], _RestoreJob[int]]] = []
    queue = RestoreQueue(1, 0, executed.append)

    def submit(function: Callable[[_RestoreJob[int]], None], job: _RestoreJob[int]) -> Future[None]:
        """Keep an assigned worker pending until its completion is explicitly cancelled."""
        submitted.append((function, job))
        return Future()

    monkeypatch.setattr(queue._executor, 'submit', submit)
    ticket = queue.ticket()
    future = queue.join(ticket, identity(1), 1)
    assert future.cancel()
    assert queue.snapshot()['active'] == 1
    if close_first:
        assert queue.close() is False
    function, job = submitted[0]
    function(job)
    queue.release(ticket)
    assert executed == []
    assert future.cancelled()
    assert queue.futures() == ()
    assert queue.close()
