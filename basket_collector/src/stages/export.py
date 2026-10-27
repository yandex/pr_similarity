"""Project completed tasks into the standalone benchmark basket contract."""

import posixpath

from basket_collector.models.artifacts import (
    BasketChangeRequest,
    BasketChanges,
    BasketDocument,
    BasketMeta,
    BasketRecord,
    BasketRevisions,
    IssueRef,
    Provenance,
    TaskRecord,
    TasksDocument,
    format_agent_task,
)
from basket_collector.persistence.storage import expected_input, receipt
from basket_collector.selection import omissions


def project_record(record: TaskRecord, *, include_golden_diff: bool) -> BasketRecord:
    """Project one successful task into the frozen benchmark basket format.

    Args:
        record: Completed task and its source provenance.
        include_golden_diff: Whether to retain the reference patch for scoring.

    Returns:
        Self-contained basket record ready for the benchmark.

    """
    resolved = record.revisions.resolved
    parents = [posixpath.dirname(file.path) for file in record.changes.files]
    workspace = (posixpath.commonpath(parents) or '.') if parents else '.'
    fields: dict[str, object] = {
        'id': record.id,
        'change_request': BasketChangeRequest(
            kind=record.change_request.kind,
            repository=record.change_request.repository,
            number=record.change_request.number,
            url=record.change_request.url,
            title=record.change_request.title,
            author=record.change_request.author,
            merged_at=record.change_request.merged_at,
        ),
        'revisions': BasketRevisions(
            policy=resolved.policy,
            before=resolved.before,
            after=resolved.after,
            before_tree=resolved.before_tree,
            after_tree=resolved.after_tree,
        ),
        'source': record.source,
        'diff_policy': 'git_source_changes_v1',
        'changes': BasketChanges(files=record.changes.files, stats=record.changes.stats),
        'workspace': workspace,
        'task': record.task,
        'agent_task': format_agent_task(record.task),
        'provenance': Provenance(
            issue_refs=[IssueRef(id=issue.id, url=issue.url) for issue in record.issues],
            generation=record.generation,
            retrieval=record.retrieval,
            source_revisions=record.revisions.source,
        ),
    }
    if include_golden_diff:
        fields['golden_diff'] = record.changes.diff
    return BasketRecord.model_validate(fields)


def export_tasks(tasks: TasksDocument, meta: BasketMeta) -> BasketDocument:
    """Export successful tasks; failed IDs remain visible in basket metadata."""
    if tasks.meta.status != 'completed':
        raise ValueError('tasks stage must be completed before export')
    if meta.input != expected_input(tasks):
        raise ValueError('basket input does not match tasks document')
    if meta.lineage != [*tasks.meta.lineage, receipt(tasks)]:
        raise ValueError('basket lineage does not match tasks document')
    if meta.repository != tasks.meta.repository or meta.producer != tasks.meta.producer:
        raise ValueError('basket source repository or producer differs from tasks document')
    successful = tasks.tasks
    if not successful:
        raise ValueError('cannot export empty basket')
    if [item for item in meta.omissions if item.stage == 'generate'] != omissions(tasks):
        raise ValueError('basket generation omissions do not match task outcomes')
    if meta.selection != tasks.selection or meta.coverage.generate != tasks.meta.counts:
        raise ValueError('basket selection or coverage differs from tasks')
    include_golden_diff = meta.settings.get('include_golden_diff')
    if type(include_golden_diff) is not bool:
        raise ValueError('export settings need include_golden_diff boolean')
    if meta.counts.succeeded != len(successful) or meta.counts.input != len(successful):
        raise ValueError('export counts do not match successful tasks')
    return BasketDocument(
        meta=meta,
        tasks=[project_record(record, include_golden_diff=include_golden_diff) for record in successful],
    )
