"""Isolate real HTTP decoding failures at the record retry boundary."""

from pathlib import Path

import httpx
import pytest
from basket_collector.attempts import AttemptRunner
from basket_collector.contracts import TasksDocument
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.llm import LLMClient
from basket_collector.pipeline import Pipeline
from basket_collector.storage import read_document
from test_llm import _response
from test_pipeline import FakeGitHub, FakeGitRepository, _config, _filter_one, _producer, auth_generation


def test_corrupt_http_body_exhausts_one_generation_record_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HTTP decompression failures consume only the affected record's stage budget."""
    monkeypatch.setenv('BASKET_TEST_LLM_KEY', 'fixture')
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    monkeypatch.setattr(AttemptRunner, '_wait_ready', lambda self, journal: None)
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2]
    config.filter.selection.shuffle = False
    config.generation = auth_generation('openai_responses')
    config.generation.max_retries = 1
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return corrupt gzip for the first record and a normal response for the next."""
        calls.append(len(calls) + 1)
        if len(calls) <= 2:
            return httpx.Response(200, headers={'Content-Encoding': 'gzip'}, content=b'broken gzip')
        return httpx.Response(200, json=_response('openai_responses'))

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(config.generation, http_client=transport) as client:
            runner = Pipeline(
                config,
                producer=_producer(),
                remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
                git_repository=FakeGitRepository(),
                llm_client=client,
            )
            assert runner.run('basket', allow_partial=True) == 0
    document = read_document(Path(config.output_dir) / 'tasks.json', TasksDocument)
    assert [item.record.change_request.number for item in document.failed] == [1]
    assert document.failed[0].attempts == 2
    assert [item.change_request.number for item in document.tasks] == [2]
    assert len(calls) == 3
