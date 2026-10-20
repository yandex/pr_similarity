"""Tests for persisted stage and basket JSON contracts."""

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

import pytest
from basket_collector.contracts import (
    BasketDocument,
    BasketMeta,
    CollectDocument,
    CollectMeta,
    FilterDocument,
    TaskContent,
    TasksDocument,
    format_agent_task,
)
from fixture_helpers import descriptor, issue_identity, retrieval
from pydantic import BaseModel, ValidationError

H40 = 'a' * 40
H64 = 'b' * 64
TIME = '2026-09-29T00:00:00Z'


def _settings_hash(settings: Mapping[str, object]) -> str:
    """Hash fixture settings with the canonical JSON encoding."""
    canonical = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _selection(identity: str) -> dict[str, Any]:
    """Bind a one-record fixture to an explicit v2 selection snapshot."""
    data = {
        'schema_version': 2,
        'input_sha256': 'a' * 64,
        'selected_ids': [identity],
        'generation_keys': {identity: 'd' * 64},
        'fingerprint': _settings_hash({'tasks': [[identity, 'd' * 64]]}),
        'settings': {},
        'generation_settings_sha256': _settings_hash({}),
        'excluded': [],
        'omissions': [],
    }
    return {**data, 'sha256': _settings_hash(data)}


def _selection_receipt(identity: str) -> dict[str, Any]:
    """Keep only fields used by generation and frozen basket receipts."""
    return {
        key: value
        for key, value in _selection(identity).items()
        if key not in {'settings', 'generation_settings_sha256', 'excluded', 'omissions'}
    }


def _collect() -> dict[str, Any]:
    """Build a valid completed collection document fixture."""
    return {
        'meta': {
            'schema_family': 'pr_similarity_os.collector',
            'schema_version': 2,
            'stage': 'collect',
            'status': 'completed',
            'input': None,
            'settings': {},
            'settings_hash': _settings_hash({}),
            'producer': {
                'version': '0.1',
                'implementation_sha256': H64,
                'dependencies_sha256': H64,
                'python_version': '3.12.3',
                'git_version': '2.0',
            },
            'lineage': [],
            'repository': {
                'provider': 'github',
                'server_url': 'https://github.com',
                'project_id': '123',
                'path': 'owner/repo',
                'web_url': 'https://github.com/owner/repo',
                'default_branch': 'develop',
            },
            'started_at': TIME,
            'updated_at': TIME,
            'completed_at': TIME,
            'counts': {'input': 1, 'succeeded': 1, 'rejected': 0, 'failed': 0, 'pending': 0},
            'error': None,
            'discovery': {'matched': 1, 'selected': 1, 'capped': False},
        },
        'tasks': [
            {
                'id': 'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea',
                'change_request': {
                    'repository': 'owner/repo',
                    'number': 1,
                    'url': 'https://github.com/owner/repo/pull/1',
                    'title': 'Title',
                    'body': '',
                    'author': None,
                    'merged_at': TIME,
                    'kind': 'pull_request',
                },
                'revisions': {
                    'source': {
                        'base_sha': H40,
                        'head_sha': H40,
                        'merge_commit_sha': None,
                        'head_repository': None,
                        'commits_count': 1,
                        'expected_base_sha': None,
                        'squash_commit_sha': None,
                        'diff_version_id': None,
                        'target_branch': 'develop',
                    }
                },
                'source': descriptor('owner/repo', 1).model_dump(mode='json'),
            }
        ],
    }


def _filter() -> dict[str, Any]:
    """Extend the collection fixture with verified filter data."""
    doc = _collect()
    meta = doc['meta']
    meta['stage'] = 'filter'
    meta['input'] = {'stage': 'collect', 'sha256': H64}
    meta['lineage'] = [
        {
            'stage': 'collect',
            'sha256': H64,
            'settings_hash': _settings_hash({}),
            'settings': {},
            'producer': meta['producer'],
        }
    ]
    meta.pop('discovery')
    record = doc['tasks'][0]
    record['revisions']['resolved'] = {
        'policy': 'change_merge_base_v1',
        'before': 'c' * 40,
        'after': H40,
        'commits': [H40],
        'before_tree': 'dddddddddddddddddddddddddddddddddddddddd',
        'after_tree': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    }
    record['changes'] = {
        'files': [],
        'stats': {'files_count': 0, 'additions': 0, 'deletions': 0, 'changed_lines': 0, 'patch_chars': 0},
        'diff': '',
        'commit_messages': [{'sha': H40, 'message': 'Change'}],
    }
    record['issues'] = []
    record['retrieval'] = retrieval(descriptor('owner/repo', 1), 'c' * 40, H40).model_dump(mode='json')
    doc['rejected'] = []
    doc['selection'] = _selection(record['id'])
    return doc


def _tasks() -> dict[str, Any]:
    """Extend the filter fixture with a directly generated task."""
    doc = _filter()
    meta = doc['meta']
    meta['stage'] = 'generate'
    meta['input'] = {'stage': 'filter', 'sha256': H64}
    meta['lineage'].append(
        {
            'stage': 'filter',
            'sha256': H64,
            'settings_hash': _settings_hash({}),
            'settings': {},
            'producer': meta['producer'],
        }
    )
    doc.pop('rejected')
    doc['failed'] = []
    record = doc['tasks'][0]
    doc['selection'] = _selection_receipt(record['id'])
    record['task'] = {'task': 'Do work', 'subtasks': []}
    record['generation'] = {
        'issue_ids': [],
        'attempts': 0,
        'response_id': None,
        'response_model': None,
        'usage': None,
        'request_sha256': None,
        'input_sha256': 'd' * 64,
    }
    return doc


def _basket() -> dict[str, Any]:
    """Build a valid exported basket fixture without a golden diff."""
    doc = _collect()
    meta = doc['meta']
    meta['schema_family'] = 'pr_similarity_os.basket'
    meta['stage'] = 'export'
    meta['input'] = {'stage': 'generate', 'sha256': H64}
    meta['settings'] = {'include_golden_diff': False}
    meta['settings_hash'] = _settings_hash(meta['settings'])
    meta['lineage'] = [
        {
            'stage': stage,
            'sha256': H64,
            'settings_hash': _settings_hash({}),
            'settings': {},
            'producer': meta['producer'],
        }
        for stage in ('collect', 'filter', 'generate')
    ]
    discovery = meta.pop('discovery')
    counts = {'input': 1, 'succeeded': 1, 'failed': 0, 'rejected': 0, 'pending': 0}
    meta.update(
        name='basket',
        description='fixture',
        maintainers=['alice'],
        partial=False,
        omissions=[],
        coverage={'discovery': discovery, 'collect': counts, 'filter': counts, 'selected': 1, 'generate': counts},
        selection=_selection_receipt(doc['tasks'][0]['id']),
    )
    doc['tasks'] = [
        {
            'id': 'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea',
            'change_request': {
                'repository': 'owner/repo',
                'number': 1,
                'url': 'https://github.com/owner/repo/pull/1',
                'title': 'Title',
                'author': None,
                'merged_at': TIME,
                'kind': 'pull_request',
            },
            'revisions': {
                'policy': 'change_merge_base_v1',
                'before': H40,
                'after': H40,
                'before_tree': 'dddddddddddddddddddddddddddddddddddddddd',
                'after_tree': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            },
            'changes': {
                'files': [],
                'stats': {'files_count': 0, 'additions': 0, 'deletions': 0, 'changed_lines': 0, 'patch_chars': 0},
            },
            'task': {'task': 'Do work', 'subtasks': []},
            'workspace': '.',
            'agent_task': 'Task: Do work',
            'provenance': {
                'issue_refs': [],
                'generation': {
                    'issue_ids': [],
                    'attempts': 0,
                    'response_id': None,
                    'response_model': None,
                    'usage': None,
                    'request_sha256': None,
                    'input_sha256': 'd' * 64,
                },
                'retrieval': retrieval(descriptor('owner/repo', 1), H40, H40).model_dump(mode='json'),
                'source_revisions': {
                    'base_sha': H40,
                    'head_sha': H40,
                    'expected_base_sha': None,
                    'merge_commit_sha': None,
                    'squash_commit_sha': None,
                    'head_repository': None,
                    'target_branch': 'develop',
                    'diff_version_id': None,
                    'commits_count': 1,
                },
            },
            'source': descriptor('owner/repo', 1).model_dump(mode='json'),
            'diff_policy': 'git_source_changes_v1',
        }
    ]
    return doc


def test_collect_round_trip_and_schema() -> None:
    """Round-trip a collection document and forbid unknown record fields."""
    document = CollectDocument.model_validate(_collect())
    assert document.meta.schema_version == 2
    assert CollectDocument.model_validate(document.model_dump(mode='json')) == document
    schema = CollectDocument.model_json_schema()
    assert schema['$defs']['CollectRecord']['additionalProperties'] is False


def test_collect_input_count_must_match_discovery_population() -> None:
    """A completed artifact cannot silently shrink its selected PR population."""
    payload = _collect()
    payload['meta']['discovery'].update(matched=2, selected=2)
    with pytest.raises(ValidationError, match='input count does not match discovery selected'):
        CollectDocument.model_validate(payload)


@pytest.mark.parametrize('fixture, model', [(_collect, CollectMeta), (_basket, BasketMeta)])
@pytest.mark.parametrize('version', [True, 1.0], ids=['boolean', 'float'])
def test_schema_version_rejects_non_integer_one(
    fixture: Callable[[], dict[str, Any]], model: type[BaseModel], version: bool | float
) -> None:
    """Reject boolean and floating-point versions in collector and basket metadata."""
    meta = fixture()['meta']
    meta['schema_version'] = version
    with pytest.raises(ValidationError, match='schema_version'):
        model.model_validate(meta)


@pytest.mark.parametrize('fixture, model', [(_collect, CollectMeta), (_basket, BasketMeta)])
def test_schema_version_exposes_integer_two(fixture: Callable[[], dict[str, Any]], model: type[BaseModel]) -> None:
    """Require version two as an integer value and JSON Schema constant."""
    meta = model.model_validate(fixture()['meta']).model_dump(mode='json')
    assert type(meta['schema_version']) is int
    assert meta['schema_version'] == 2
    schema = model.model_json_schema()['properties']['schema_version']
    assert schema['const'] == 2
    assert schema['type'] == 'integer'


def test_rejects_unknown_revision_policy_in_filter_and_basket() -> None:
    """Reject unsupported Git policies in filter and basket records."""
    filtered = _filter()
    filtered['tasks'][0]['revisions']['resolved']['policy'] = 'unknown_policy'
    with pytest.raises(ValidationError, match='policy'):
        FilterDocument.model_validate(filtered)

    basket = _basket()
    basket['tasks'][0]['revisions']['policy'] = 'unknown_policy'
    with pytest.raises(ValidationError, match='policy'):
        BasketDocument.model_validate(basket)


@pytest.mark.parametrize(
    'change',
    [
        lambda doc: doc['meta'].update(schema_family='unsupported.family'),
        lambda doc: doc['meta'].update(schema_version=1),
        lambda doc: doc['meta']['repository'].pop('default_branch'),
        lambda doc: doc['meta']['repository'].update(default_branch=None),
        lambda doc: doc['meta']['counts'].update(input=True),
        lambda doc: doc['tasks'][0]['change_request'].update(number='1'),
        lambda doc: doc['tasks'][0].update(extra='no'),
        lambda doc: doc['tasks'].append(deepcopy(doc['tasks'][0])),
    ],
)
def test_collect_rejects_wrong_family_scalar_or_identity(change: Callable[[dict[str, Any]], None]) -> None:
    """Reject invalid schema, scalar, repository, or duplicate identity data."""
    document = _collect()
    change(document)
    with pytest.raises(ValidationError):
        CollectDocument.model_validate(document)


def test_task_content_is_strict_and_nonempty() -> None:
    """Require nonempty strict task content and stable task rendering."""
    assert TaskContent.model_validate({'task': 'Do work', 'subtasks': []}).task == 'Do work'
    for value in (
        {'task': ' ', 'subtasks': []},
        {'task': 'Do', 'subtasks': ['']},
        {'task': 'Do', 'subtasks': [1]},
        {'task': 'Do', 'subtasks': [], 'extra': 1},
    ):
        with pytest.raises(ValidationError):
            TaskContent.model_validate(value)
    assert TaskContent.model_json_schema()['additionalProperties'] is False
    assert (
        format_agent_task(TaskContent(task='Do', subtasks=['First\ncontinued']))
        == 'Task: Do\n\nSubtasks:\n- First\n  continued'
    )


def test_basket_golden_diff_presence_and_string_task() -> None:
    """Enforce golden diff presence and require a string agent task."""
    doc = _basket()
    basket = BasketDocument.model_validate(doc)
    assert 'golden_diff' not in basket.model_dump(mode='json')['tasks'][0]
    assert BasketDocument.model_validate(basket.model_dump(mode='json')) == basket
    doc['tasks'][0]['golden_diff'] = None
    with pytest.raises(ValidationError):
        BasketDocument.model_validate(doc)
    doc['tasks'][0]['golden_diff'] = 'patch'
    doc['meta']['settings']['include_golden_diff'] = True
    doc['meta']['settings_hash'] = _settings_hash(doc['meta']['settings'])
    BasketDocument.model_validate(doc)
    doc['tasks'][0]['agent_task'] = {'description': 'bad'}
    with pytest.raises(ValidationError):
        BasketDocument.model_validate(doc)


@pytest.mark.parametrize(
    'mutation',
    [
        lambda record: record['revisions']['resolved'].update(after='d' * 40, commits=['d' * 40]),
        lambda record: record['revisions']['source'].update(commits_count=2),
        lambda record: record['changes'].update(commit_messages=[{'sha': 'd' * 40, 'message': 'Change'}]),
    ],
)
def test_filter_rejects_inconsistent_revision_provenance(mutation: Callable[[dict[str, Any]], None]) -> None:
    """Reject source, resolved commit, or message provenance mismatches."""
    doc = _filter()
    mutation(doc['tasks'][0])
    with pytest.raises(ValidationError):
        FilterDocument.model_validate(doc)


def test_filter_rejects_commit_messages_in_wrong_order() -> None:
    """Require commit messages in resolved commit order."""
    doc = _filter()
    record = doc['tasks'][0]
    record['revisions']['source']['commits_count'] = 2
    record['revisions']['resolved']['commits'] = ['c' * 40, H40]
    record['changes']['commit_messages'] = [
        {'sha': H40, 'message': 'Last'},
        {'sha': 'c' * 40, 'message': 'First'},
    ]
    with pytest.raises(ValidationError):
        FilterDocument.model_validate(doc)


@pytest.mark.parametrize(
    'fixture, model',
    [
        (_collect, CollectDocument),
        (_filter, FilterDocument),
        (_tasks, TasksDocument),
        (_basket, BasketDocument),
    ],
)
def test_document_rejects_record_from_different_repository(
    fixture: Callable[[], dict[str, Any]], model: type[BaseModel]
) -> None:
    """Reject foreign PR records in every stage document."""
    doc = fixture()
    doc['tasks'][0]['change_request']['repository'] = 'someone/else'
    doc['tasks'][0]['id'] = 'change:71910953c154893ff9961704b3cc209dddd7f5d7b9124b91a411881112db9e5d'
    with pytest.raises(ValidationError):
        model.model_validate(doc)


def test_filter_rejects_rejected_record_from_different_repository() -> None:
    """Reject a foreign PR in the filter rejection list."""
    doc = _filter()
    doc['tasks'] = []
    doc['rejected'] = [
        {
            'id': 'change:71910953c154893ff9961704b3cc209dddd7f5d7b9124b91a411881112db9e5d',
            'change_request': {
                'repository': 'someone/else',
                'number': 1,
                'url': 'https://github.com/someone/else/pull/1',
                'title': 'Title',
                'author': None,
            },
            'reasons': [{'code': 'size', 'message': 'too large', 'observed': 10, 'expected': 5}],
        }
    ]
    doc['meta']['counts'].update(succeeded=0, rejected=1)
    with pytest.raises(ValidationError):
        FilterDocument.model_validate(doc)


def test_tasks_rejects_failed_record_from_different_repository() -> None:
    """Reject a foreign PR in the generation failure list."""
    doc = _tasks()
    failed_record = deepcopy(_filter()['tasks'][0])
    failed_record['change_request']['repository'] = 'someone/else'
    failed_record['id'] = 'change:71910953c154893ff9961704b3cc209dddd7f5d7b9124b91a411881112db9e5d'
    doc['tasks'] = []
    doc['failed'] = [
        {
            'record': failed_record,
            'error': {
                'code': 'request_failed',
                'message': 'failed',
                'retryable': True,
            },
            'attempts': 1,
        }
    ]
    doc['meta']['counts'].update(succeeded=0, failed=1)
    with pytest.raises(ValidationError):
        TasksDocument.model_validate(doc)


@pytest.mark.parametrize('fixture, model', [(_tasks, TasksDocument), (_basket, BasketDocument)])
@pytest.mark.parametrize(
    'issue_ids',
    [
        [
            'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
            'issue:dac49cc1c2d0360927576777361293ed14bea276ff1cce446d19fb4d6ff331a6',
        ],
        [],
        ['issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0'],
        [
            'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
            'issue:8732913279b8a0e01dc22e185e5ff31d88927c92d16648c771eca1bc65958cf9',
        ],
        [
            'issue:dac49cc1c2d0360927576777361293ed14bea276ff1cce446d19fb4d6ff331a6',
            'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
        ],
    ],
    ids=['matching', 'missing-all', 'missing-one', 'divergent', 'reordered'],
)
def test_documents_require_exact_ordered_generation_issue_provenance(
    fixture: Callable[[], dict[str, Any]], model: type[BaseModel], issue_ids: list[str]
) -> None:
    """Require generation issue IDs to match every linked issue in order."""
    doc = fixture()
    record = doc['tasks'][0]
    issues = [
        {
            'id': issue_identity(number),
            'repository': 'owner/repo',
            'number': number,
            'url': f'https://github.com/owner/repo/issues/{number}',
            'title': f'Issue {number}',
            'body': 'Task context',
        }
        for number in (2, 3)
    ]
    if model is TasksDocument:
        record['issues'] = issues
        generation = record['generation']
    else:
        record['provenance']['issue_refs'] = [{'id': issue['id'], 'url': issue['url']} for issue in issues]
        generation = record['provenance']['generation']
    generation['issue_ids'] = issue_ids
    if issue_ids == [
        'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
        'issue:dac49cc1c2d0360927576777361293ed14bea276ff1cce446d19fb4d6ff331a6',
    ]:
        model.model_validate(doc)
    else:
        with pytest.raises(ValidationError, match='generation issue IDs'):
            model.model_validate(doc)


def test_meta_and_lineage_validate_canonical_settings_hash() -> None:
    """Require canonical settings digests in metadata and lineage."""
    doc = _filter()
    doc['meta']['settings_hash'] = H64
    with pytest.raises(ValidationError):
        FilterDocument.model_validate(doc)
    doc['meta']['settings_hash'] = _settings_hash(doc['meta']['settings'])
    doc['meta']['lineage'][0]['settings_hash'] = H64
    with pytest.raises(ValidationError):
        FilterDocument.model_validate(doc)


@pytest.mark.parametrize('timestamp', ['2026-02-30T00:00:00Z', '2026-01-01T25:00:00Z'])
def test_rejects_impossible_utc_timestamp(timestamp: str) -> None:
    """Reject calendar-invalid or out-of-range UTC timestamps."""
    doc = _collect()
    doc['tasks'][0]['change_request']['merged_at'] = timestamp
    with pytest.raises(ValidationError):
        CollectDocument.model_validate(doc)


@pytest.mark.parametrize('workspace', ['.', 'src', 'src/nested', 'новый каталог'])
def test_basket_workspace_round_trip(workspace: str) -> None:
    """Persist the explicit safe cwd in initial schema v1 without recomputing it."""
    payload = _basket()
    payload['tasks'][0]['workspace'] = workspace
    document = BasketDocument.model_validate(payload)
    assert document.meta.schema_version == 2
    assert document.model_dump(mode='json')['tasks'][0]['workspace'] == workspace
    assert BasketDocument.model_validate_json(document.model_dump_json()) == document
    schema = BasketDocument.model_json_schema()
    assert 'workspace' in schema['$defs']['BasketRecord']['required']


def test_basket_workspace_is_required() -> None:
    """Reject old test baskets instead of silently choosing a benchmark cwd."""
    payload = _basket()
    del payload['tasks'][0]['workspace']
    with pytest.raises(ValidationError, match='workspace'):
        BasketDocument.model_validate(payload)


@pytest.mark.parametrize(
    'workspace',
    [None, 1, True, [], '', '/src', '../src', 'src/..', './src', 'src/./a', 'src//a', 'src/', 'src\\a', 'src\x00a'],
)
def test_basket_workspace_rejects_invalid_paths_and_types(workspace: object) -> None:
    """Reject coercion and unsafe or noncanonical paths at the basket boundary."""
    payload = _basket()
    payload['tasks'][0]['workspace'] = workspace
    with pytest.raises(ValidationError, match='workspace'):
        BasketDocument.model_validate(payload)
