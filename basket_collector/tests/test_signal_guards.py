"""Exercise real signal intent before the coordinator propagates its worker event."""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
from basket_collector.config import FilterConfig
from basket_collector.contracts import (
    CollectRecord,
    FilterDocument,
    FilterRecord,
    RejectedRecord,
    Repository,
)
from basket_collector.git import GitRepository, GitStopRequested
from basket_collector.github import GitHubClient
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.llm import GenerationInterrupted, LLMClient
from basket_collector.pipeline import Pipeline, PipelineInterrupted, Shutdown
from basket_collector.stages.filter import RemoteVCS, process_one
from basket_collector.storage import read_checkpoint, read_document
from fixture_helpers import native_config
from test_llm import _config as llm_config
from test_llm import _response as llm_response
from test_pipeline import FakeGitHub, FakeGitRepository, _config, _filter_one, _producer


def test_sigint_between_filter_requests_keeps_pending_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop after an active PR response while coordinator propagation is delayed."""
    config = _config(tmp_path)
    config.source.requests_per_second = 100000
    producer = _producer()
    assert (
        Pipeline(config, producer=producer, remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run('collect')
        == 0
    )
    shutdown = Shutdown()
    propagation_allowed = threading.Event()
    propagation_allowed.set()
    original_sync = shutdown.sync
    paths: list[str] = []
    processes: list[object] = []
    completed: list[int] = []

    def delayed_sync() -> bool:
        """Keep the actual event unset until the active per-PR operation finishes."""
        return original_sync() if propagation_allowed.is_set() else False

    def tracked_process_one(
        record: CollectRecord,
        repository: GitRepository,
        github: RemoteVCS,
        settings: FilterConfig,
        stop_event: threading.Event | None = None,
        *,
        hosting_repository: Repository,
    ) -> FilterRecord | RejectedRecord:
        """Expose the request-to-request gap without coordinator intervention."""
        propagation_allowed.clear()
        try:
            return process_one(record, repository, github, settings, stop_event, hosting_repository=hosting_repository)
        finally:
            propagation_allowed.set()

    def response(request: httpx.Request) -> httpx.Response:
        """Return the active PR response after SIGINT and flag any further request."""
        paths.append(request.url.path)
        if len(paths) != 1:
            raise httpx.ReadTimeout('unexpected request after signal', request=request)
        os.kill(os.getpid(), signal.SIGINT)
        deadline = time.monotonic() + 5
        while not shutdown.requested and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        assert shutdown.requested
        assert not threading.Event.is_set(shutdown.stop_event)
        completed.append(1)
        return httpx.Response(200, json=FakeGitHub().get_pull_request('owner/repo', 1))

    def forbidden_popen(*args: object, **kwargs: object) -> None:
        """Record and forbid any Git launch during the interrupted worker."""
        processes.append(args[0])
        raise AssertionError('Git started after signal')

    repository = GitRepository(tmp_path / 'bare', str(tmp_path / 'unused-source'), 10, shutdown.stop_event)
    repository.prepare()
    monkeypatch.setattr(shutdown, 'sync', delayed_sync)
    monkeypatch.setattr('basket_collector.pipeline.process_one', tracked_process_one)
    monkeypatch.setattr(repository, 'prepare', lambda: None)
    monkeypatch.setattr(subprocess, 'Popen', forbidden_popen)
    github = GitHubClient(
        native_config(config.source),
        token='offline-token',
        transport=httpx.MockTransport(response),
        stop_event=shutdown.stop_event,
    )
    pipeline = Pipeline(
        config,
        producer=producer,
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=repository,
        shutdown=shutdown,
    )
    with pytest.raises(PipelineInterrupted):
        pipeline.run('filter')
    assert completed == [1]
    assert paths == ['/repos/owner/repo/pulls/1']
    assert processes == []
    document, state = read_checkpoint(Path(config.output_dir) / '.work' / 'filter.json', FilterDocument)
    assert document.meta.status == 'interrupted' and document.meta.error is None
    assert document.meta.counts.pending == 1 and document.meta.counts.failed == 0
    assert document.tasks == [] and document.rejected == []
    assert state['phase'] == 'local'
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    resumed = Pipeline(
        config,
        producer=producer,
        remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
        git_repository=FakeGitRepository(),
    )
    assert resumed.run('filter', retry_failed=True) == 0
    finished = read_document(Path(config.output_dir) / 'filtered.json', FilterDocument)
    assert finished.meta.counts.succeeded == 1 and finished.meta.counts.pending == 0


def test_sigint_after_active_git_command_blocks_next_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain completed Git init but forbid verification before coordinator sync."""
    shutdown = Shutdown()
    repository = GitRepository(tmp_path / 'bare', str(tmp_path / 'unused-source'), 10, shutdown.stop_event)
    original_run = repository._run
    operations: list[str] = []

    def signal_after_init(*args: str, input_bytes: bytes | None = None) -> bytes:
        """Send SIGINT immediately after the active init process finishes."""
        result = original_run(*args, input_bytes=input_bytes)
        operations.append(args[0])
        if args[0] == 'init':
            signal.raise_signal(signal.SIGINT)
            assert shutdown.requested
            assert not threading.Event.is_set(shutdown.stop_event)
        return result

    monkeypatch.setattr(repository, '_run', signal_after_init)
    with shutdown.installed(), pytest.raises(GitStopRequested):
        repository.prepare()
    assert operations == ['init']
    assert (repository.path / 'HEAD').exists()


def test_sigint_during_llm_client_setup_blocks_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check signal intent again after async HTTP setup and before opening a stream."""
    monkeypatch.setenv('TEST_LLM_KEY', 'offline-key')
    shutdown = Shutdown()
    original_enter = httpx.AsyncClient.__aenter__
    requests: list[httpx.Request] = []

    async def signal_after_enter(client: httpx.AsyncClient) -> httpx.AsyncClient:
        """Request shutdown after the client enters its async context."""
        result = await original_enter(client)
        signal.raise_signal(signal.SIGINT)
        assert shutdown.requested
        assert not threading.Event.is_set(shutdown.stop_event)
        return result

    def response(request: httpx.Request) -> httpx.Response:
        """Capture an HTTP request that must never start after the signal."""
        requests.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncClient, '__aenter__', signal_after_enter)
    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = LLMClient(llm_config(), http_client=transport, stop_event=shutdown.stop_event)
        with shutdown.installed(), pytest.raises(GenerationInterrupted):
            client.generate('System', 'User')
    assert requests == []


def test_successful_llm_response_during_sigint_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the result of an active request while forbidding the next generation."""
    monkeypatch.setenv('TEST_LLM_KEY', 'offline-key')
    shutdown = Shutdown()
    requests: list[httpx.Request] = []

    def response(request: httpx.Request) -> httpx.Response:
        """Send a real signal during the successful active HTTP operation."""
        requests.append(request)
        signal.raise_signal(signal.SIGINT)
        assert shutdown.requested
        assert not threading.Event.is_set(shutdown.stop_event)
        return httpx.Response(200, json=llm_response('openai_responses'))

    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = LLMClient(llm_config(), http_client=transport, stop_event=shutdown.stop_event)
        with shutdown.installed():
            result = client.generate('System', 'User')
            assert result.task.task == 'Implement change' and result.attempts == 1
            with pytest.raises(GenerationInterrupted):
                client.generate('System', 'User')
    assert len(requests) == 1


def test_coordinator_propagates_and_announces_real_repeated_sigint(capsys: pytest.CaptureFixture[str]) -> None:
    """Expose intent before sync and propagate the underlying event exactly once announced."""
    shutdown = Shutdown()
    with shutdown.installed():
        signal.raise_signal(signal.SIGINT)
        assert shutdown.stop_event.is_set() and shutdown.stop_event.wait(0)
        assert not threading.Event.is_set(shutdown.stop_event)
        assert shutdown.sync() and threading.Event.is_set(shutdown.stop_event)
        assert threading.Event.wait(shutdown.stop_event, 0)
        signal.raise_signal(signal.SIGINT)
        assert shutdown.sync()
    assert capsys.readouterr().err.count('Stopping new operations;') == 1
