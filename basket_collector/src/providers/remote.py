"""Hosting-service interfaces expose normalized facts, never native API payloads."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from basket_collector.models.artifacts import CollectRecord, Issue, RejectedRecord
from basket_collector.models.sources import Repository


class RemoteError(RuntimeError):
    """Preserve the cause and bounded recovery semantics of a hosting operation."""

    def __init__(
        self, message: str, *, code: str = 'source_error', retryable: bool = False, retry_after: float | None = None
    ) -> None:
        """Store a safe user-facing error without retaining credentials or payload bodies."""
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class ChangeRejected(RemoteError):
    """Carry a candidate-local exclusion without treating API failures as skips."""

    def __init__(self, record: RejectedRecord) -> None:
        """Preserve the validated identity and safe reason for durable collection."""
        reason = record.reasons[0]
        super().__init__(reason.message, code=reason.code)
        self.record = record


class RemoteInterrupted(RemoteError):
    """The caller stopped external operations before the frozen queue was complete."""


@dataclass(frozen=True)
class Candidate:
    """Supply only the metadata needed for deterministic merged-change discovery."""

    number: int
    merged_at: str
    target_branch: str


class RemoteVCS(Protocol):
    """Describe provider-independent read operations used by the collector."""

    def resolve_repository(self, locator: str) -> Repository:
        """Resolve one stable project identity and its current default branch."""
        ...

    def list_merged_candidates(
        self,
        repository: Repository,
        *,
        start_page: int = 1,
        merged_from: str | None = None,
        merged_before: str | None = None,
    ) -> Iterator[tuple[int, list[Candidate]]]:
        """Yield complete numbered discovery pages in the provider's bounded order."""
        ...

    def get_change_request(self, repository: Repository, number: int) -> CollectRecord:
        """Return a normalized merged request and its frozen source descriptor."""
        ...

    def get_commits(self, record: CollectRecord) -> list[str]:
        """Return the complete source commit list for history validation."""
        ...

    def get_closing_issues(self, record: CollectRecord) -> list[Issue]:
        """Return accessible linked issue snapshots with global opaque identities."""
        ...

    def close(self) -> None:
        """Release only the resources owned by this reader."""
        ...


def utc_time(value: object) -> str:
    """Normalize an aware provider timestamp before shared discovery or persistence."""
    if not isinstance(value, str):
        raise RemoteError('Source returned an invalid timestamp.', code='invalid_source_metadata')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamp has no timezone')
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except ValueError as exc:
        raise RemoteError('Source returned an invalid timestamp.', code='invalid_source_metadata') from exc
