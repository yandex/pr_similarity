"""Exercise worker acknowledgement and checkpoint failures through the real thread boundary."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from basket_collector.config import RetryPolicy
from basket_collector.models.artifacts import StageError
from basket_collector.runtime.attempts import AttemptJournal
from basket_collector.runtime.record_runtime import RecordRuntime


class Interrupted(RuntimeError):
    """Signal cooperative cancellation in the runtime fixture."""


def classify(error: Exception) -> StageError | None:
    """Treat fixture exceptions as fatal unless the test specifically handles them."""
    return None


def test_external_operation_waits_for_durable_reservation() -> None:
    """The coordinator must persist before a worker performs the observable side effect."""
    state = {}
    saved = []
    calls = []
    runtime = RecordRuntime(
        RetryPolicy(max_retries=0),
        state,
        threading.Event(),
        lambda: saved.append(state['attempts']['record']['rounds'][0]['attempts'][0]['outcome']),
        classify,
        (Interrupted,),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, 'record', 'a' * 64, lambda journal: calls.append(len(saved)))
        while not future.done():
            runtime.poll()
            threading.Event().wait(0.001)
        result = future.result()
    assert calls == [1] and saved == ['running']
    assert state['attempts']['record']['rounds'][0]['attempts'][0]['outcome'] == 'running'
    runtime.accept('record', result)
    assert state['attempts']['record']['rounds'][0]['attempts'][0]['outcome'] == 'succeeded'


def test_storage_failure_releases_all_workers_without_external_calls() -> None:
    """A failed acknowledgement must not deadlock shutdown or authorize unrecorded calls."""
    stop = threading.Event()
    calls = []

    def fail() -> None:
        """Inject a fatal disk failure at the reservation boundary."""
        raise OSError('disk full')

    runtime = RecordRuntime(RetryPolicy(max_retries=0), {}, stop, fail, classify, (Interrupted,))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(runtime.run, str(index), 'a' * 64, lambda journal: calls.append(1)) for index in range(2)
        ]
        while not all(future.done() for future in futures):
            runtime.poll()
            threading.Event().wait(0.001)
        for future in futures:
            with pytest.raises((OSError, Interrupted)):
                future.result()
    assert stop.is_set() and calls == [] and isinstance(runtime.failure, OSError)


@pytest.mark.parametrize('retry_pending', [False, True])
def test_unknown_last_slot_requires_explicit_new_budget(retry_pending: bool) -> None:
    """A crashed last attempt stays pending until RETRY_FAILED authorizes another round."""
    policy = RetryPolicy(max_retries=0)
    journal = AttemptJournal.new('a' * 64, policy)
    journal.reserve()
    state = {'attempts': {'record': journal.model_dump(mode='json')}}
    calls = []
    runtime = RecordRuntime(
        policy, state, threading.Event(), lambda: None, classify, (Interrupted,), retry_pending=retry_pending
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, 'record', 'a' * 64, lambda journal: calls.append(1))
        while not future.done():
            runtime.poll()
            threading.Event().wait(0.001)
        result = future.result()
    assert calls == ([1] if retry_pending else [])
    assert result.needs_budget is not retry_pending
    assert result.journal is not None
    assert len(result.journal.rounds) == (2 if retry_pending else 1)
