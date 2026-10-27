"""Per-change shallow-history failures preserve queue progress and durable outcomes."""

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_pipeline import BASE, FakeGitHub, FakeGitRepository, _config, _filter_one, _producer

from basket_collector.config import FilterConfig
from basket_collector.filters import compact_rejection
from basket_collector.models.artifacts import (
    BasketDocument,
    CollectRecord,
    FilterDocument,
    FilterRecord,
    RejectedRecord,
    RejectionReason,
    Repository,
)
from basket_collector.persistence.storage import StageDocument, StageStore, StorageError, read_checkpoint, read_document
from basket_collector.pipeline import Pipeline, PipelineInterrupted
from basket_collector.providers.github_source import GitHubRemoteVCS
from basket_collector.providers.remote import RemoteVCS
from basket_collector.runtime.logs import InvocationLogs
from basket_collector.stages.filter import FilterInterrupted
from basket_collector.vcs.git import GitFatalError, GitInfrastructureError, GitRepository


def history_rejection(record: CollectRecord) -> RejectedRecord:
    """Build the exact structured outcome produced by the native classifier."""
    return compact_rejection(
        record,
        RejectionReason(
            code='source_history_unavailable',
            message='Git could not obtain the frozen shallow history for this change.',
            observed={
                'cause': 'git_unshallow_object_missing',
                'operation': 'fetch',
                'reference': BASE,
                'depth': 2,
                'missing_commit': BASE,
            },
            expected='retrievable frozen Git history',
        ),
    )


def history_filter(bad_numbers: set[int], calls: list[int]) -> Callable[..., FilterRecord | RejectedRecord]:
    """Build a typed filter fixture that preserves the production call interface."""

    def filter_one(
        record: CollectRecord,
        repository: GitRepository,
        remote: RemoteVCS,
        config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord | RejectedRecord:
        """Return a rejection only for the selected fixture records."""
        calls.append(record.change_request.number)
        if record.change_request.number in bad_numbers:
            return history_rejection(record)
        return _filter_one(record, repository, remote, config, stop_event, hosting_repository=hosting_repository)

    return filter_one


@pytest.mark.parametrize('workers', [1, 3])
@pytest.mark.parametrize('bad_numbers', [{2}, {2, 3}])
@pytest.mark.parametrize('mode', ['plain', 'off'])
def test_history_rejection_completes_basket_and_reports_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    workers: int,
    bad_numbers: set[int],
    mode: str,
) -> None:
    """Persist all outcomes without stopping neighboring records or repeating work on reuse."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3, 4]
    config.filter.workers = workers
    config.filter.selection.limit = 10
    calls: list[int] = []

    monkeypatch.setattr('basket_collector.pipeline.process_one', history_filter(bad_numbers, calls))
    remote = GitHubRemoteVCS(config.source, client=FakeGitHub())
    logs = InvocationLogs(Path(config.output_dir), 'ERROR')
    try:
        pipeline = Pipeline(
            config,
            producer=_producer(),
            remote=remote,
            git_repository=FakeGitRepository(),
            logs=logs,
            progress_mode=mode,
        )
        assert pipeline.run('basket') == 0
    finally:
        logs.close()
    output = Path(config.output_dir)
    filtered = read_document(output / 'filtered.json', FilterDocument)
    basket = read_document(output / 'basket.json', BasketDocument)
    assert filtered.meta.counts.succeeded == 4 - len(bad_numbers)
    assert filtered.meta.counts.rejected == len(bad_numbers)
    assert filtered.meta.counts.pending == filtered.meta.counts.failed == 0
    assert filtered.meta.status == 'completed' and filtered.meta.error is None
    assert {r.change_request.number for r in basket.tasks} == {1, 2, 3, 4} - bad_numbers
    assert basket.meta.partial is False and basket.meta.omissions == []
    assert pipeline.shutdown.stop_event.is_set() is False
    console = capsys.readouterr().out
    assert console.count('rejected: source_history_unavailable;') == len(bad_numbers)
    assert f'source_history_unavailable={len(bad_numbers)}' in console
    assert BASE in console and '.work/filter.json' in console
    assert 'pull_request owner/repo #2' in console
    assert 'rejected: source_history_unavailable;' in logs.path.read_text()
    resumed = Pipeline(
        config,
        producer=_producer(),
        remote=remote,
        git_repository=FakeGitRepository(),
        progress_mode=mode,
    )
    assert resumed.run('basket') == 0
    assert sorted(calls) == [1, 2, 3, 4]
    console = capsys.readouterr().out
    assert 'rejected: source_history_unavailable;' not in console
    assert f'source_history_unavailable={len(bad_numbers)}' in console
    assert 'candidate_shortfall=' in logs.path.read_text()


def test_saved_history_rejection_survives_interrupt_and_producer_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resume skips saved rejections while a changed implementation remains incompatible."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3]
    config.filter.workers = 1
    remote = GitHubRemoteVCS(config.source, client=FakeGitHub())
    assert Pipeline(config, producer=_producer(), remote=remote).run('collect') == 0
    calls: list[int] = []

    monkeypatch.setattr('basket_collector.pipeline.process_one', history_filter({1}, calls))
    pipeline = Pipeline(config, producer=_producer(), remote=remote, git_repository=FakeGitRepository())
    original_checkpoint = pipeline._checkpoint

    def checkpoint(store: StageStore, document: StageDocument, state: dict[str, Any]) -> None:
        """Request shutdown only after a rejection has been durably saved."""
        original_checkpoint(store, document, state)
        if isinstance(document, FilterDocument) and document.rejected:
            pipeline.shutdown.request(2, None)

    monkeypatch.setattr(pipeline, '_checkpoint', checkpoint)
    with pytest.raises(PipelineInterrupted):
        pipeline.run('filter')
    output = Path(config.output_dir)
    saved, state = read_checkpoint(output / '.work/filter.json', FilterDocument)
    assert saved.meta.status == 'interrupted' and state['phase'] == 'local'
    assert saved.meta.counts.rejected == 1 and saved.meta.counts.pending == 2
    assert calls == [1]
    changed = _producer().model_copy(update={'version': 'different-product-version'})
    with pytest.raises(StorageError, match='producer changed'):
        Pipeline(config, producer=changed, remote=remote, git_repository=FakeGitRepository()).run('filter')
    capsys.readouterr()
    resumed = Pipeline(config, producer=_producer(), remote=remote, git_repository=FakeGitRepository())
    assert resumed.run('filter') == 0
    assert calls == [1, 2, 3]
    console = capsys.readouterr().out
    assert 'rejected: source_history_unavailable;' not in console
    assert 'source_history_unavailable=1' in console


def test_history_rejection_is_durable_beside_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain the rejection even when a neighboring worker stops the stage."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3, 4]
    config.filter.workers = 2
    remote = GitHubRemoteVCS(config.source, client=FakeGitHub())
    assert Pipeline(config, producer=_producer(), remote=remote).run('collect') == 0
    pipeline = Pipeline(config, producer=_producer(), remote=remote, git_repository=FakeGitRepository())
    saved = threading.Event()
    calls: list[int] = []
    original_checkpoint = pipeline._checkpoint

    def checkpoint(store: StageStore, document: StageDocument, state: dict[str, Any]) -> None:
        """Release the failing neighbor only after the rejection reaches disk."""
        original_checkpoint(store, document, state)
        if isinstance(document, FilterDocument) and document.rejected:
            saved.set()

    def filter_one(
        record: CollectRecord,
        repository: GitRepository,
        remote: RemoteVCS,
        config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord | RejectedRecord:
        """Synchronize rejection, infrastructure failure and already active work."""
        number = record.change_request.number
        calls.append(number)
        if number == 1:
            return history_rejection(record)
        if number == 2:
            assert saved.wait(5)
            raise GitFatalError('fixture network outage')
        assert number == 3
        assert pipeline.shutdown.stop_event.wait(5)
        raise FilterInterrupted('fixture active work stopped')

    monkeypatch.setattr(pipeline, '_checkpoint', checkpoint)
    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_one)
    with pytest.raises(GitInfrastructureError, match='fixture network outage'):
        pipeline.run('filter')
    document, _state = read_checkpoint(Path(config.output_dir) / '.work/filter.json', FilterDocument)
    assert document.meta.status == 'failed' and document.meta.error is not None
    assert document.meta.error.code == 'git_infrastructure_error' and document.meta.error.retryable
    assert document.meta.counts.rejected == 1 and document.meta.counts.pending == 3
    assert document.meta.counts.failed == 0
    assert [r.change_request.number for r in document.rejected] == [1]
    assert 4 not in calls


def test_all_history_rejections_preserve_empty_stage_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never generate or export an empty basket after rejecting the full population."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2]
    remote = GitHubRemoteVCS(config.source, client=FakeGitHub())

    monkeypatch.setattr('basket_collector.pipeline.process_one', history_filter({1, 2}, []))
    pipeline = Pipeline(config, producer=_producer(), remote=remote, git_repository=FakeGitRepository())
    assert pipeline.run('basket') == 3
    output = Path(config.output_dir)
    document = read_document(output / 'filtered.json', FilterDocument)
    assert document.meta.counts.rejected == 2 and document.meta.counts.pending == 0
    assert document.meta.error is None and document.meta.status == 'completed'
    assert not (output / 'tasks.json').exists() and not (output / 'basket.json').exists()


def test_broken_history_warning_stops_active_workers_before_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve a saved rejection while stopping neighbors when console output fails."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3]
    config.filter.workers = 2
    remote = GitHubRemoteVCS(config.source, client=FakeGitHub())
    assert Pipeline(config, producer=_producer(), remote=remote).run('collect') == 0
    pipeline = Pipeline(config, producer=_producer(), remote=remote, git_repository=FakeGitRepository())
    neighbor_started = threading.Event()
    warning_failed = threading.Event()
    calls: list[int] = []
    failures: list[BaseException] = []

    def filter_one(
        record: CollectRecord,
        repository: GitRepository,
        remote: RemoteVCS,
        config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord | RejectedRecord:
        """Keep the second worker active until the coordinator sets global stop."""
        number = record.change_request.number
        calls.append(number)
        if number == 1:
            assert neighbor_started.wait(5)
            return history_rejection(record)
        assert number == 2
        neighbor_started.set()
        assert pipeline.shutdown.stop_event.wait(5)
        raise FilterInterrupted('fixture active work stopped')

    def broken_warning(record: RejectedRecord, path: Path) -> None:
        """Simulate a closed output stream after the durable rejection checkpoint."""
        warning_failed.set()
        raise OSError('fixture output stream closed')

    def run() -> None:
        """Capture the original failure without blocking the test thread."""
        try:
            pipeline.run('filter')
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_one)
    monkeypatch.setattr(pipeline, '_print_history_rejection', broken_warning)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert warning_failed.wait(5)
        stopped = pipeline.shutdown.stop_event.wait(2)
    finally:
        pipeline.shutdown.stop_event.set()
        worker.join(timeout=5)
    assert stopped, 'coordinator waited for active workers without signalling stop'
    assert not worker.is_alive() and len(failures) == 1
    assert isinstance(failures[0], OSError) and str(failures[0]) == 'fixture output stream closed'
    document, _state = read_checkpoint(Path(config.output_dir) / '.work/filter.json', FilterDocument)
    assert document.meta.status == 'failed' and document.meta.counts.rejected == 1
    assert document.meta.counts.pending == 2 and 3 not in calls
