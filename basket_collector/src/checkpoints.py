"""Typed resumable stage state and consistency with saved document progress."""

import re
from datetime import datetime
from typing import Any

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
        if numbers or counts.input or discovery.matched or discovery.selected or document.tasks or document.rejected:
            raise ValueError('unfrozen checkpoint phase must have zero progress and no queue')
        if state.phase == 'repository' and (state.matches or state.next_page != 1):
            raise ValueError('repository checkpoint cannot contain discovery progress')
    else:
        if len(numbers) != counts.input or len(numbers) != discovery.selected:
            raise ValueError('checkpoint queue length differs from input or discovery selected')
        saved_numbers = [record.change_request.number for record in document.tasks]
        rejected_numbers = [record.change_request.number for record in document.rejected]
        saved_set, rejected_set = set(saved_numbers), set(rejected_numbers)
        prefix = numbers[: len(saved_numbers) + len(rejected_numbers)]
        if (
            saved_set & rejected_set
            or saved_set | rejected_set != set(prefix)
            or saved_numbers != [number for number in prefix if number in saved_set]
            or rejected_numbers != [number for number in prefix if number in rejected_set]
        ):
            raise ValueError('checkpoint saved PRs must follow the frozen queue order')
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
        if any(re.fullmatch(r'change:[0-9a-f]{64}', identity) is None for identity in generated.retry_attempts):
            raise ValueError('retry_attempts contains an invalid change identity')
        if generated.phase == 'ready' and document.meta.counts.pending:
            raise ValueError('ready checkpoint has pending tasks')
        return generated.model_dump(mode='json', exclude_unset=True)
    exported = ExportCheckpoint.model_validate(state)
    if document.meta.counts.pending:
        raise ValueError('ready checkpoint has pending basket records')
    return exported.model_dump(mode='json')
