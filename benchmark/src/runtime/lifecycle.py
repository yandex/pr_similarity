"""Cancellation intent and native retry policy without artifact selection."""

import threading
from types import FrameType
from typing import Literal, Protocol

from benchmark.models.report import ReportError, TaskDraft
from benchmark.models.state import StateTaskItem


class RetryPolicy(Protocol):
    """Read configured retry controls without sharing configuration ownership."""

    @property
    def categories(self) -> list[str]:
        """Return the configured native retry triggers."""
        ...

    @property
    def max_retries(self) -> int:
        """Return the maximum automatic native retries per invocation."""
        ...


class Stop(threading.Event):
    """Signal handlers change intent only; workers own process termination."""

    def __init__(self) -> None:
        """Keep user signal identity independent of a non-signal fatal stop."""
        super().__init__()
        self.signal_number: int | None = None
        self.interrupt_count = 0

    def signal(self, number: int, _frame: FrameType | None) -> None:
        """Record cancellation intent once; repeated signals only request a notice."""
        self.interrupt_count += 1
        if self.signal_number is None:
            self.signal_number = number

    def is_set(self) -> bool:
        """Observe signal intent without taking an Event lock inside the handler."""
        return self.signal_number is not None or super().is_set()

    def observed_interrupts(self) -> int:
        """Wake Event waiters from the observer, outside Python's signal handler."""
        if self.signal_number is not None:
            super().set()
        return self.interrupt_count


def retry_reasons(row: TaskDraft, categories: list[str]) -> list[str]:
    """Select independent retry triggers without treating interrupt as failure."""
    if row.processing_status == 'interrupted':
        return []
    result = []
    if row.infrastructure_error is not None and row.infrastructure_error.stage not in (
        'score',
        'telemetry',
        'pricing',
        'evidence',
    ):
        result.append('infrastructure_error')
    timeout = row.execution.status == 'timeout'
    empty = row.changes.status == 'empty'
    for key in categories:
        if (
            (key == 'timeout_no_changes' and timeout and empty)
            or (key == 'timeout' and timeout)
            or (key == 'no_changes' and empty)
            or (key == 'agent_error' and row.execution.status == 'error' and row.infrastructure_error is None)
        ):
            result.append(key)
    return result


def needs_host_score(selected: StateTaskItem, row: TaskDraft) -> bool:
    """Require sealed source before planning or executing recovery of a missing host score."""
    return (
        selected.seal is not None
        and row.artifacts.triplets_manifest is not None
        and row.scoring.status != 'available'
        and row.execution.status in ('completed', 'timeout', 'error', 'cancelled')
    )


def native_policy(
    row: TaskDraft, retries: RetryPolicy, retry_index: int, *, fatal: bool, scoring_error: ReportError | None
) -> tuple[Literal['retry_pending', 'interrupted', 'completed'], list[str]]:
    """Choose scheduling status from native facts while keeping host failure independent."""
    reasons = retry_reasons(row, retries.categories)
    if fatal:
        return 'retry_pending', reasons
    if scoring_error is not None and row.artifacts.triplets_manifest is not None:
        return 'interrupted', []
    if row.processing_status == 'interrupted' or row.execution.status == 'cancelled':
        return 'interrupted', []
    if reasons and retry_index < retries.max_retries:
        return 'retry_pending', reasons
    return 'completed', reasons
