"""Enrich one collected PR; the coordinator owns scheduling and checkpoints."""

from threading import Event

from basket_collector.config import FilterConfig
from basket_collector.filters import check_issues, check_local, compact_rejection
from basket_collector.models.artifacts import (
    CollectRecord,
    FilterRecord,
    FilterRevisions,
    RejectedRecord,
    RejectionReason,
)
from basket_collector.models.sources import Repository
from basket_collector.providers.remote import RemoteVCS
from basket_collector.vcs.base import VCS
from basket_collector.vcs.git import RejectedChange


class SourceChangedError(RuntimeError):
    """The live PR revision identity differs from the collected snapshot."""


class FilterInterrupted(RuntimeError):
    """The caller should leave this PR pending after graceful shutdown."""


def _check_stop(stop_event: Event | None) -> None:
    """Leave the PR pending when shutdown is requested."""
    if stop_event is not None and stop_event.is_set():
        raise FilterInterrupted('filter interrupted before next external operation')


def _verify_source(record: CollectRecord, live: CollectRecord) -> None:
    """Reject moved source identity before resolving or generating a task."""
    if record.id != live.id or record.source != live.source or record.revisions.source != live.revisions.source:
        raise SourceChangedError('source_changed: saved change revisions differ; rebuild collect')


def process_one(
    record: CollectRecord,
    repository: VCS,
    remote: RemoteVCS,
    config: FilterConfig,
    stop_event: Event | None = None,
    *,
    hosting_repository: Repository,
) -> FilterRecord | RejectedRecord:
    """Apply per-PR checks in the specified order without mutating input."""
    source = record.revisions.source
    try:
        _check_stop(stop_event)
        _verify_source(record, remote.get_change_request(hosting_repository, record.change_request.number))
        _check_stop(stop_event)
        api_commits = remote.get_commits(record)
        _check_stop(stop_event)
        repository.fetch_commit(source.head_sha, record.source.fetch_refs)
        _check_stop(stop_event)
        repository.fetch_commit(source.base_sha, record.source.fetch_refs)
        _check_stop(stop_event)
        resolved = repository.resolve_revisions(source, api_commits, record.source.fetch_refs)
        _check_stop(stop_event)
        changes = repository.read_changes(resolved)
        reason = check_local(changes, config)
        if reason is not None:
            return compact_rejection(record, reason, changes.stats)
        _check_stop(stop_event)
        issues = remote.get_closing_issues(record)
        reason = check_issues(issues, config.issues)
        if reason is not None:
            return compact_rejection(record, reason, changes.stats)
        return FilterRecord(
            id=record.id,
            change_request=record.change_request,
            revisions=FilterRevisions(source=record.revisions.source, resolved=resolved),
            changes=changes,
            issues=issues,
            source=record.source,
            retrieval=repository.verify_retrieval(record.source, resolved),
        )
    except RejectedChange as exc:
        reason = RejectionReason(code=exc.code, message=exc.message, observed=exc.observed, expected=exc.expected)
        return compact_rejection(record, reason)
