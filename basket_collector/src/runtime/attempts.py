"""Durable per-record attempt rounds with one retry owner and cooperative cancellation."""

import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, StrictFloat, StrictInt, field_validator, model_validator

from basket_collector.config import RetryPolicy
from basket_collector.models.artifacts import RejectedRecord, StageError, StrictModel, settings_sha256
from basket_collector.runtime.transport_scope import record_scope


def _now() -> str:
    """Timestamp an attempt independently of its monotonic process deadline."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def policy_hash(policy: RetryPolicy) -> str:
    """Identify only retry limits, not unrelated stage configuration."""
    return settings_sha256(
        {
            'max_retries': policy.max_retries,
            'retry_delay_seconds': policy.retry_delay_seconds,
            'max_retry_delay_seconds': policy.max_retry_delay_seconds,
        }
    )


class NeedsAttemptBudget(RuntimeError):
    """An interrupted last slot needs permission for another attempt round."""


class Attempt(StrictModel):
    """A reserved operation and its observed terminal outcome."""

    number: StrictInt = Field(gt=0)
    started_at: str
    finished_at: str | None = None
    outcome: Literal['running', 'succeeded', 'rejected', 'failed', 'interrupted'] = 'running'
    error: StageError | None = None

    @model_validator(mode='after')
    def check_outcome(self) -> 'Attempt':
        """Keep unknown outcomes distinct from proven record failures."""
        if (self.outcome == 'running') != (self.finished_at is None):
            raise ValueError('attempt completion time disagrees with outcome')
        if (self.outcome == 'failed') != (self.error is not None):
            raise ValueError('only a failed attempt carries a record error')
        return self


class AttemptRound(StrictModel):
    """The finite budget of one explicitly started processing pass."""

    number: StrictInt = Field(gt=0)
    limit: StrictInt = Field(gt=0)
    attempts: list[Attempt] = Field(default_factory=list)
    not_before: StrictFloat = Field(default=0.0, ge=0)
    warm_bytes: StrictInt = Field(default=0, ge=0)
    cold_bytes: StrictInt = Field(default=0, ge=0)
    request_count: StrictInt = Field(default=0, ge=0)

    @property
    def exhausted(self) -> bool:
        """Count reserved calls, including unknown outcomes after a crash."""
        return len(self.attempts) >= self.limit

    @model_validator(mode='after')
    def check_attempts(self) -> 'AttemptRound':
        """Reject lost reservations, excess calls and calls after success."""
        if [attempt.number for attempt in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError('attempt numbers must be contiguous')
        if len(self.attempts) > self.limit:
            raise ValueError('attempt budget exceeded')
        if any(attempt.outcome not in {'failed', 'interrupted'} for attempt in self.attempts[:-1]):
            raise ValueError('attempt follows an unfinished or successful operation')
        return self


class AttemptJournal(StrictModel):
    """Bind every retry pass to the same frozen record input and policy."""

    schema_version: Literal[3]
    input_sha256: str
    policy_sha256: str
    reused_from: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    rounds: list[AttemptRound] = Field(min_length=1)

    @field_validator('schema_version', mode='before')
    @classmethod
    def check_version(cls, value: object) -> int:
        """Require explicit integer v3 without coercions."""
        if type(value) is not int or value != 3:
            raise ValueError('attempt journal requires integer schema_version 3')
        return value

    @field_validator('input_sha256', 'policy_sha256')
    @classmethod
    def check_hash(cls, value: str) -> str:
        """Require canonical input and policy digests."""
        if re.fullmatch(r'[0-9a-f]{64}', value) is None:
            raise ValueError('invalid attempt journal digest')
        return value

    @model_validator(mode='after')
    def check_rounds(self) -> 'AttemptJournal':
        """Reject a new pass before the preceding budget was exhausted."""
        if [item.number for item in self.rounds] != list(range(1, len(self.rounds) + 1)):
            raise ValueError('round numbers must be contiguous')
        for previous in self.rounds[:-1]:
            if not previous.exhausted or previous.attempts[-1].outcome not in {'failed', 'interrupted'}:
                raise ValueError('retry round has no exhausted predecessor')
        return self

    @property
    def current(self) -> AttemptRound:
        """Return the only pass that can accept a new operation."""
        return self.rounds[-1]

    @classmethod
    def new(cls, input_sha256: str, policy: RetryPolicy) -> 'AttemptJournal':
        """Create the first pass before any external operation starts."""
        return cls(
            schema_version=3,
            input_sha256=input_sha256,
            policy_sha256=policy_hash(policy),
            rounds=[AttemptRound(number=1, limit=policy.max_retries + 1)],
        )

    def reserve(self) -> Attempt:
        """Reserve a slot that must be persisted before launching work."""
        if self.current.exhausted:
            raise NeedsAttemptBudget('Record needs a new attempt budget; use RETRY_FAILED=1.')
        if self.current.attempts and self.current.attempts[-1].outcome not in {'failed', 'interrupted'}:
            raise ValueError('cannot reserve after running or completed record')
        attempt = Attempt(number=len(self.current.attempts) + 1, started_at=_now())
        self.current.attempts.append(attempt)
        return attempt

    def retry(self, policy: RetryPolicy, input_sha256: str) -> None:
        """Open an authorized pass without discarding prior evidence."""
        if self.input_sha256 != input_sha256 or self.policy_sha256 != policy_hash(policy):
            raise ValueError('attempt input or policy changed')
        last = self.current.attempts[-1] if self.current.attempts else None
        if not self.current.exhausted or last is None or last.outcome not in {'failed', 'interrupted'}:
            raise ValueError('only an exhausted failed or interrupted record can retry')
        self.rounds.append(AttemptRound(number=len(self.rounds) + 1, limit=policy.max_retries + 1))


@dataclass(frozen=True)
class RecordResult[T]:
    """A verified value or an exhausted nonfatal failure for the coordinator."""

    value: T | None = None
    error: StageError | None = None
    journal: AttemptJournal | None = None
    needs_budget: bool = False


class AttemptRunner:
    """Execute one record with persisted reservations and cooperative backoff."""

    def __init__(
        self,
        policy: RetryPolicy,
        stop: threading.Event,
        save: Callable[[AttemptJournal], None],
        classify: Callable[[Exception], StageError | None],
        interruptions: tuple[type[Exception], ...],
        *,
        wait: Callable[[float], object] | None = None,
        observe: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the durable callback and controlled external error boundary."""
        if not interruptions:
            raise ValueError('an interruption type is required')
        self.policy = policy
        self.stop = stop
        self.save = save
        self.classify = classify
        self.interruptions = interruptions
        self.wait = wait if wait is not None else stop.wait
        self.observe = observe

    def _check_stop(self) -> None:
        """Leave the record pending before another external action."""
        if self.stop.is_set():
            raise self.interruptions[0]()

    def _finish(
        self, journal: AttemptJournal, outcome: str, error: StageError | None = None, *, persist: bool = True
    ) -> None:
        """Persist an outcome before returning or scheduling another attempt."""
        attempt = journal.current.attempts[-1]
        journal.current.attempts[-1] = Attempt.model_validate(
            {**attempt.model_dump(), 'outcome': outcome, 'finished_at': _now(), 'error': error}
        )
        if persist:
            self.save(journal)

    def _backoff(self, journal: AttemptJournal, error: Exception) -> None:
        """Persist the next allowed launch time, honoring server cooldown hints."""
        bound = self.policy.retry_delay_seconds
        for _ in range(len(journal.current.attempts) - 1):
            bound = min(self.policy.max_retry_delay_seconds, bound * 2)
            if bound == self.policy.max_retry_delay_seconds:
                break
        delay = random.SystemRandom().uniform(bound / 2, bound)
        supplied = getattr(error, 'retry_after', None)
        if isinstance(supplied, (int, float)) and 0 <= supplied < float('inf'):
            delay = max(delay, supplied)
        journal.current.not_before = time.time() + delay
        self.save(journal)
        if self.observe is not None:
            self.observe(
                f'retry attempt={len(journal.current.attempts) + 1}/{journal.current.limit} delay={delay:.3f}s'
            )

    def _wait_ready(self, journal: AttemptJournal) -> None:
        """Resume saved backoff instead of resetting its policy on restart."""
        self._check_stop()
        remaining = max(0.0, journal.current.not_before - time.time())
        if remaining:
            self.wait(remaining)
            self._check_stop()
        if journal.current.not_before:
            journal.current.not_before = 0.0
            self.save(journal)

    def run[T](self, journal: AttemptJournal, operation: Callable[[], T]) -> RecordResult[T]:
        """Process a record until success, rejection, exhausted failure or interruption.

        The save callback acknowledges durable state before returning. Fatal
        exceptions propagate after recording an unknown/interrupted outcome.
        """
        if journal.policy_sha256 != policy_hash(self.policy):
            raise ValueError('record retry policy changed')
        if any(item.limit != self.policy.max_retries + 1 for item in journal.rounds):
            raise ValueError('record attempt budget disagrees with retry policy')
        if journal.current.attempts and journal.current.attempts[-1].outcome == 'running':
            self._finish(journal, 'interrupted')
        while True:
            self._check_stop()
            if journal.current.exhausted:
                last = journal.current.attempts[-1]
                if last.outcome == 'failed':
                    return RecordResult(error=last.error, journal=journal)
                raise NeedsAttemptBudget('Record needs a new attempt budget; use RETRY_FAILED=1.')
            self._wait_ready(journal)
            journal.reserve()
            self.save(journal)
            try:
                self._check_stop()
                with record_scope():
                    value = operation()
            except self.interruptions:
                self._finish(journal, 'interrupted')
                raise
            except Exception as error:
                failure = self.classify(error)
                if failure is None:
                    self._finish(journal, 'interrupted')
                    raise
                self._finish(journal, 'failed', failure, persist=False)
                if journal.current.exhausted:
                    self.save(journal)
                    return RecordResult(error=failure, journal=journal)
                self._backoff(journal, error)
            else:
                self._finish(journal, 'rejected' if isinstance(value, RejectedRecord) else 'succeeded', persist=False)
                return RecordResult(value=value, journal=journal)
