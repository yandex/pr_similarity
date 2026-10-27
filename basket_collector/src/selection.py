"""Deterministic candidate snapshots and complete generation-input fingerprints."""

from typing import Any

from basket_collector.config import CollectorConfig, TemplateSnapshot, effective_settings
from basket_collector.filters import apply_population
from basket_collector.models.artifacts import (
    CollectDocument,
    CollectFailure,
    CollectRecord,
    FilterDocument,
    FilterRecord,
    Omission,
    Producer,
    SelectionReceipt,
    SelectionSnapshot,
    TasksDocument,
)
from basket_collector.persistence.artifact_io import sha256, stage_sha256


def semantic_record(record: CollectRecord) -> dict[str, Any]:
    """Exclude execution timestamps while retaining complete frozen source and task context."""
    payload = record.model_dump(mode='json')
    if 'retrieval' in payload:
        payload['retrieval'] = dict(payload['retrieval'])
        payload['retrieval'].pop('checked_at', None)
    return payload


def generation_key(record: FilterRecord, settings: dict[str, Any], producer: Producer) -> str:
    """Identify exact reusable task content independently of retry counters and scheduling."""
    semantic_settings = {
        key: value
        for key, value in settings.items()
        if key
        not in {
            'max_retries',
            'retry_delay_seconds',
            'max_retry_delay_seconds',
            'workers',
            'requests_per_second',
            'request_timeout_seconds',
        }
    }
    return sha256(
        {
            'record': semantic_record(record),
            'generation': semantic_settings,
            'producer': producer.model_dump(mode='json'),
        }
    )


def omissions(document: CollectDocument | FilterDocument | TasksDocument) -> list[Omission]:
    """Project only exhausted failures and unsuccessful selected tasks into coverage."""
    result = []
    for failure in document.failed:
        identity = failure.identity.id if isinstance(failure, CollectFailure) else failure.record.id
        result.append(
            Omission(
                id=identity,
                stage=document.meta.stage,
                kind='failed',
                code=failure.error.code,
                result_sha256=sha256(failure),
            )
        )
    if isinstance(document, TasksDocument):
        for rejected in document.rejected:
            result.append(
                Omission(
                    id=rejected.id,
                    stage='generate',
                    kind='rejected',
                    code=rejected.reasons[0].code,
                    result_sha256=sha256(rejected),
                )
            )
    return result


def selection_receipt(snapshot: SelectionSnapshot) -> SelectionReceipt:
    """Copy the validated public binding without population-only implementation details."""
    return SelectionReceipt.model_validate(snapshot.model_dump(mode='json', include=set(SelectionReceipt.model_fields)))


def build_selection(
    document: FilterDocument,
    collected: CollectDocument,
    config: CollectorConfig,
    producer: Producer,
    template_snapshot: TemplateSnapshot | None,
) -> SelectionSnapshot:
    """Compute a proposal without applying it to generation or discarding accepted candidates."""
    selected, excluded = apply_population(document.tasks, config.filter)
    settings = effective_settings(config, 'generate', template_snapshot=template_snapshot)
    keys = {record.id: generation_key(record, settings, producer) for record in selected}
    upstream = [*omissions(collected), *omissions(document)]
    candidate_state = {
        'collect': stage_sha256(collected),
        'filter_settings': document.meta.settings,
        'candidates': [semantic_record(record) for record in document.tasks],
        'rejected': [record.model_dump(mode='json') for record in document.rejected],
        'failed': [record.model_dump(mode='json') for record in document.failed],
    }
    payload = {
        'schema_version': 3,
        'input_sha256': sha256(candidate_state),
        'fingerprint': sha256({'tasks': [[record.id, keys[record.id]] for record in selected]}),
        'selected_ids': [record.id for record in selected],
        'generation_keys': keys,
        'settings': {
            'percentile': config.filter.percentile.model_dump(mode='json'),
            'selection': config.filter.selection.model_dump(mode='json'),
        },
        'generation_settings_sha256': sha256(settings),
        'excluded': [record.model_dump(mode='json') for record in excluded],
        'omissions': [item.model_dump(mode='json') for item in upstream],
    }
    return SelectionSnapshot.model_validate({**payload, 'sha256': sha256(payload)})


def selected_records(document: FilterDocument) -> list[FilterRecord]:
    """Return the frozen selected order while keeping the complete candidate pool elsewhere."""
    if document.selection is None:
        raise ValueError('filter has no accepted selection snapshot')
    candidates = {record.id: record for record in document.tasks}
    return [candidates[identity] for identity in document.selection.selected_ids]
