"""Typed resumable stage state and consistency with saved document progress."""

import re
from datetime import datetime
from typing import Any

from basket_collector.attempts import AttemptJournal
from basket_collector.config import CollectConfig
from basket_collector.contracts import (
    BasketDocument,
    CollectCheckpoint,
    CollectDocument,
    ExportCheckpoint,
    FilterCheckpoint,
    FilterDocument,
    GenerateCheckpoint,
    TasksDocument,
)


def _validate_collect(document: CollectDocument, state: CollectCheckpoint) -> None:
    """Match frozen queues to configuration, discovery and saved record order."""
    config = CollectConfig.model_validate(document.meta.settings.get('collect'))
    numbers = state.numbers
    counts = document.meta.counts
    discovery = document.meta.discovery
    if state.phase in ('repository', 'discovery'):
        if (
            numbers
            or counts.input
            or discovery.matched
            or discovery.selected
            or document.tasks
            or document.rejected
            or document.failed
        ):
            raise ValueError('unfrozen checkpoint phase must have zero progress and no queue')
        if state.phase == 'repository' and (state.matches or state.next_page != 1):
            raise ValueError('repository checkpoint cannot contain discovery progress')
    else:
        if len(numbers) != counts.input or len(numbers) != discovery.selected:
            raise ValueError('checkpoint queue length differs from input or discovery selected')
        groups = [
            [record.change_request.number for record in document.tasks],
            [record.change_request.number for record in document.rejected],
            [record.identity.number for record in document.failed],
        ]
        observed: set[int] = set()
        for numbers_in_group in groups:
            selected = set(numbers_in_group)
            if numbers_in_group != [number for number in numbers if number in selected] or observed & selected:
                raise ValueError('checkpoint outcomes differ from frozen queue order')
            observed.update(selected)
        if counts.pending == 0 and observed != set(numbers):
            raise ValueError('completed record pass does not partition its queue')
        if state.phase == 'ready' and counts.pending:
            raise ValueError('ready checkpoint has pending PRs')
        if config.selection == 'change_ids':
            if numbers != config.change_ids or discovery.matched != len(numbers) or discovery.capped:
                raise ValueError('checkpoint queue differs from configured PR IDs')
        else:
            matched = len(state.matches)
            selected = [item[1] for item in state.matches[: config.max_changes]]
            if numbers != selected or discovery.matched != matched or discovery.capped != (len(selected) < matched):
                raise ValueError('checkpoint frozen queue differs from discovery matches or cap')
    if config.selection == 'change_ids':
        if state.matches or state.next_page != 1:
            raise ValueError('PR IDs checkpoint cannot contain paginated discovery state')
    else:
        assert config.merged_from is not None and config.merged_before is not None
        start = datetime.fromisoformat(config.merged_from.replace('Z', '+00:00'))
        end = datetime.fromisoformat(config.merged_before.replace('Z', '+00:00'))
        for stamp, _ in state.matches:
            assert isinstance(stamp, str)
            if not start <= datetime.fromisoformat(stamp.replace('Z', '+00:00')) < end:
                raise ValueError('checkpoint match falls outside collect date range')


def validate_checkpoint(
    document: CollectDocument | FilterDocument | TasksDocument | BasketDocument, state: dict[str, Any]
) -> dict[str, Any]:
    """Validate resume state before reading clients or writing work artifacts.

    Raises:
        ValueError: State fields, phase, queue or saved progress are inconsistent.

    """
    _validate_attempts(state)
    _validate_outcome_journals(document, state)
    if isinstance(document, CollectDocument):
        checkpoint = CollectCheckpoint.model_validate(state)
        _validate_collect(document, checkpoint)
        return checkpoint.model_dump(mode='json')
    if isinstance(document, FilterDocument):
        filtered = FilterCheckpoint.model_validate(state)
        if filtered.phase != 'local' and document.meta.counts.pending:
            raise ValueError('selection or ready checkpoint has pending PRs')
        return filtered.model_dump(mode='json')
    if isinstance(document, TasksDocument):
        generated = GenerateCheckpoint.model_validate(state)
        if generated.phase == 'ready' and document.meta.counts.pending:
            raise ValueError('ready checkpoint has pending tasks')
        return generated.model_dump(mode='json', exclude_unset=True)
    exported = ExportCheckpoint.model_validate(state)
    if document.meta.counts.pending:
        raise ValueError('ready checkpoint has pending basket records')
    return exported.model_dump(mode='json')


def _validate_attempts(state: dict[str, Any]) -> None:
    """Validate attempt journals before resuming or creating an external client."""
    attempts = state.get('attempts', {})
    if not isinstance(attempts, dict):
        raise ValueError('invalid attempt journals')
    for identity, value in attempts.items():
        if not isinstance(identity, str) or re.fullmatch(r'change:[0-9a-f]{64}', identity) is None:
            raise ValueError('invalid attempt identity')
        AttemptJournal.model_validate(value)
    retry_ids = state.get('retry_ids', [])
    if not isinstance(retry_ids, list) or len(retry_ids) != len(set(retry_ids)) or set(retry_ids) - set(attempts):
        raise ValueError('retry pass identities differ from attempt journals')


def _validate_outcome_journals(
    document: CollectDocument | FilterDocument | TasksDocument | BasketDocument,
    state: dict[str, Any],
) -> None:
    """Prevent saved terminal outcomes from contradicting their acknowledged operation history."""
    if isinstance(document, BasketDocument):
        return
    journals = state.get('attempts', {})
    outcomes = {item.id: 'succeeded' for item in document.tasks}
    outcomes.update({item.id: 'rejected' for item in document.rejected})
    if isinstance(document, CollectDocument):
        outcomes.update({item.identity.id: 'failed' for item in document.failed})
    else:
        outcomes.update({item.record.id: 'failed' for item in document.failed})
    reused = (
        {item.id for item in document.tasks if item.generation.reused_from}
        if isinstance(document, TasksDocument)
        else set()
    )
    for identity, outcome in outcomes.items():
        saved = journals.get(identity)
        if saved is None:
            if journals and identity not in reused:
                raise ValueError('saved outcome has no attempt journal')
            continue
        journal = AttemptJournal.model_validate(saved)
        if not journal.current.attempts or journal.current.attempts[-1].outcome != outcome:
            raise ValueError('saved outcome disagrees with attempt journal')
        if outcome == 'failed' and not journal.current.exhausted:
            raise ValueError('failed record has an unexhausted attempt budget')
