"""Persist record attempt budgets before side effects and survive interrupted passes."""

import threading
from pathlib import Path

import pytest

from basket_collector.config import RetryPolicy
from basket_collector.models.artifacts import StageError
from basket_collector.persistence.storage import atomic_write, read_json
from basket_collector.runtime.attempts import AttemptJournal, AttemptRunner, NeedsAttemptBudget, RecordResult


class TransportFailure(RuntimeError):
    """Represent an expected nonfatal failure at a controlled transport boundary."""


class Interrupted(RuntimeError):
    """Interrupt one record without declaring its input invalid."""


def classify(error: Exception) -> StageError | None:
    """Retry only controlled external errors, never internal defects or storage failures."""
    if isinstance(error, TransportFailure):
        return StageError(code='transport_error', message='external operation failed', retryable=True)
    return None


def test_persisted_attempts_bound_failures_before_retry(tmp_path: Path) -> None:
    """Reserve each call durably and stop precisely after the configured record budget."""
    path = tmp_path / 'journal.json'
    policy = RetryPolicy(max_retries=2, retry_delay_seconds=10, max_retry_delay_seconds=12)
    journal = AttemptJournal.new('a' * 64, policy)
    calls: list[int] = []
    waits: list[float] = []

    def save(value: AttemptJournal) -> None:
        """Use the same atomic writer as durable coordinator checkpoints."""
        atomic_write(path, value.model_dump(mode='json'))

    def fail() -> str:
        """Observe the persisted reservation before the external action fails."""
        stored = AttemptJournal.model_validate(read_json(path))
        assert stored.current.attempts[-1].outcome == 'running'
        calls.append(len(stored.current.attempts))
        raise TransportFailure('fixture')

    runner = AttemptRunner(policy, threading.Event(), save, classify, (Interrupted,), wait=waits.append)
    result = runner.run(journal, fail)
    assert isinstance(result, RecordResult) and result.error is not None
    assert calls == [1, 2, 3] and len(waits) == 2
    assert 5 <= waits[0] <= 10 and 6 <= waits[1] <= 12
    assert journal.current.attempts[-1].outcome == 'failed'
    assert journal.current.exhausted


def test_resume_does_not_reset_a_crashed_attempt_budget() -> None:
    """An already launched unknown attempt consumes its slot on ordinary resume."""
    policy = RetryPolicy(max_retries=0)
    journal = AttemptJournal.new('a' * 64, policy)
    journal.reserve()
    restored = AttemptJournal.model_validate(journal.model_dump(mode='json'))
    calls: list[int] = []
    runner = AttemptRunner(policy, threading.Event(), lambda value: None, classify, (Interrupted,))
    with pytest.raises(NeedsAttemptBudget):
        runner.run(restored, lambda: calls.append(1))
    assert calls == [] and restored.current.attempts[-1].outcome == 'interrupted'
    restored.retry(policy, 'a' * 64)
    result = runner.run(restored, lambda: 'success')
    assert result.value == 'success' and result.error is None
    assert len(restored.rounds) == 2


def test_retry_failure_and_fatal_exception_have_different_outcomes() -> None:
    """Preserve failed history across explicit retry and propagate programming defects."""
    policy = RetryPolicy(max_retries=0)
    journal = AttemptJournal.new('a' * 64, policy)
    runner = AttemptRunner(policy, threading.Event(), lambda value: None, classify, (Interrupted,))

    def fail() -> str:
        """Fail at an expected external operation."""
        raise TransportFailure('fixture')

    assert runner.run(journal, fail).error is not None
    journal.retry(policy, 'a' * 64)

    def broken() -> str:
        """Expose an implementation defect that must stop the stage immediately."""
        raise TypeError('programming defect')

    with pytest.raises(TypeError, match='programming defect'):
        runner.run(journal, broken)
    assert journal.rounds[0].attempts[-1].outcome == 'failed'
    assert journal.current.attempts[-1].outcome == 'interrupted'


def test_stop_during_backoff_keeps_record_pending() -> None:
    """Cancellation never spends another attempt or turns a pending record into failed."""
    policy = RetryPolicy(max_retries=3)
    stop = threading.Event()
    journal = AttemptJournal.new('a' * 64, policy)
    calls: list[int] = []

    def fail() -> str:
        """Trigger one backoff after a controlled external failure."""
        calls.append(1)
        raise TransportFailure('fixture')

    def cancel(delay: float) -> None:
        """Request cancellation instead of waiting for the whole delay."""
        stop.set()

    runner = AttemptRunner(policy, stop, lambda value: None, classify, (Interrupted,), wait=cancel)
    with pytest.raises(Interrupted):
        runner.run(journal, fail)
    assert calls == [1] and not journal.current.exhausted


def test_success_is_committed_together_with_its_record() -> None:
    """Leave the persisted reservation recoverable until the coordinator saves the result."""
    policy = RetryPolicy(max_retries=0)
    journal = AttemptJournal.new('a' * 64, policy)
    saved: list[dict[str, object]] = []
    runner = AttemptRunner(
        policy, threading.Event(), lambda value: saved.append(value.model_dump()), classify, (Interrupted,)
    )
    result = runner.run(journal, lambda: 'checked result')
    assert result.value == 'checked result' and result.journal is journal
    assert result.journal is not None
    assert result.journal.current.attempts[-1].outcome == 'succeeded'
    assert AttemptJournal.model_validate(saved[-1]).current.attempts[-1].outcome == 'running'


def test_failed_attempt_and_backoff_are_saved_together() -> None:
    """A restart cannot observe a retryable failure without its server cooldown."""
    policy = RetryPolicy(max_retries=1)
    journal = AttemptJournal.new('a' * 64, policy)
    saved: list[AttemptJournal] = []
    stop = threading.Event()

    def save(value: AttemptJournal) -> None:
        """Capture independent durable snapshots for the crash boundary assertion."""
        saved.append(value.model_copy(deep=True))

    def fail() -> str:
        """Exercise the nonterminal failure persistence boundary."""
        raise TransportFailure('fixture')

    runner = AttemptRunner(policy, stop, save, classify, (Interrupted,), wait=lambda delay: stop.set())
    with pytest.raises(Interrupted):
        runner.run(journal, fail)
    failures = [item for item in saved if item.current.attempts[-1].outcome == 'failed']
    assert failures and all(item.current.not_before > 0 for item in failures)


def test_restored_budget_must_match_policy() -> None:
    """Reject a modified limit even if the saved policy hash is still correct."""
    policy = RetryPolicy(max_retries=1)
    journal = AttemptJournal.new('a' * 64, policy)
    journal.current.limit = 100
    runner = AttemptRunner(policy, threading.Event(), lambda value: None, classify, (Interrupted,))
    with pytest.raises(ValueError, match='budget disagrees'):
        runner.run(journal, lambda: 'must not run')
    assert journal.current.attempts == []
