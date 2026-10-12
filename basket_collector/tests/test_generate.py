"""Exercise issue and LLM task generation with frozen templates."""

import hashlib
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from basket_collector.config import (
    CollectorConfig,
    GenerationConfig,
    effective_settings,
    load_template_snapshot,
)
from basket_collector.contracts import (
    FailedRecord,
    FilterRecord,
    TaskRecord,
    settings_sha256,
)
from basket_collector.llm import LLMClient, LLMConfigurationError
from basket_collector.stages.generate import generate_record
from fixture_helpers import descriptor, retrieval, task_identity

SHA = 'a' * 40
TIME = '2026-09-29T00:00:00Z'


def _record(issues: list[dict[str, object]] | None = None) -> FilterRecord:
    """Build a filtered PR fixture with optional linked issues."""
    return FilterRecord.model_validate(
        {
            'id': 'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea',
            'change_request': {
                'repository': 'owner/repo',
                'number': 1,
                'url': 'https://github.com/owner/repo/pull/1',
                'title': 'PR title',
                'body': 'PR private marker',
                'author': None,
                'merged_at': TIME,
                'kind': 'pull_request',
            },
            'revisions': {
                'source': {
                    'base_sha': SHA,
                    'head_sha': SHA,
                    'merge_commit_sha': None,
                    'head_repository': None,
                    'commits_count': 1,
                    'expected_base_sha': None,
                    'squash_commit_sha': None,
                    'diff_version_id': None,
                    'target_branch': 'develop',
                },
                'resolved': {
                    'policy': 'change_merge_base_v1',
                    'before': SHA,
                    'after': SHA,
                    'commits': [SHA],
                    'before_tree': 'dddddddddddddddddddddddddddddddddddddddd',
                    'after_tree': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                },
            },
            'changes': {
                'files': [],
                'stats': {'files_count': 0, 'additions': 0, 'deletions': 0, 'changed_lines': 0, 'patch_chars': 11},
                'diff': 'diff marker',
                'commit_messages': [{'sha': SHA, 'message': 'commit marker'}],
            },
            'issues': (
                issues
                if issues is not None
                else [
                    {
                        'id': 'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
                        'repository': 'owner/repo',
                        'number': 2,
                        'url': 'https://github.com/owner/repo/issues/2',
                        'title': 'Issue title',
                        'body': 'Issue body',
                    }
                ]
            ),
            'source': descriptor('owner/repo', 1).model_dump(mode='json'),
            'retrieval': retrieval(descriptor('owner/repo', 1), SHA, SHA).model_dump(mode='json'),
        }
    )


def _templates(tmp_path: Path, user: str) -> dict[str, str]:
    """Write system and user prompt templates for generation tests."""
    system_path = tmp_path / 'system.jinja2'
    user_path = tmp_path / 'user.jinja2'
    system_path.write_text('Schema {{ result_schema }}', encoding='utf-8')
    user_path.write_text(user, encoding='utf-8')
    return {'system': str(system_path), 'user': str(user_path)}


def _config(tmp_path: Path, context: str, user: str, **overrides: object) -> GenerationConfig:
    """Build the local configuration fixture for this test module."""
    return GenerationConfig.model_validate(
        {
            'source': 'llm',
            'context': context,
            'templates': _templates(tmp_path, user),
            'llm': {
                'api': 'openai_responses',
                'base_url': 'https://example.test/v1',
                'api_key_env': 'TEST_LLM_KEY',
                'model': 'fixture',
            },
            **overrides,
        }
    )


def test_issue_source_preserves_all_linked_issues() -> None:
    """Verify issue-based tasks preserve all linked issue identities and bodies."""
    issues = [
        {
            'id': 'issue:03935e706d9b3f2aa539babb0ff39db64eee506feb7b482007a926d604581ae5',
            'repository': 'another/repo',
            'number': 4,
            'url': 'https://github.com/another/repo/issues/4',
            'title': ' First ',
            'body': '',
        },
        {
            'id': 'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
            'repository': 'owner/repo',
            'number': 2,
            'url': 'https://github.com/owner/repo/issues/2',
            'title': 'Second',
            'body': ' Markdown\nbody ',
        },
    ]
    result = generate_record(_record(issues), GenerationConfig(source='issue'))
    assert isinstance(result, TaskRecord)
    assert (
        result.task.task == '[issue:03935e706d9b3f2aa539babb0ff39db64eee506feb7b482007a926d604581ae5] First\n\n'
        '[issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0] Second'
    )
    assert result.task.subtasks == [
        '[issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0]\nMarkdown\nbody'
    ]
    assert result.generation.issue_ids == [
        'issue:03935e706d9b3f2aa539babb0ff39db64eee506feb7b482007a926d604581ae5',
        'issue:3d975f424a85317b0c0b0ea1ce4df0b37ad2dd6d2894c46283cb42a94a26aff0',
    ]
    assert result.generation.attempts == 0


def test_issue_source_missing_and_invalid_title_are_failed() -> None:
    """Verify missing issues and empty issue titles become per-PR failures."""
    no_issue = generate_record(_record([]), GenerationConfig(source='issue'))
    assert isinstance(no_issue, FailedRecord) and no_issue.error.code == 'missing_issue'
    issue = _record().model_dump(mode='json')
    issue['issues'][0]['title'] = ' '
    bad_title = generate_record(FilterRecord.model_validate(issue), GenerationConfig(source='issue'))
    assert isinstance(bad_title, FailedRecord) and bad_title.error.code == 'invalid_issue'


@pytest.mark.parametrize(
    'context,user,expected,excluded',
    [
        ('diff', '{{ pr_title }} {{ pr_body }} {{ commit_messages }} {{ diff }}', 'diff marker', 'Issue body'),
        (
            'issue',
            '{% for issue in issues %}{{ issue.id }} {{ issue.title }} {{ issue.body }}{% endfor %}',
            'Issue body',
            'PR private marker',
        ),
        (
            'diff+issue',
            '{{ diff }} {% for issue in issues %}{{ issue.body }}{% endfor %}',
            'Issue body',
            'PR private marker',
        ),
    ],
)
def test_context_only_exposes_selected_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: str, user: str, expected: str, excluded: str
) -> None:
    """Verify prompt context exposes only fields selected by configuration."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the selected prompt fields and return a task."""
        payload = json.loads(request.content)
        rendered = payload['input'][1]['content']
        seen.append(rendered)
        return httpx.Response(
            200,
            json={
                'id': 'r1',
                'model': 'fixture',
                'status': 'completed',
                'output': [
                    {
                        'type': 'message',
                        'content': [{'type': 'output_text', 'text': '{"task":"Do work","subtasks":[]}'}],
                    }
                ],
            },
        )

    config = _config(tmp_path, context, user)
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(config, http_client=transport) as client:
            result = generate_record(_record(), config, llm_client=client)
    assert isinstance(result, TaskRecord)
    assert expected in seen[0]
    assert excluded not in seen[0]
    assert result.generation.attempts == 1


def test_undefined_or_file_loading_template_is_rejected(tmp_path: Path) -> None:
    """Verify unsafe or undefined Jinja template input is rejected."""
    for user in ('{{ diff }}', '{% include \'secret.txt\' %}'):
        config = _config(tmp_path, 'issue', user)
        with pytest.raises(LLMConfigurationError):
            generate_record(_record(), config)


def test_input_limit_and_missing_issue_do_not_call_api(tmp_path: Path) -> None:
    """Verify local validation failures consume no LLM requests."""
    missing = generate_record(_record([]), _config(tmp_path, 'issue', '{{ issues }}'))
    assert isinstance(missing, FailedRecord) and missing.error.code == 'missing_issue'
    config = _config(tmp_path, 'diff', '{{ diff }}', max_input_chars=1)
    large = generate_record(_record(), config)
    assert isinstance(large, FailedRecord) and large.error.code == 'input_too_large'
    assert large.attempts == 0


def test_retry_failed_accumulates_actual_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify retrying a failed task retains the cumulative request count."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    config = _config(tmp_path, 'diff', '{{ diff }}', max_retries=0)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as transport:
        with LLMClient(config, http_client=transport) as client:
            failed = generate_record(_record(), config, llm_client=client, previous_attempts=4)
    assert isinstance(failed, FailedRecord)
    assert failed.error.code == 'request_failed'
    assert failed.error.retryable is True
    assert failed.attempts == 5


def test_two_prs_use_one_template_snapshot_when_files_change_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify two PRs use one frozen template snapshot despite later file edits."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    generation = _config(tmp_path, 'diff', '{{ pr_title }} original {{ diff }}')
    config = CollectorConfig.model_validate(
        {
            'source': {'provider': 'github', 'project': 'owner/repo'},
            'dataset': {'name': 'fixture', 'description': '', 'maintainers': ['tester']},
            'collect': {'selection': 'change_ids', 'change_ids': [1, 2]},
            'generation': generation.model_dump(),
        }
    )
    snapshot = load_template_snapshot(config.generation)
    assert snapshot is not None
    settings = effective_settings(config, 'generate', template_snapshot=snapshot)
    settings_hash = settings_sha256(settings)
    template_settings = cast(dict[str, dict[str, str]], settings['templates'])
    original_system = template_settings['system']['content']
    original_user = template_settings['user']['content']
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Change template files after the first request, then return tasks."""
        payload = json.loads(request.content)
        prompts = tuple(item['content'] for item in payload['input'])
        seen.append(prompts)
        if len(seen) == 1:
            Path(generation.templates.system).write_text('MUTATED SYSTEM', encoding='utf-8')
            assert generation.templates.user is not None
            Path(generation.templates.user).write_text('MUTATED USER {{ diff }}', encoding='utf-8')
        return httpx.Response(
            200,
            json={
                'id': f'r{len(seen)}',
                'model': 'fixture',
                'status': 'completed',
                'output': [
                    {
                        'type': 'message',
                        'content': [{'type': 'output_text', 'text': '{"task":"Do work","subtasks":[]}'}],
                    }
                ],
            },
        )

    second = _record().model_dump(mode='json')
    second['id'] = task_identity(2)
    second['change_request']['number'] = 2
    second['change_request']['url'] = 'https://github.com/owner/repo/pull/2'
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(generation, http_client=transport) as client:
            first_result = generate_record(_record(), generation, llm_client=client, template_snapshot=snapshot)
            second_result = generate_record(
                FilterRecord.model_validate(second), generation, llm_client=client, template_snapshot=snapshot
            )

    assert isinstance(first_result, TaskRecord)
    assert isinstance(second_result, TaskRecord)
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert 'original diff marker' in seen[0][1]
    assert 'MUTATED' not in ''.join(seen[0] + seen[1])
    assert template_settings['system']['sha256'] == hashlib.sha256(original_system.encode('utf-8')).hexdigest()
    assert template_settings['user']['sha256'] == hashlib.sha256(original_user.encode('utf-8')).hexdigest()
    assert effective_settings(config, 'generate', template_snapshot=snapshot) == settings
    assert settings_sha256(effective_settings(config, 'generate', template_snapshot=snapshot)) == settings_hash
    assert effective_settings(config, 'generate') != settings
