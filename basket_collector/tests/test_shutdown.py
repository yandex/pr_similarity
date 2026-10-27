"""Coordinator drains active work and preserves a resumable checkpoint."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, cast

import pytest
from test_pipeline import FakeGitHub, _config

from basket_collector.models.artifacts import CollectDocument, Producer, Repository
from basket_collector.persistence.storage import read_checkpoint, read_document
from basket_collector.pipeline import Pipeline, PipelineInterrupted, Shutdown
from basket_collector.providers.github import GitHubClient, GitHubInterrupted
from basket_collector.providers.github_source import GitHubRemoteVCS


class BlockingGitHub(GitHubClient):
    """Block an active PR fetch until shutdown requests cancellation."""

    def __init__(self, stop_event: threading.Event) -> None:
        """Create synchronization events and count PR fetches."""
        self.stop_event = stop_event
        self.entered = threading.Event()
        self.calls = 0

    def repository(self, full_name: str) -> Repository:
        """Return the repository metadata needed before PR collection."""
        return Repository(
            path=full_name,
            web_url=f'https://github.com/{full_name}',
            default_branch='develop',
            provider='github',
            server_url='https://github.com',
            project_id='123',
        )

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Wait for shutdown during a PR fetch and report interruption."""
        self.calls += 1
        self.entered.set()
        self.stop_event.wait(5)
        raise GitHubInterrupted('operation stopped')


class BlockingRepositoryGitHub(BlockingGitHub):
    """Block the initial repository lookup instead of a PR fetch."""

    def repository(self, full_name: str) -> Repository:
        """Wait for shutdown during repository metadata lookup."""
        self.entered.set()
        self.stop_event.wait(5)
        raise GitHubInterrupted('repository lookup stopped')


def test_repeated_stop_drains_and_keeps_pending_pr(tmp_path: Path) -> None:
    """Ignore repeated stop requests while preserving a pending PR."""
    shutdown = Shutdown()
    github = BlockingGitHub(shutdown.stop_event)
    producer = Producer(
        version='test',
        python_version='3.12.3',
        git_version='git test',
    )
    config = _config(tmp_path)
    pipeline = Pipeline(
        config, producer=producer, remote=GitHubRemoteVCS(config.source, client=github), shutdown=shutdown
    )
    outcome: list[object] = []

    def run() -> None:
        """Capture the worker result or interruption for the main thread."""
        try:
            outcome.append(pipeline.run('collect'))
        except PipelineInterrupted as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert github.entered.wait(5)
    shutdown.request(2, None)
    shutdown.request(2, None)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], PipelineInterrupted)
    document, state = read_checkpoint(Path(config.output_dir) / '.work' / 'collect.json', CollectDocument)
    assert document.meta.status == 'interrupted'
    assert document.meta.counts.input == 1 and document.meta.counts.pending == 1
    assert state['numbers'] == [1]
    assert github.calls == 1

    class RefetchForbidden(FakeGitHub):
        """Reject repository metadata refetch while resuming a checkpoint."""

        def repository(self, full_name: str) -> Repository:
            """Fail if resume fetches already-saved repository metadata."""
            raise AssertionError('resume must use the default branch saved in the checkpoint')

    resumed = Pipeline(config, producer=producer, remote=GitHubRemoteVCS(config.source, client=RefetchForbidden()))
    assert resumed.run('collect', retry_failed=True) == 0
    collected = Path(config.output_dir) / 'collected.json'
    assert collected.exists()
    assert read_document(collected, CollectDocument).meta.repository.default_branch == 'develop'


def test_signal_between_schedule_check_and_submit_does_not_start_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid submitting new work when shutdown starts between checks."""
    shutdown = Shutdown()
    pipeline = Pipeline(
        _config(tmp_path),
        producer=Producer(
            version='test',
            python_version='3.12.3',
            git_version='git test',
        ),
        shutdown=shutdown,
    )
    started: list[int] = []
    original_submit = cast(Callable[..., object], ThreadPoolExecutor.submit)

    def signal_inside_submit(
        self: ThreadPoolExecutor, operation: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        """Request shutdown immediately before executor submission."""
        shutdown.request(2, None)
        return original_submit(self, operation, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, 'submit', signal_inside_submit)
    pipeline._run_items([1], 1, lambda item: started.append(item) or item, lambda item, result: None)
    assert shutdown.requested
    assert started == []


def test_stop_during_first_repository_lookup_saves_resumable_collect(
    tmp_path: Path,
) -> None:
    """Save a resumable checkpoint when the first lookup is interrupted."""
    shutdown = Shutdown()
    github = BlockingRepositoryGitHub(shutdown.stop_event)
    producer = Producer(
        version='test',
        python_version='3.12.3',
        git_version='git test',
    )
    config = _config(tmp_path)
    pipeline = Pipeline(
        config, producer=producer, remote=GitHubRemoteVCS(config.source, client=github), shutdown=shutdown
    )
    outcome: list[object] = []

    def run() -> None:
        """Capture a repository lookup interruption in the worker thread."""
        try:
            outcome.append(pipeline.run('collect'))
        except PipelineInterrupted as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert github.entered.wait(5)
    shutdown.request(2, None)
    shutdown.request(2, None)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], PipelineInterrupted)
    document, state = read_checkpoint(
        Path(config.output_dir) / '.work' / 'collect.json',
        CollectDocument,
    )
    assert document.meta.status == 'interrupted'
    assert state['phase'] == 'repository'
    assert document.meta.counts.input == 0
    assert (
        Pipeline(config, producer=producer, remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run('collect')
        == 0
    )
