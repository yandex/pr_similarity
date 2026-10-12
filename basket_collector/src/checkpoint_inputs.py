"""Cross-stage checkpoint validation against verified predecessor snapshots."""

import hashlib
from typing import Any

from basket_collector.config import FilterConfig
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    FilterRecord,
    TasksDocument,
)
from basket_collector.stages.export import export_tasks
from basket_collector.storage import StorageError, receipt


def _check_partition(
    source_ids: list[str], groups: list[list[str]], complete: bool, first_order: list[str] | None = None
) -> None:
    """Require ordered subsets, and a full partition for completed progress."""
    observed: set[str] = set()
    for index, identities in enumerate(groups):
        selected = set(identities)
        order = first_order if index == 0 and first_order is not None else source_ids
        if identities != [identity for identity in order if identity in selected]:
            raise ValueError('saved record identities differ from predecessor order or population')
        if observed & selected:
            raise ValueError('saved outcome partitions overlap')
        observed.update(selected)
    if complete and observed != set(source_ids):
        raise ValueError('ready checkpoint does not partition the complete predecessor population')


def _check_filter(document: FilterDocument, source: CollectDocument, state: dict[str, Any] | None) -> None:
    """Verify the accepted and rejected records retain their collected source."""
    inputs = {record.id: record for record in source.tasks}
    if document.meta.counts.input != len(inputs):
        raise ValueError('filter input count differs from collected predecessor')
    complete = state is None or state['phase'] in ('selection', 'ready')
    accepted_order = list(inputs)
    if state is None or state['phase'] == 'ready':
        config = FilterConfig.model_validate(document.meta.settings.get('filter'))
        if config.selection.shuffle:
            seed = config.selection.seed
            accepted_order.sort(
                key=lambda identity: (hashlib.sha256(f'{seed}\n{identity}'.encode()).hexdigest(), identity)
            )
    _check_partition(
        list(inputs),
        [[record.id for record in document.tasks], [record.id for record in document.rejected]],
        complete,
        accepted_order,
    )
    for record in document.tasks:
        original = inputs[record.id]
        if record.change_request != original.change_request or record.revisions.source != original.revisions.source:
            raise ValueError('accepted record source differs from collected predecessor')
    for rejected in document.rejected:
        original_pr = inputs[rejected.id].change_request.model_dump(
            mode='json', include=set(type(rejected.change_request).model_fields)
        )
        if rejected.change_request.model_dump(mode='json') != original_pr:
            raise ValueError('rejected record source differs from collected predecessor')


def _check_generate(document: TasksDocument, source: FilterDocument, state: dict[str, Any] | None) -> None:
    """Verify task outcomes and retry identities retain their filtered source."""
    inputs = {record.id: record for record in source.tasks}
    if document.meta.counts.input != len(inputs):
        raise ValueError('generation input count differs from filtered predecessor')
    complete = state is None or state.get('phase') == 'ready'
    _check_partition(
        list(inputs),
        [[record.id for record in document.tasks], [record.record.id for record in document.failed]],
        complete,
    )
    for record in document.tasks:
        saved = record.model_dump(mode='json', include=set(FilterRecord.model_fields))
        if saved != inputs[record.id].model_dump(mode='json'):
            raise ValueError('task source differs from filtered predecessor')
    for failed in document.failed:
        if failed.record != inputs[failed.record.id]:
            raise ValueError('failed task source differs from filtered predecessor')
    if state is not None and set(state.get('retry_attempts', {})) - set(inputs):
        raise ValueError('retry_attempts PR is absent from generation input')


def validate_checkpoint_input(
    document: CollectDocument | FilterDocument | TasksDocument | BasketDocument,
    source: CollectDocument | FilterDocument | TasksDocument | None,
    state: dict[str, Any] | None,
) -> None:
    """Reject edited stage progress before clients, resume or publication.

    Raises:
        StorageError: Source identity, outcomes or projection disagree with input.

    """
    if source is None:
        if isinstance(document, CollectDocument):
            return
        raise StorageError('invalid checkpoint input: missing predecessor')
    try:
        if document.meta.repository != source.meta.repository:
            raise ValueError('repository differs from predecessor')
        if document.meta.lineage != [*source.meta.lineage, receipt(source)]:
            raise ValueError('lineage differs from verified predecessor')
        if isinstance(document, FilterDocument) and isinstance(source, CollectDocument):
            _check_filter(document, source, state)
        elif isinstance(document, TasksDocument) and isinstance(source, FilterDocument):
            _check_generate(document, source, state)
        elif isinstance(document, BasketDocument) and isinstance(source, TasksDocument):
            dataset = {
                'name': document.meta.name,
                'description': document.meta.description,
                'maintainers': document.meta.maintainers,
            }
            if dataset != document.meta.settings.get('dataset'):
                raise ValueError('basket dataset metadata differs from frozen settings')
            expected = export_tasks(source, document.meta)
            if document.tasks != expected.tasks:
                raise ValueError('basket records differ from successful task projection')
        else:
            raise ValueError('stage document and predecessor types disagree')
    except ValueError as exc:
        raise StorageError(f'invalid checkpoint input: {exc}; use OVERWRITE=1') from exc
