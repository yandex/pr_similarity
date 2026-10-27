"""Validate exported accounting against the exact immutable predecessor chain."""

from basket_collector.models.artifacts import BasketDocument, CollectDocument, Coverage, FilterDocument, TasksDocument
from basket_collector.persistence.artifact_io import StorageError, stage_sha256
from basket_collector.selection import omissions


def validate_export_accounting(
    basket: BasketDocument, collected: CollectDocument, filtered: FilterDocument, tasks: TasksDocument
) -> None:
    """Reject self-consistent but edited accounting before reuse or publication."""
    predecessors = (collected, filtered, tasks)
    actual = [(item.stage, item.sha256) for item in basket.meta.lineage]
    expected = [(item.meta.stage, stage_sha256(item)) for item in predecessors]
    if actual != expected:
        raise StorageError('invalid export checkpoint: accounting predecessors differ from frozen lineage')
    coverage = Coverage(
        discovery=collected.meta.discovery,
        collect=collected.meta.counts,
        filter=filtered.meta.counts,
        selected=len(tasks.selection.selected_ids),
        generate=tasks.meta.counts,
    )
    if basket.meta.coverage != coverage:
        raise StorageError('invalid export checkpoint: coverage differs from frozen predecessors')
    omitted = [*omissions(collected), *omissions(filtered), *omissions(tasks)]
    if basket.meta.omissions != omitted or basket.meta.partial != bool(omitted):
        raise StorageError('invalid export checkpoint: omissions differ from frozen predecessors')
