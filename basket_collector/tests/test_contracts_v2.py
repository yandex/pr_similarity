"""Validate per-stage failures and complete-population coverage in the v2 wire contract."""

from typing import Any

import pytest
from basket_collector.contracts import BasketDocument, CollectDocument, FilterDocument, SelectionSnapshot
from basket_collector.storage import sha256
from fixture_helpers import task_identity
from pydantic import ValidationError
from test_contracts import _basket, _collect, _filter


def selection(identity: str) -> dict[str, Any]:
    """Build a complete independently hashed one-task selection snapshot."""
    data: dict[str, Any] = {
        'schema_version': 2,
        'input_sha256': 'a' * 64,
        'selected_ids': [identity],
        'generation_keys': {identity: 'd' * 64},
        'fingerprint': sha256({'tasks': [[identity, 'd' * 64]]}),
        'settings': {},
        'generation_settings_sha256': sha256({}),
        'excluded': [],
        'omissions': [],
    }
    return {**data, 'sha256': sha256(data)}


def test_collect_failure_needs_no_invented_source_snapshot() -> None:
    """A failed API lookup retains discovery identity and completes its record pass."""
    data = _collect()
    data['meta']['schema_version'] = 2
    row = data['tasks'].pop()
    data['meta']['counts'].update(succeeded=0, failed=1)
    data['failed'] = [
        {
            'identity': {
                'id': row['id'],
                **{key: row['change_request'][key] for key in ('kind', 'repository', 'number')},
            },
            'attempts': 4,
            'error': {'code': 'source_error', 'message': 'request failed', 'retryable': True},
        }
    ]
    document = CollectDocument.model_validate(data)
    assert document.meta.status == 'completed' and document.meta.counts.failed == 1
    assert not document.tasks and not hasattr(document.failed[0], 'record')


def test_filter_keeps_candidates_separate_from_selection() -> None:
    """Selection stores an explicit fingerprint while accepted records remain intact."""
    data = _filter()
    data['meta']['schema_version'] = 2
    data['selection'] = selection(data['tasks'][0]['id'])
    document = FilterDocument.model_validate(data)
    assert document.meta.counts.succeeded == len(document.tasks) == 1
    data['selection']['generation_keys'][data['tasks'][0]['id']] = 'e' * 64
    with pytest.raises(ValidationError, match='fingerprint'):
        FilterDocument.model_validate(data)


def test_selection_rejects_edited_snapshot_contents() -> None:
    """A valid task fingerprint cannot conceal changed population settings."""
    data = selection(task_identity(1))
    SelectionSnapshot.model_validate(data)
    data['settings']['limit'] = 99
    with pytest.raises(ValidationError, match='digest'):
        SelectionSnapshot.model_validate(data)


@pytest.mark.parametrize('version', [None, 1, True, 2.0, '2'])
def test_selection_requires_explicit_integer_version(version: object) -> None:
    """Apply strict v2 version validation to independently stored selections too."""
    data = selection(task_identity(1))
    if version is None:
        del data['schema_version']
    else:
        data['schema_version'] = version
    data['sha256'] = sha256({key: value for key, value in data.items() if key != 'sha256'})
    with pytest.raises(ValidationError):
        SelectionSnapshot.model_validate(data)


def test_upstream_failed_makes_complete_generation_export_partial() -> None:
    """Do not hide lost population coverage behind a fully generated selected sample."""
    data = _basket()
    meta = data['meta']
    meta['schema_version'] = 2
    meta['selection'] = {
        key: value
        for key, value in selection(data['tasks'][0]['id']).items()
        if key not in {'settings', 'generation_settings_sha256', 'excluded', 'omissions'}
    }
    data['tasks'][0]['provenance']['generation']['input_sha256'] = 'd' * 64
    meta['omissions'] = [
        {
            'id': task_identity(2),
            'stage': 'collect',
            'kind': 'failed',
            'code': 'source_error',
            'result_sha256': 'c' * 64,
        }
    ]
    complete = {'input': 1, 'succeeded': 1, 'rejected': 0, 'failed': 0, 'pending': 0}
    meta['coverage'] = {
        'discovery': {'matched': 2, 'selected': 2, 'capped': False},
        'collect': {**complete, 'input': 2, 'failed': 1},
        'filter': complete,
        'selected': 1,
        'generate': complete,
    }
    meta['partial'] = True
    document = BasketDocument.model_validate(data)
    assert document.meta.partial and document.meta.coverage.generate.failed == 0
    meta['partial'] = False
    with pytest.raises(ValidationError, match='partial'):
        BasketDocument.model_validate(data)


@pytest.mark.parametrize('version', [None, 1, True, 2.0, '2'])
def test_v2_artifacts_refuse_missing_or_noninteger_version(version: object) -> None:
    """Prevent v1 and malformed documents from entering v2 processing."""
    data = _collect()
    if version is None:
        del data['meta']['schema_version']
    else:
        data['meta']['schema_version'] = version
    with pytest.raises(ValidationError):
        CollectDocument.model_validate(data)
