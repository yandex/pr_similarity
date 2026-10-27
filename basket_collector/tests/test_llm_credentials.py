"""Verify recoverable credential failures preserve resumable generation progress."""

import json
from pathlib import Path

import httpx
import pytest
from test_pipeline import FakeGitHub, FakeGitRepository, _config, _filter_one, _producer

from basket_collector.config import GenerationConfig
from basket_collector.models.artifacts import TasksDocument
from basket_collector.persistence.storage import read_checkpoint, read_document
from basket_collector.pipeline import Pipeline
from basket_collector.providers.github_source import GitHubRemoteVCS
from basket_collector.providers.llm import LLMClient, LLMConfigurationError


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
def test_missing_key_resumes_pending_tasks_after_credential_is_added(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: str,
) -> None:
    """Resume an unchanged generation checkpoint after adding its missing key."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    monkeypatch.delenv('BASKET_TEST_MISSING_KEY', raising=False)
    prompts = Path(__file__).resolve().parents[1] / 'prompts'
    generation = GenerationConfig.model_validate(
        {
            'source': 'llm',
            'context': 'diff',
            'workers': 1,
            'max_retries': 3,
            'requests_per_second': 100000,
            'templates': {'system': str(prompts / 'system.jinja2'), 'user': str(prompts / 'diff.jinja2')},
            'llm': {
                'api': api,
                'model': 'fixture',
                'base_url': 'https://example.test/v1',
                'api_key_env': 'BASKET_TEST_MISSING_KEY',
            },
        }
    )
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            'collect': config.collect.model_copy(update={'change_ids': [1, 2]}),
            'generation': generation,
        }
    )
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )
        == 0
    )
    assert Pipeline(config, producer=_producer(), git_repository=FakeGitRepository()).run('filter') == 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a valid response only after the credential has been supplied."""
        requests.append(request)
        assert request.headers['Authorization'] == 'Bearer added-key'
        content = json.dumps({'task': f'Task {len(requests)}', 'subtasks': []})
        if api == 'openai_completions':
            response = {
                'id': f'chat-{len(requests)}',
                'model': 'fixture',
                'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
            }
        else:
            response = {
                'id': f'resp-{len(requests)}',
                'model': 'fixture',
                'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}],
            }
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(generation, http_client=transport) as client:
            with pytest.raises(LLMConfigurationError, match='missing API key'):
                Pipeline(config, producer=_producer(), llm_client=client).run('generate')
        output = Path(config.output_dir)
        checkpoint, _ = read_checkpoint(output / '.work' / 'generate.json', TasksDocument)
        assert checkpoint.meta.status == 'failed'
        assert checkpoint.meta.counts.pending == 2
        assert checkpoint.meta.counts.succeeded == 0
        assert checkpoint.tasks == []
        assert checkpoint.failed == []
        assert checkpoint.meta.error is not None
        assert checkpoint.meta.error.code == 'llm_auth_error'
        assert checkpoint.meta.error.retryable is True
        assert requests == []
        assert not (output / 'tasks.json').exists()
        monkeypatch.setenv('BASKET_TEST_MISSING_KEY', 'added-key')
        with LLMClient(generation, http_client=transport) as client:
            resumed = Pipeline(config, producer=_producer(), llm_client=client)
            assert resumed.run('generate') == 0
            assert resumed.stage_modes['generate'] == 'resumed'

    tasks = read_document(output / 'tasks.json', TasksDocument)
    assert tasks.meta.status == 'completed'
    assert tasks.meta.counts.pending == 0
    assert tasks.meta.counts.succeeded == 2
    assert {task.change_request.number for task in tasks.tasks} == {1, 2}
    assert {task.task.task for task in tasks.tasks} == {'Task 1', 'Task 2'}
    assert all(task.generation.attempts == 1 for task in tasks.tasks)
    assert tasks.failed == []
    assert len(requests) == 2
    assert not (output / '.work' / 'generate.json').exists()
