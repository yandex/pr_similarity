"""Coordinate durable worker reservations through a single checkpoint writer."""

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from basket_collector.config import RetryPolicy
from basket_collector.models.artifacts import StageError
from basket_collector.runtime.attempts import AttemptJournal, AttemptRunner, NeedsAttemptBudget, RecordResult


@dataclass
class _Reservation:
    """A worker waits for acknowledgement before beginning an external operation."""

    identity: str
    journal: AttemptJournal
    acknowledged: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class RecordRuntime:
    """Keep workers off checkpoint files and drain acknowledgements even after disk failures."""

    def __init__(
        self,
        policy: RetryPolicy,
        state: dict[str, Any],
        stop: threading.Event,
        persist: Callable[[], None],
        classify: Callable[[Exception], StageError | None],
        interruptions: tuple[type[Exception], ...],
        *,
        retry_pending: bool = False,
        observe: Callable[[str, AttemptJournal], None] | None = None,
    ) -> None:
        """Bind one stage pass to its coordinator-owned state and storage callback."""
        self.policy = policy
        self.state = state
        self.stop = stop
        self.persist = persist
        self.classify = classify
        self.interruptions = interruptions
        self.retry_pending = retry_pending
        self.observe = observe
        self.observed: set[tuple[str, int, int]] = set()
        self.requests: queue.Queue[_Reservation] = queue.Queue()
        self.failure: BaseException | None = None
        state.setdefault('attempts', {})

    def poll(self) -> None:
        """Acknowledge every request without abandoning blocked workers on write failure."""
        while True:
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                return
            try:
                if self.failure is not None:
                    raise self.failure
                self.state['attempts'][request.identity] = request.journal.model_dump(mode='json')
                self.persist()
                current = request.journal.current
                marker = (request.identity, current.number, len(current.attempts))
                if current.attempts and current.attempts[-1].outcome == 'failed' and marker not in self.observed:
                    self.observed.add(marker)
                    if self.observe is not None:
                        self.observe(request.identity, request.journal)
            except BaseException as error:
                self.failure = error
                request.error = error
                self.stop.set()
            finally:
                request.acknowledged.set()

    def save(self, identity: str, journal: AttemptJournal) -> None:
        """Block only the worker until the coordinator has durably saved the reservation."""
        request = _Reservation(identity, journal.model_copy(deep=True))
        self.requests.put(request)
        request.acknowledged.wait()
        if request.error is not None:
            raise request.error

    def run[T](self, identity: str, input_sha256: str, operation: Callable[[AttemptJournal], T]) -> RecordResult[T]:
        """Resume the existing pass; only the coordinator may authorize a new round."""
        saved = self.state['attempts'].get(identity)
        journal = (
            AttemptJournal.model_validate(saved) if saved is not None else AttemptJournal.new(input_sha256, self.policy)
        )
        if journal.input_sha256 != input_sha256:
            raise ValueError('record attempt input changed')
        runner = AttemptRunner(
            self.policy, self.stop, lambda value: self.save(identity, value), self.classify, self.interruptions
        )
        try:
            return runner.run(journal, lambda: operation(journal))
        except NeedsAttemptBudget:
            if not self.retry_pending:
                return RecordResult(journal=journal, needs_budget=True)
            journal.retry(self.policy, input_sha256)
            self.save(identity, journal)
            return runner.run(journal, lambda: operation(journal))

    def accept(self, identity: str, result: RecordResult[Any]) -> None:
        """Attach the terminal journal to the same checkpoint as the corresponding outcome."""
        if result.journal is None:
            raise ValueError('record result has no attempt journal')
        self.state['attempts'][identity] = result.journal.model_dump(mode='json')

    def retry(self, identities: list[str]) -> None:
        """Open one explicit retry pass before any worker starts using it."""
        for identity in identities:
            journal = AttemptJournal.model_validate(self.state['attempts'][identity])
            journal.retry(self.policy, journal.input_sha256)
            self.state['attempts'][identity] = journal.model_dump(mode='json')
        self.state['retry_ids'] = identities
