"""The benchmark-facing task projection is deliberately smaller than tasks.json."""

from typing import cast

import pytest
from basket_collector.contracts import (
    BasketMeta,
    Counts,
    GenerateMeta,
    InputReceipt,
    LineageReceipt,
    Producer,
    Repository,
    StageName,
    TaskRecord,
    TasksDocument,
)
from basket_collector.stages.export import export_tasks, project_record
from basket_collector.storage import sha256, stage_sha256
from fixture_helpers import descriptor, retrieval


def _task_record() -> TaskRecord:
    """Build a task record with saved diff and generation metadata."""
    diff = 'diff --git a/a.py b/a.py\n+print(1)\n'
    return TaskRecord.model_validate(
        {
            'id': 'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea',
            'change_request': {
                'repository': 'o/r',
                'number': 1,
                'url': 'https://github.com/o/r/pull/1',
                'title': 'Add output',
                'body': 'Developer context',
                'author': 'alice',
                'merged_at': '2026-01-01T00:00:00Z',
                'kind': 'pull_request',
            },
            'revisions': {
                'source': {
                    'base_sha': 'a' * 40,
                    'head_sha': 'b' * 40,
                    'merge_commit_sha': 'c' * 40,
                    'head_repository': 'o/r',
                    'commits_count': 1,
                    'expected_base_sha': None,
                    'squash_commit_sha': None,
                    'diff_version_id': None,
                    'target_branch': 'develop',
                },
                'resolved': {
                    'policy': 'change_merge_base_v1',
                    'before': 'a' * 40,
                    'after': 'b' * 40,
                    'commits': ['b' * 40],
                    'before_tree': 'dddddddddddddddddddddddddddddddddddddddd',
                    'after_tree': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                },
            },
            'changes': {
                'files': [
                    {
                        'path': 'a.py',
                        'status': 'added',
                        'additions': 1,
                        'deletions': 0,
                        'before_blob': None,
                        'after_blob': 'd' * 40,
                        'before_mode': None,
                        'after_mode': '100644',
                    }
                ],
                'stats': {
                    'files_count': 1,
                    'additions': 1,
                    'deletions': 0,
                    'changed_lines': 1,
                    'patch_chars': len(diff),
                },
                'diff': diff,
                'commit_messages': [{'sha': 'b' * 40, 'message': 'Add output'}],
            },
            'issues': [],
            'task': {'task': 'Add output', 'subtasks': ['Write a.py']},
            'generation': {
                'issue_ids': [],
                'attempts': 0,
                'response_id': None,
                'response_model': None,
                'usage': None,
                'request_sha256': None,
            },
            'source': descriptor('o/r', 1).model_dump(mode='json'),
            'retrieval': retrieval(descriptor('o/r', 1), 'a' * 40, 'b' * 40).model_dump(mode='json'),
        }
    )


def test_export_uses_flat_agent_task_and_omits_reference_by_default() -> None:
    """Export a flat agent task without private source context by default."""
    result = project_record(_task_record(), include_golden_diff=False)
    payload = result.model_dump(mode='json')
    assert payload['agent_task'] == 'Task: Add output\n\nSubtasks:\n- Write a.py'
    assert 'description' not in payload['agent_task']
    assert 'golden_diff' not in payload
    assert 'diff' not in payload['changes']
    assert 'body' not in payload['change_request']


def test_golden_diff_is_exact_saved_patch_when_requested() -> None:
    """Include the exact saved patch when golden diff export is enabled."""
    task = _task_record()
    result = project_record(task, include_golden_diff=True)
    assert result.model_dump(mode='json')['golden_diff'] == task.changes.diff


def test_export_rejects_metadata_pointing_to_another_tasks_document() -> None:
    """Reject export metadata referring to another tasks document."""
    producer = Producer(
        version='0.1.0',
        implementation_sha256='a' * 64,
        dependencies_sha256='b' * 64,
        python_version='3.12.3',
        git_version='git 2',
    )
    repository = Repository(
        path='o/r',
        web_url='https://github.com/o/r',
        default_branch='develop',
        provider='github',
        server_url='https://github.com',
        project_id='123',
    )
    empty_hash = sha256({})
    lineage = [
        LineageReceipt(
            stage=cast(StageName, stage), sha256=digest, settings_hash=empty_hash, settings={}, producer=producer
        )
        for stage, digest in (('collect', 'c' * 64), ('filter', 'd' * 64))
    ]
    common = {
        'status': 'completed',
        'settings': {},
        'settings_hash': empty_hash,
        'producer': producer,
        'repository': repository,
        'started_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:01Z',
        'completed_at': '2026-01-01T00:00:02Z',
        'error': None,
        'counts': Counts(input=1, succeeded=1, rejected=0, failed=0, pending=0),
    }
    tasks = TasksDocument(
        meta=GenerateMeta(
            **common,
            input=InputReceipt(stage='filter', sha256='d' * 64),
            lineage=lineage,
        ),
        tasks=[_task_record()],
        failed=[],
    )
    export_settings = {'include_golden_diff': False}
    wrong_hash = 'e' * 64
    assert wrong_hash != stage_sha256(tasks)
    basket_meta = BasketMeta(
        **{**common, 'settings': export_settings, 'settings_hash': sha256(export_settings)},
        input=InputReceipt(stage='generate', sha256=wrong_hash),
        lineage=[
            *lineage,
            LineageReceipt(
                stage='generate',
                sha256=wrong_hash,
                settings_hash=empty_hash,
                settings={},
                producer=producer,
            ),
        ],
        name='fixture',
        description='fixture',
        maintainers=['alice'],
        partial=False,
        omitted_failed_ids=[],
    )
    with pytest.raises(ValueError, match='input|lineage'):
        export_tasks(tasks, basket_meta)


@pytest.mark.parametrize(
    ('paths', 'expected'),
    [
        (['src/a.py', 'src/b.py'], 'src'),
        (['src/a.py', 'src/nested/b.py'], 'src'),
        (['src/a.py', 'tests/test_a.py'], '.'),
        (['README.md', 'src/a.py'], '.'),
        (['src/a.py', 'z.py'], '.'),
        (['src/a.py'], 'src'),
        (['a.py'], '.'),
        (['src/a/one.py', 'src/ab/two.py'], 'src'),
        (['src/a.py', 'src_extra/b.py'], '.'),
        (['новый каталог/a.py'], 'новый каталог'),
        ([], '.'),
    ],
)
@pytest.mark.parametrize('include_golden_diff', [False, True])
def test_export_workspace_uses_changed_file_parent_directories(
    paths: list[str], expected: str, include_golden_diff: bool
) -> None:
    """Select a component-wise LCA without reading the checkout or reference patch."""
    payload = _task_record().model_dump(mode='json')
    template = payload['changes']['files'][0]
    diff = ''.join(f'diff --git a/{path} b/{path}\n+print(1)\n' for path in paths)
    payload['changes']['files'] = [{**template, 'path': path} for path in paths]
    payload['changes']['diff'] = diff
    payload['changes']['stats'].update(
        files_count=len(paths), additions=len(paths), changed_lines=len(paths), patch_chars=len(diff)
    )
    task = TaskRecord.model_validate(payload)
    result = project_record(task, include_golden_diff=include_golden_diff)
    assert result.model_dump(mode='json')['workspace'] == expected
    assert result.changes.files == task.changes.files
    assert 'workspace' not in task.model_dump(mode='json')


def test_export_workspace_includes_modified_and_deleted_paths() -> None:
    """A deleted root file keeps cwd at root even when additions are nested."""
    payload = _task_record().model_dump(mode='json')
    template = payload['changes']['files'][0]
    payload['changes']['files'] = [
        {**template, 'path': 'src/new.py'},
        {**template, 'path': 'src/current.py', 'status': 'modified', 'before_blob': 'c' * 40, 'before_mode': '100644'},
        {
            **template,
            'path': 'old.py',
            'status': 'deleted',
            'additions': 0,
            'deletions': 1,
            'before_blob': 'c' * 40,
            'before_mode': '100644',
            'after_blob': None,
            'after_mode': None,
        },
    ]
    payload['changes']['files'].sort(key=lambda item: item['path'])
    payload['changes']['diff'] = (
        'diff --git a/old.py b/old.py\n-print(1)\n'
        'diff --git a/src/current.py b/src/current.py\n+print(1)\n'
        'diff --git a/src/new.py b/src/new.py\n+print(1)\n'
    )
    payload['changes']['stats'].update(
        files_count=3, additions=2, deletions=1, changed_lines=3, patch_chars=len(payload['changes']['diff'])
    )
    result = project_record(TaskRecord.model_validate(payload), include_golden_diff=False)
    assert result.model_dump(mode='json')['workspace'] == '.'
