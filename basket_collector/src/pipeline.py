"""Single-writer stage orchestration, resume and graceful shutdown."""

import argparse
import importlib
import os
import queue
import re
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from basket_collector.attempts import AttemptJournal, RecordResult
from basket_collector.checkpoint_inputs import validate_checkpoint_input
from basket_collector.config import (
    CollectorConfig,
    TemplateSnapshot,
    effective_settings,
    load_config,
    load_environment,
    load_template_snapshot,
)
from basket_collector.contracts import (
    BasketDocument,
    BasketMeta,
    CollectDocument,
    CollectFailure,
    CollectMeta,
    CollectRecord,
    Counts,
    Coverage,
    Discovery,
    FailedRecord,
    FilterDocument,
    FilterFailure,
    FilterMeta,
    FilterRecord,
    GenerateMeta,
    InputReceipt,
    LineageReceipt,
    Producer,
    RecordIdentity,
    RejectedRecord,
    Repository,
    StageError,
    TaskRecord,
    TasksDocument,
)
from basket_collector.export_validation import validate_export_accounting
from basket_collector.git import (
    GitDiagnosticContext,
    GitFatalError,
    GitInfrastructureError,
    GitRepository,
    GitStopRequested,
)
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.gitlab_source import GitLabRemoteVCS
from basket_collector.llm import (
    GenerationInterrupted,
    LLMAuthenticationError,
    LLMCallError,
    LLMClient,
    LLMConfigurationError,
)
from basket_collector.logs import InvocationLogs, friendly_error
from basket_collector.progress import MODES, Progress
from basket_collector.record_runtime import RecordRuntime
from basket_collector.remote import ChangeRejected, RemoteError, RemoteInterrupted, RemoteVCS
from basket_collector.selection import build_selection, omissions, selected_records, selection_receipt, semantic_record
from basket_collector.sources import SourceDescriptor, change_id, source_hash
from basket_collector.stages.collect import (
    DiscoveryResult,
    collect_change,
    discover_date_range,
    select_change_ids,
)
from basket_collector.stages.export import export_tasks
from basket_collector.stages.filter import (
    FilterInterrupted,
    SourceChangedError,
    process_one,
)
from basket_collector.stages.generate import generate_record
from basket_collector.storage import (
    StageDocument,
    StageStore,
    StorageError,
    current_producer,
    expected_input,
    read_document,
    receipt,
    sha256,
    single_writer,
    stage_sha256,
    utc_now,
)
from basket_collector.transport_scope import generation_requests
from basket_collector.vcs import VCS

T = TypeVar('T')
R = TypeVar('R')
DocumentT = TypeVar('DocumentT', bound=StageDocument)
STAGES = ('collect', 'filter', 'generate', 'export')
DOCUMENTS: dict[str, type[CollectDocument] | type[FilterDocument] | type[TasksDocument] | type[BasketDocument]] = {
    'collect': CollectDocument,
    'filter': FilterDocument,
    'generate': TasksDocument,
    'export': BasketDocument,
}


class PipelineInterrupted(RuntimeError):
    """A first SIGINT or SIGTERM drained workers and left a checkpoint."""


class PipelineError(RuntimeError):
    """An infrastructure or stage operation failed."""


class DecisionRequired(PipelineError):
    """Completed record work needs explicit permission before selection or publication."""


class EmptyStageError(PipelineError):
    """A definitive empty population cannot proceed to a downstream stage."""

    def __init__(self, stage: str, *, predecessor: str | None = None, details: str = '') -> None:
        """Provide a stable error code and actionable summary for the owning stage."""
        self.code = 'empty_stage_input' if predecessor is not None else 'empty_stage_output'
        self.stage = stage
        message = (
            f'{stage}: {predecessor} contains no tasks; rebuild that stage.'
            if predecessor is not None
            else f'{stage}: no tasks remain; check the selection and saved rejection/failure reasons.'
        )
        super().__init__(message + (' ' + details if details else ''))


def _stage_error(stage: str, error: Exception) -> StageError:
    """Map a stage exception to a durable retry policy without exposing secrets."""
    if isinstance(error, EmptyStageError):
        return StageError(code=error.code, message=str(error), retryable=False)
    if isinstance(error, SourceChangedError):
        return StageError(code='source_changed', message=str(error), retryable=False)
    if isinstance(error, LLMAuthenticationError):
        # Corrected credentials in a new process can resume this checkpoint
        # without discarding already generated tasks.
        return StageError(code='llm_auth_error', message=str(error), retryable=True)
    if isinstance(error, LLMConfigurationError):
        return StageError(code='llm_configuration', message=str(error), retryable=False)
    if isinstance(error, LLMCallError):
        return StageError(code=error.code, message=str(error), retryable=error.retryable)
    if isinstance(error, FileNotFoundError):
        return StageError(code='missing_file', message=str(error), retryable=False)
    if isinstance(error, RemoteError):
        # HTTP auth is not retried inside one request, but a corrected token can
        # resume a stage checkpoint without discarding already collected PRs.
        return StageError(
            code=error.code, message=str(error), retryable=error.retryable or error.code == 'github_auth_error'
        )
    if isinstance(error, GitInfrastructureError):
        return StageError(code=error.code, message=str(error), retryable=error.retryable)
    return StageError(code=f'{stage}_failed', message=friendly_error(error), retryable=True)


class _TaskSkippedForShutdown(Exception):
    """A queued item lost the race to the first shutdown request."""


class _SignalAwareEvent(threading.Event):
    """Expose signal intent immediately while the coordinator wakes event waiters."""

    def __init__(self, requested: Callable[[], bool]) -> None:
        """Attach a lock-free intent reader to the normal threading event."""
        super().__init__()
        self._requested = requested

    def is_set(self) -> bool:
        """Observe both signal intent and a coordinator-propagated stop."""
        return self._requested() or super().is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Skip waiting after signal intent and retain normal coordinator wakeups."""
        if self.is_set():
            return True
        signaled = super().wait(timeout)
        return signaled or self.is_set()


class Shutdown:
    """Flag intent in signal handlers and wake worker waiters in the coordinator."""

    def __init__(self) -> None:
        """Initialize signal intent and the worker stop event."""
        self.requested = False
        self.stop_event = _SignalAwareEvent(self._is_requested)
        self._announced = False
        self.logs: InvocationLogs | None = None

    def _is_requested(self) -> bool:
        """Read signal intent without acquiring any event or scheduler lock."""
        return self.requested

    def request(self, signum: int, frame: object) -> None:
        """Record a termination request without blocking inside the signal handler."""
        self.requested = True

    def sync(self) -> bool:
        """Propagate the signal request to workers and announce draining once."""
        if self.requested:
            self.stop_event.set()
            if not self._announced:
                message = (
                    'Stopping new operations; waiting for active work and saving checkpoint. '
                    'Repeated Ctrl+C is ignored.'
                )
                if self.logs is not None:
                    self.logs.message(message, stream=sys.stderr)
                else:
                    print(message, file=sys.stderr, flush=True)
                self._announced = True
        return self.stop_event.is_set()

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Install handlers that ignore repeated signals until the run exits."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        old_int = signal.getsignal(signal.SIGINT)
        old_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self.request)
        signal.signal(signal.SIGTERM, self.request)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)


def _counts(total: int, succeeded: int, rejected: int = 0, failed: int = 0) -> Counts:
    """Build stage counts and derive the number of pending records."""
    return Counts(
        input=total,
        succeeded=succeeded,
        rejected=rejected,
        failed=failed,
        pending=total - succeeded - rejected - failed,
    )


def _empty_collect_details(document: CollectDocument) -> str:
    """Describe the exhausted collection population without record payloads."""
    return (
        f'Matched={document.meta.discovery.matched}; selected={document.meta.discovery.selected}; '
        f'rejected={len(document.rejected)}.'
    )


def _empty_filter_details(document: FilterDocument) -> str:
    """Summarize retained rejection reasons for a definitive empty filter stage."""
    reasons: dict[str, int] = {}
    for item in document.rejected:
        for reason in item.reasons:
            reasons[reason.code] = reasons.get(reason.code, 0) + 1
    return 'Rejected=' + str(len(document.rejected)) + '; reasons=' + str(reasons) + '.'


class Pipeline:
    """Runs one stage or the ordered basket pipeline under one output lock."""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        project_dir: Path | None = None,
        producer: Producer | None = None,
        remote: RemoteVCS | None = None,
        git_repository: VCS | None = None,
        llm_client: LLMClient | None = None,
        shutdown: Shutdown | None = None,
        logs: InvocationLogs | None = None,
        progress: Progress | None = None,
        progress_mode: str = 'auto',
    ) -> None:
        """Set up clients and provenance without opening network connections."""
        self.config = config
        self.output_dir = Path(config.output_dir)
        self.project_dir = project_dir
        self.progress = progress or Progress(
            progress_mode,
            level=logs.level if logs is not None else 'INFO',
            redact=logs.formatter.redact if logs is not None else lambda text: text,
            log=logs.progress_snapshot if logs is not None else None,
        )
        if logs is not None:
            logs.progress = self.progress
        self.producer = producer or current_producer(project_dir)
        self.remote = remote
        self.git_repository = git_repository
        self.logs = logs
        self.llm_client = llm_client
        self.shutdown = shutdown or Shutdown()
        self.shutdown.logs = logs
        self._scheduler_lock = threading.Lock()
        self._owns_remote = False
        self._owns_llm = False
        self._generation_snapshot: TemplateSnapshot | None = None
        self.stage_modes: dict[str, str] = {}
        self.allow_partial = False
        self.reselect = False
        self.overwrite = False
        self.retry_failed = False
        self._worker_messages: queue.SimpleQueue[str] = queue.SimpleQueue()

    def _remote(self) -> RemoteVCS:
        """Create the selected hosting adapter only after verified stage input."""
        if self.remote is None:
            adapter = GitHubRemoteVCS if self.config.source.provider == 'github' else GitLabRemoteVCS
            remote = adapter(self.config.source, stop_event=self.shutdown.stop_event)
            remote.client.wait_callback = self.progress.wait
            self.remote = remote
            self._owns_remote = True
        return self.remote

    def _llm(self) -> LLMClient:
        """Get the shared LLM client, creating it only when needed."""
        if self.llm_client is None:
            self.llm_client = LLMClient(self.config.generation, stop_event=self.shutdown.stop_event)
            self._owns_llm = True
        return self.llm_client

    def close(self) -> None:
        """Close owned clients after workers, then finish the optional display."""
        try:
            if self._owns_llm and self.llm_client is not None:
                self.llm_client.close()
            if self._owns_remote and self.remote is not None:
                self.remote.close()
        finally:
            self.progress.close()

    def _poll_progress(self) -> None:
        """Observe shutdown without changing scheduling or checkpoint state."""
        while not self._worker_messages.empty():
            self._message(self._worker_messages.get_nowait(), level='WARNING')
        if self.shutdown.sync():
            self.progress.update(status='draining')
        self.progress.poll()

    def _wait_one(self, future: Future[T], on_poll: Callable[[], None] | None = None) -> T:
        """Poll one future while flushing coordinator progress and signal state."""
        while True:
            if on_poll is not None:
                on_poll()
            self._poll_progress()
            done, _ = wait([future], timeout=0.1, return_when=FIRST_COMPLETED)
            if done:
                if on_poll is not None:
                    on_poll()
                self.shutdown.sync()
                return future.result()

    def _run_one(self, operation: Callable[[], T], on_poll: Callable[[], None] | None = None) -> T:
        """Guard one operation against shutdown and flush progress while waiting."""

        def guarded() -> T:
            """Reject a queued operation if shutdown won the scheduling race."""
            with self._scheduler_lock:
                if self.shutdown.requested or self.shutdown.stop_event.is_set():
                    raise _TaskSkippedForShutdown()
            self.progress.worker_started('HTTP request')
            try:
                return operation()
            finally:
                self.progress.worker_finished()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self._scheduler_lock:
                if self.shutdown.requested or self.shutdown.stop_event.is_set():
                    self.shutdown.sync()
                    raise PipelineInterrupted()
                future = executor.submit(guarded)
            try:
                return self._wait_one(future, on_poll)
            except _TaskSkippedForShutdown as exc:
                self.shutdown.sync()
                raise PipelineInterrupted() from exc

    def _run_items(
        self,
        items: list[T],
        workers: int,
        operation: Callable[[T], R],
        accept: Callable[[T, R], None],
        on_poll: Callable[[], None] | None = None,
    ) -> None:
        """Run bounded workers and pass their completed results to the writer."""
        remaining = iter(items)
        active: dict[Future[R], T] = {}
        failure: BaseException | None = None

        def guarded(item: T) -> R:
            """Reject a queued PR if shutdown won the scheduling race."""
            with self._scheduler_lock:
                if self.shutdown.requested or self.shutdown.stop_event.is_set():
                    raise _TaskSkippedForShutdown()
            reasons = {'collect': 'HTTP request', 'filter': 'Git operation', 'generate': 'task processing'}
            if self.config.generation.source == 'llm':
                reasons['generate'] = 'LLM request'
            reason = reasons.get(self.progress.current or '', 'processing')
            self.progress.worker_started(reason)
            try:
                return operation(item)
            finally:
                self.progress.worker_finished()

        with ThreadPoolExecutor(max_workers=workers) as executor:

            def fill() -> None:
                """Fill available worker slots while new operations remain allowed."""
                while len(active) < workers and not self.shutdown.sync():
                    try:
                        item = next(remaining)
                    except StopIteration:
                        return
                    with self._scheduler_lock:
                        if self.shutdown.requested or self.shutdown.stop_event.is_set():
                            return
                        active[executor.submit(guarded, item)] = item

            fill()
            while active:
                if on_poll is not None:
                    on_poll()
                self._poll_progress()
                done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    item = active.pop(future)
                    if future.cancelled():
                        continue
                    try:
                        result = future.result()
                        accept(item, result)
                    except (
                        RemoteInterrupted,
                        GenerationInterrupted,
                        FilterInterrupted,
                        _TaskSkippedForShutdown,
                    ) as exc:
                        if not self.shutdown.sync() and failure is None:
                            failure = exc
                            self.shutdown.stop_event.set()
                        continue
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                            self.shutdown.stop_event.set()
                        continue
                if failure is not None or self.shutdown.sync():
                    for future in active:
                        future.cancel()
                else:
                    fill()
        self._poll_progress()
        if failure is not None:
            raise failure

    def _lineage(self, predecessors: list[CollectDocument | FilterDocument | TasksDocument]) -> list[LineageReceipt]:
        """Copy complete predecessor receipts into a stage document."""
        return [receipt(item) for item in predecessors]

    def _meta_fields(
        self,
        stage: str,
        repository: Repository,
        lineage: list[LineageReceipt],
        counts: Counts,
        *,
        status: str = 'running',
        started_at: str | None = None,
        error: StageError | None = None,
        template_snapshot: TemplateSnapshot | None = None,
    ) -> dict[str, Any]:
        """Build shared stage metadata from settings, lineage, and producer identity."""
        now = utc_now()
        settings = effective_settings(self.config, stage, template_snapshot=template_snapshot)
        return {
            'schema_version': 2,
            'stage': stage,
            'status': status,
            'input': InputReceipt(stage=lineage[-1].stage, sha256=lineage[-1].sha256) if lineage else None,
            'settings': settings,
            'settings_hash': sha256(settings),
            'producer': self.producer,
            'lineage': lineage,
            'repository': repository,
            'started_at': started_at or now,
            'updated_at': now,
            'completed_at': now if status == 'completed' else None,
            'counts': counts,
            'error': error,
        }

    def _verified_predecessors(self, stage: str) -> list[CollectDocument | FilterDocument | TasksDocument]:
        """Load completed predecessors and validate their current identity and lineage."""
        stages = STAGES[: STAGES.index(stage)]
        documents: list[CollectDocument | FilterDocument | TasksDocument] = []
        for name in stages:
            model = DOCUMENTS[name]
            store = StageStore(self.output_dir, name)
            previous = documents[-1] if documents else None
            loaded, checkpoint = store.load(
                model,
                settings=effective_settings(
                    self.config,
                    name,
                    template_snapshot=self._generation_snapshot if name == 'generate' else None,
                ),
                producer=self.producer,
                input_receipt=expected_input(previous) if previous is not None else None,
                validate_loaded=lambda saved, state: validate_checkpoint_input(saved, previous, state),
            )
            if loaded is None or checkpoint is not None or loaded.meta.status != 'completed':
                raise StorageError(f'{name} is absent or unfinished; complete it before {stage}')
            if not isinstance(loaded, (CollectDocument, FilterDocument, TasksDocument)):
                raise StorageError(f'{name} has an invalid predecessor document type')
            if loaded.meta.lineage != self._lineage(documents):
                raise StorageError(f'{name} lineage differs from current predecessors; use OVERWRITE=1')
            if isinstance(loaded, FilterDocument) and stage in ('generate', 'export'):
                collected = documents[0]
                assert isinstance(collected, CollectDocument)
                proposal = build_selection(
                    loaded,
                    collected,
                    self.config,
                    self.producer,
                    self._generation_snapshot or load_template_snapshot(self.config.generation),
                )
                if (
                    loaded.selection is not None
                    and proposal.generation_settings_sha256 != loaded.selection.generation_settings_sha256
                ):
                    raise StorageError(
                        'generation settings differ from selection; recompute filter or basket with OVERWRITE=1'
                    )
                if loaded.selection is None or proposal.sha256 != loaded.selection.sha256:
                    raise DecisionRequired('selection inputs changed; run filter or basket with RESELECT=1 as needed')
            documents.append(loaded)
        return documents

    def _update(
        self, document: DocumentT, *, counts: Counts, status: str, error: StageError | None = None
    ) -> DocumentT:
        """Return a validated document with updated counts, status, and timestamps."""
        now = utc_now()
        meta = document.meta.model_copy(
            update={
                'counts': counts,
                'status': status,
                'updated_at': now,
                'completed_at': now if status == 'completed' else None,
                'error': error,
            }
        )
        return type(document).model_validate({**document.model_dump(mode='json'), 'meta': meta.model_dump(mode='json')})

    def _repository(self) -> Repository:
        """Fetch the repository identity through the guarded worker scheduler."""
        return self._run_one(lambda: self._remote().resolve_repository(self.config.source.project))

    def _current_input(self, stage: str) -> InputReceipt:
        """Hash the current completed predecessor before publication."""
        model = DOCUMENTS[stage]
        path = StageStore(self.output_dir, stage).path
        current = read_document(path, model)
        if current.meta.status != 'completed':
            raise StorageError(f'{stage} is no longer completed')
        return expected_input(current)

    def _observe_document(
        self,
        document: CollectDocument | FilterDocument | TasksDocument | BasketDocument,
        state: dict[str, Any] | None = None,
    ) -> None:
        """Project saved counters into the display without adding artifact fields."""
        stage = document.meta.stage
        counts = document.meta.counts
        saved_phase = (state or {}).get('phase')
        metrics: dict[str, Any] = {'pending': counts.pending, 'failed': counts.failed, 'wait': ''}
        if document.meta.status != 'running':
            metrics['active'] = 0
        n, total = counts.succeeded + counts.rejected + counts.failed, counts.input
        if stage == 'collect':
            phase = saved_phase if saved_phase in ('repository', 'discovery') else 'records'
            if phase == 'repository':
                n, total = 0, None
            elif phase == 'discovery':
                assert state is not None
                n, total = max(0, state['next_page'] - 1), None
                metrics.update(matched=len(state['matches']), next_page=state['next_page'])
            else:
                assert isinstance(document, CollectDocument)
                metrics.update(matched=document.meta.discovery.matched, next_page=None, rejected=counts.rejected)
        elif stage == 'filter':
            phase = 'local' if saved_phase == 'local' else 'selection'
            metrics.update(rejected=counts.rejected)
            if phase == 'local':
                metrics['candidate_accepted'] = counts.succeeded
            else:
                metrics['selected'] = counts.succeeded
            if saved_phase == 'ready' or document.meta.status == 'completed':
                self.progress.basket_update(available=counts.succeeded)
        elif stage == 'generate':
            phase = 'tasks'
            metrics['successful'] = counts.succeeded
            metrics['rejected'] = counts.rejected
            self.progress.basket_update(successful=counts.succeeded, failed=counts.failed)
        else:
            assert isinstance(document, BasketDocument)
            phase = 'writing'
            self.progress.basket_update(
                successful=len(document.tasks),
                failed=sum(item.kind == 'failed' for item in document.meta.omissions),
                omitted=len(document.meta.omissions),
            )
        self.progress.update(
            phase=phase,
            status=document.meta.status,
            mode=self.stage_modes.get(stage, 'new'),
            n=n,
            total=total,
            **metrics,
        )

    def _checkpoint(
        self,
        store: StageStore,
        document: CollectDocument | FilterDocument | TasksDocument | BasketDocument,
        state: dict[str, Any],
    ) -> None:
        """Save at the original boundary, then report only durable results."""
        self.progress.update(wait='saving checkpoint')
        store.checkpoint(document, state)
        self._observe_document(document, state)

    def _publish(
        self,
        store: StageStore,
        document: CollectDocument | FilterDocument | TasksDocument | BasketDocument,
        state: dict[str, Any],
        input_stage: str | None = None,
    ) -> None:
        """Publish a completed document only if its input and shutdown state are valid."""
        current_input = self._current_input(input_stage) if input_stage is not None else None
        if self.shutdown.sync():
            interrupted = self._update(document, counts=document.meta.counts, status='interrupted')
            self._checkpoint(store, interrupted, state)
            raise PipelineInterrupted()
        self.progress.update(wait='saving checkpoint')
        store.publish(document, current_input=current_input, operation_state=state)
        self._observe_document(document, state)

    def _initial_collect(
        self,
        store: StageStore,
    ) -> tuple[CollectDocument, dict[str, Any], Repository]:
        """Create the initial repository checkpoint before any API request."""
        source = self.config.source
        path = source.project if '/' in source.project else 'unresolved/' + source.project
        repository = Repository(
            provider=source.provider,
            server_url=source.server_url,
            project_id='unresolved',
            path=path,
            web_url=source.server_url + '/' + path,
            default_branch=None,
        )
        initial = self._meta_fields('collect', repository, [], _counts(0, 0))
        document = CollectDocument(
            meta=CollectMeta(**initial, discovery=Discovery(matched=0, selected=0, capped=False)),
            tasks=[],
        )
        state = {'phase': 'repository', 'next_page': 1, 'matches': [], 'numbers': []}
        self._checkpoint(store, document, state)
        return document, state, repository

    def _discover_collect(
        self,
        document: CollectDocument,
        state: dict[str, Any],
        repository: Repository,
        store: StageStore,
    ) -> tuple[DiscoveryResult, list[int]]:
        """Freeze the PR queue and checkpoint each completed discovery page."""
        if self.config.collect.selection == 'change_ids':
            discovery = select_change_ids(self.config.collect)
            numbers = list(discovery.numbers)
        else:
            messages: queue.SimpleQueue[tuple[int, tuple[tuple[str, int], ...]]] = queue.SimpleQueue()

            def page_progress(page: int, matches: tuple[tuple[str, int], ...]) -> None:
                """Queue a page result for the single writer to persist."""
                messages.put((page, matches))

            def save_pages() -> None:
                """Save all pages that finished while the discovery worker ran."""
                while not messages.empty():
                    page, matches = messages.get_nowait()
                    state.update({'next_page': page, 'matches': [list(item) for item in matches]})
                    self._checkpoint(store, document, state)

            discovery = self._run_one(
                lambda: discover_date_range(
                    self._remote(),
                    repository,
                    self.config.collect,
                    start_page=state['next_page'],
                    previous_matches=tuple((str(item[0]), int(item[1])) for item in state['matches']),
                    on_page=page_progress,
                ),
                save_pages,
            )
            numbers = list(discovery.numbers)
        return discovery, numbers

    def _collected_with_discovery(
        self,
        document: CollectDocument,
        repository: Repository,
        discovery: DiscoveryResult,
        numbers: list[int],
    ) -> CollectDocument:
        """Attach frozen discovery metadata before publishing the PR queue."""
        return CollectDocument(
            meta=CollectMeta(
                **self._meta_fields(
                    'collect',
                    repository,
                    [],
                    _counts(len(numbers), len(document.tasks)),
                    started_at=document.meta.started_at,
                ),
                discovery=discovery.discovery,
            ),
            tasks=document.tasks,
        )

    def _record_runtime(self, stage: str, state: dict[str, Any], persist: Callable[[], None]) -> RecordRuntime:
        """Retry controlled record failures while propagating environment and programming defects."""

        def classify(error: Exception) -> StageError | None:
            """Classify at the owning operation boundary, not from broad message guesses."""
            if self.logs is not None:
                self.logs.diagnostic(error)
            if isinstance(error, (GitFatalError, LLMAuthenticationError, LLMConfigurationError, SourceChangedError)):
                return None
            if isinstance(error, RemoteError) and error.code in {
                'source_auth_error',
                'github_auth_error',
                'source_changed',
                'source_identity_changed',
            }:
                return None
            if isinstance(error, (RemoteError, GitInfrastructureError, LLMCallError)):
                return _stage_error(stage, error)
            return None

        def observe(identity: str, journal: AttemptJournal) -> None:
            """Report durable retry and failure decisions on the coordinator thread."""
            current = journal.current
            error = current.attempts[-1].error
            assert error is not None
            outcome = 'failed' if current.exhausted else 'retry scheduled'
            diagnostic = f'; DEBUG log: {self.logs.path}' if self.logs is not None else ''
            self._message(
                f'{stage}: {identity} {outcome}; attempt={len(current.attempts)}/{current.limit} '
                f'code={error.code}; {error.message}{diagnostic}',
                level='WARNING',
            )

        return RecordRuntime(
            getattr(self.config, stage if stage != 'generate' else 'generation'),
            state,
            self.shutdown.stop_event,
            persist,
            classify,
            (RemoteInterrupted, FilterInterrupted, GenerationInterrupted, GitStopRequested),
            retry_pending=self.retry_failed,
            observe=observe,
        )

    def _collect_records(
        self,
        document: CollectDocument,
        state: dict[str, Any],
        store: StageStore,
        update: Callable[[CollectDocument], None],
    ) -> CollectDocument:
        """Process the frozen queue with durable per-record attempts and ordered outcomes."""
        repository = document.meta.repository
        numbers = state['numbers']
        accepted = {item.change_request.number: item for item in document.tasks}
        rejected = {item.change_request.number: item for item in document.rejected}
        failed = {item.identity.number: item for item in document.failed}
        runtime = self._record_runtime('collect', state, lambda: self._checkpoint(store, document, state))
        kind = 'pull_request' if repository.provider == 'github' else 'merge_request'

        def identity(number: int) -> RecordIdentity:
            """Derive identity solely from the frozen discovery queue and repository."""
            return RecordIdentity(
                id=change_id(repository.provider, repository.server_url, repository.project_id, kind, number),
                kind=kind,
                repository=repository.path,
                number=number,
            )

        def operation(number: int) -> RecordResult[CollectRecord | RejectedRecord]:
            """Translate a deterministic exclusion into a successful rejected outcome."""
            record_identity = identity(number)

            def collect(journal: AttemptJournal) -> CollectRecord | RejectedRecord:
                """Execute one complete hosting operation without inner transport retries."""
                try:
                    return collect_change(self._remote(), repository, number, self.config.collect)
                except ChangeRejected as error:
                    if error.record.id != record_identity.id:
                        raise ValueError('source rejected another change') from error
                    return error.record

            return runtime.run(record_identity.id, sha256(record_identity), collect)

        def accept(number: int, result: RecordResult[CollectRecord | RejectedRecord]) -> None:
            """Commit the terminal journal and record outcome in one checkpoint."""
            nonlocal document
            record_identity = identity(number)
            runtime.accept(record_identity.id, result)
            if result.needs_budget:
                self._checkpoint(store, document, state)
                return
            if result.error is not None:
                assert result.journal is not None
                failed[number] = CollectFailure(
                    identity=record_identity, error=result.error, attempts=len(result.journal.current.attempts)
                )
            elif isinstance(result.value, RejectedRecord):
                rejected[number] = result.value
            elif isinstance(result.value, CollectRecord):
                if result.value.id != record_identity.id:
                    raise ValueError('source returned another change')
                accepted[number] = result.value
            else:
                raise ValueError('invalid collect operation result')
            document = document.model_copy(
                update={
                    'tasks': [accepted[number] for number in numbers if number in accepted],
                    'rejected': [rejected[number] for number in numbers if number in rejected],
                    'failed': [failed[number] for number in numbers if number in failed],
                }
            )
            document = self._update(
                document, counts=_counts(len(numbers), len(accepted), len(rejected), len(failed)), status='running'
            )
            update(document)
            self._checkpoint(store, document, state)

        pending = [
            number for number in numbers if number not in accepted and number not in rejected and number not in failed
        ]
        self._run_items(pending, 1, operation, accept, runtime.poll)
        return document

    def _collect(self, overwrite: bool, retry_failed: bool = False) -> CollectDocument:
        """Resume or build the collected PR snapshot with durable checkpoints."""
        stage = 'collect'
        store = StageStore(self.output_dir, stage)
        settings = effective_settings(self.config, stage)
        old, state = store.load(
            CollectDocument, settings=settings, producer=self.producer, input_receipt=None, overwrite=overwrite
        )
        if old is not None and state is None:
            if not retry_failed or not old.failed:
                self.stage_modes[stage] = 'reused'
                return old
            state = store.operation_state()
            runtime = self._record_runtime(stage, state, lambda: None)
            runtime.retry([item.identity.id for item in old.failed])
            state['phase'] = 'records'
            old = old.model_copy(update={'failed': []})
            old = self._update(
                old, counts=_counts(old.meta.counts.input, len(old.tasks), len(old.rejected)), status='running'
            )
            self._checkpoint(store, old, state)
        if old is None:
            self.stage_modes[stage] = 'new'
            doc, state, repository = self._initial_collect(store)
        else:
            self.stage_modes[stage] = 'resumed'
            doc = old
            assert state is not None
            repository = doc.meta.repository
            doc = self._update(doc, counts=doc.meta.counts, status='running')
        self._observe_document(doc, state)
        if state['phase'] == 'ready':
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            self._publish(store, doc, state)
            return doc
        try:
            if state['phase'] == 'repository':
                repository = self._repository()
                if repository.default_branch is None:
                    raise RemoteError('Source returned an unresolved default branch; check project metadata.')
                meta = doc.meta.model_copy(update={'repository': repository})
                doc = CollectDocument(meta=meta, tasks=doc.tasks)
                state = {**state, 'phase': 'discovery'}
                self._checkpoint(store, doc, state)
            if state['phase'] == 'discovery':
                discovery, numbers = self._discover_collect(doc, state, repository, store)
                doc = self._collected_with_discovery(doc, repository, discovery, numbers)
                state.update(
                    {'phase': 'records', 'numbers': numbers, 'matches': [list(item) for item in discovery.matches]}
                )
                self._checkpoint(store, doc, state)
            else:
                numbers = state['numbers']

            def keep_progress(document: CollectDocument) -> None:
                """Retain acknowledged outcomes if a later record fails fatally."""
                nonlocal doc
                doc = document

            doc = self._collect_records(doc, state, store, keep_progress)
            if self.shutdown.sync():
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted()
            if doc.meta.counts.pending:
                raise DecisionRequired('collect: interrupted records need a new attempt budget; use RETRY_FAILED=1')
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            state = {**state, 'phase': 'ready'}
            self._publish(store, doc, state)
            return doc
        except DecisionRequired:
            raise
        except (RemoteInterrupted, GenerationInterrupted, FilterInterrupted) as exc:
            if self.shutdown.requested:
                self.shutdown.sync()
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted() from exc
            raise
        except PipelineInterrupted:
            if self.shutdown.requested:
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
            raise
        except Exception as exc:
            if self.shutdown.requested:
                self.shutdown.sync()
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted() from exc
            self.shutdown.stop_event.set()
            error = _stage_error('collect', exc)
            self._checkpoint(store, self._update(doc, counts=doc.meta.counts, status='failed', error=error), state)
            raise

    def _filter_repository(
        self,
        source: SourceDescriptor,
        journal: AttemptJournal | None = None,
        save: Callable[[AttemptJournal], None] | None = None,
        context: GitDiagnosticContext | None = None,
    ) -> VCS:
        """Use the injected Git cache or create one tied to the stop event."""
        if self.git_repository is not None:
            return self.git_repository

        def account(values: dict[str, int]) -> None:
            """Persist cumulative warm and cold bytes across automatic attempts."""
            assert journal is not None and save is not None
            journal.current.warm_bytes = values['warm']
            journal.current.cold_bytes = values['cold']
            save(journal)

        return GitRepository(
            Path(self.config.cache_dir) / source_hash(source),
            source.fetch_url,
            self.config.filter.git.timeout_seconds,
            stop_event=self.shutdown.stop_event,
            initial_depth=self.config.filter.git.initial_depth,
            max_depth=self.config.filter.git.max_depth,
            max_transfer_bytes=self.config.filter.git.max_transfer_bytes,
            transfer_state=(
                {'warm': journal.current.warm_bytes, 'cold': journal.current.cold_bytes} if journal else None
            ),
            transfer_callback=account if journal is not None and save is not None else None,
            diagnostic_context=context,
            wait_callback=self.progress.wait,
            warning_callback=self._worker_messages.put,
            secret_names=(self.config.source.token_env,),
        )

    def _select_filter(self, document: FilterDocument, predecessor: CollectDocument) -> FilterDocument:
        """Apply population rules once while retaining all rejection reasons."""
        self.progress.update(phase='selection')
        snapshot = build_selection(
            document, predecessor, self.config, self.producer, load_template_snapshot(self.config.generation)
        )
        previous = StageStore(self.output_dir, 'filter').publication.selection()
        unchanged = previous is not None and previous.sha256 == snapshot.sha256
        if snapshot.omissions and not unchanged and not self.allow_partial:
            raise DecisionRequired(
                'filter: record pass completed with omissions; use RETRY_FAILED=1 or ALLOW_PARTIAL=1'
            )
        if previous is not None and not self.overwrite:
            if previous.generation_settings_sha256 != snapshot.generation_settings_sha256:
                raise StorageError('generation settings changed; use OVERWRITE=1 for the requested recalculation')
            if previous.fingerprint != snapshot.fingerprint and not self.reselect:
                added = set(snapshot.selected_ids) - set(previous.selected_ids)
                removed = set(previous.selected_ids) - set(snapshot.selected_ids)
                changed = [
                    identity
                    for identity in snapshot.selected_ids
                    if identity in previous.generation_keys
                    and previous.generation_keys[identity] != snapshot.generation_keys[identity]
                ]
                raise DecisionRequired(
                    f'selection changed: added={sorted(added)} removed={sorted(removed)} changed={changed}; '
                    'order may differ; use RESELECT=1'
                )
        if not snapshot.selected_ids:
            raise DecisionRequired('filter: selection is empty; inspect saved rejection and failure reasons')
        document = document.model_copy(update={'selection': snapshot})
        return self._update(
            document,
            counts=document.meta.counts,
            status='running',
        )

    def _filter_one(
        self,
        item: CollectRecord,
        remote: RemoteVCS,
        hosting_repository: Repository,
        journal: AttemptJournal | None = None,
        save: Callable[[AttemptJournal], None] | None = None,
    ) -> FilterRecord | RejectedRecord:
        """Prepare one descriptor-specific cache and run the hosting-independent filter."""
        request = item.change_request
        context = GitDiagnosticContext(
            item.id, f'{request.kind} {request.repository} #{request.number}', source_hash(item.source)
        )
        repository = self._filter_repository(item.source, journal, save, context)
        repository.prepare()
        return process_one(
            item,
            repository,
            remote,
            self.config.filter,
            self.shutdown.stop_event,
            hosting_repository=hosting_repository,
        )

    def _prior_stage(
        self,
        store: StageStore,
        model: type[DocumentT],
        settings: dict[str, Any],
        overwrite: bool,
    ) -> tuple[DocumentT | None, dict[str, Any] | None]:
        """Validate an older stage against its immutable predecessor before reconciling inputs."""

        def validate(document: DocumentT, state: dict[str, Any] | None) -> None:
            """Resolve the exact prior source rather than accepting a changed current head."""
            reference = document.meta.input
            if reference is None:
                raise StorageError('saved downstream stage has no predecessor')
            source = DOCUMENTS[reference.stage].model_validate(
                store.publication.lookup(reference.stage, reference.sha256)
            )
            if isinstance(source, BasketDocument):
                raise StorageError('a basket cannot be the input of a processing stage')
            validate_checkpoint_input(document, source, state)
            if isinstance(document, BasketDocument):
                assert isinstance(source, TasksDocument)
                collected_ref, filtered_ref = document.meta.lineage[:2]
                collected = CollectDocument.model_validate(store.publication.lookup('collect', collected_ref.sha256))
                filtered = FilterDocument.model_validate(store.publication.lookup('filter', filtered_ref.sha256))
                validate_export_accounting(document, collected, filtered, source)

        return store.prior(
            model, settings=settings, producer=self.producer, overwrite=overwrite, validate_loaded=validate
        )

    def _rebase_filter(
        self,
        document: FilterDocument,
        state: dict[str, Any],
        source: CollectDocument,
        store: StageStore,
    ) -> tuple[FilterDocument, dict[str, Any]]:
        """Keep only outcomes and attempt journals whose complete collected input is unchanged."""
        assert document.meta.input is not None
        previous = CollectDocument.model_validate(store.publication.lookup('collect', document.meta.input.sha256))
        old_inputs = {item.id: semantic_record(item) for item in previous.tasks}
        retained = {item.id for item in source.tasks if old_inputs.get(item.id) == semantic_record(item)}
        good = [item for item in document.tasks if item.id in retained]
        rejected = [item for item in document.rejected if item.id in retained]
        failed = [item for item in document.failed if item.record.id in retained]
        order = {item.id: index for index, item in enumerate(source.tasks)}
        fields = self._meta_fields(
            'filter',
            source.meta.repository,
            self._lineage([source]),
            _counts(len(source.tasks), len(good), len(rejected), len(failed)),
        )
        rebased = FilterDocument(
            meta=FilterMeta(**fields),
            tasks=sorted(good, key=lambda item: order[item.id]),
            rejected=sorted(rejected, key=lambda item: order[item.id]),
            failed=sorted(failed, key=lambda item: order[item.record.id]),
        )
        state = {
            **state,
            'phase': 'local',
            'attempts': {key: value for key, value in state.get('attempts', {}).items() if key in retained},
            'retry_ids': [key for key in state.get('retry_ids', []) if key in retained],
        }
        return rebased, state

    def _filter_records(
        self,
        doc: FilterDocument,
        state: dict[str, Any],
        store: StageStore,
        predecessor: CollectDocument,
        update: Callable[[FilterDocument], None],
    ) -> FilterDocument:
        """Run the complete candidate queue and preserve each durable outcome independently."""
        remote = self._remote()
        processed = (
            {item.id for item in doc.tasks}
            | {item.id for item in doc.rejected}
            | {item.record.id for item in doc.failed}
        )
        pending = [item for item in predecessor.tasks if item.id not in processed]
        accepted = {item.id: item for item in doc.tasks}
        rejected = {item.id: item for item in doc.rejected}
        failed = {item.record.id: item for item in doc.failed}
        active_state = state
        runtime = self._record_runtime('filter', state, lambda: self._checkpoint(store, doc, active_state))

        def filter_item(item: CollectRecord) -> RecordResult[FilterRecord | RejectedRecord]:
            """Bind this queue item to its hosting identity and source descriptor."""
            return runtime.run(
                item.id,
                sha256(semantic_record(item)),
                lambda journal: self._filter_one(
                    item,
                    remote,
                    predecessor.meta.repository,
                    journal,
                    lambda value: runtime.save(item.id, value),
                ),
            )

        def accept(item: CollectRecord, result: RecordResult[FilterRecord | RejectedRecord]) -> None:
            """Merge one filter result in input order and persist the checkpoint."""
            nonlocal doc
            runtime.accept(item.id, result)
            if result.needs_budget:
                self._checkpoint(store, doc, active_state)
                return
            if result.error is not None:
                assert result.journal is not None
                failed[item.id] = FilterFailure(
                    record=item, error=result.error, attempts=len(result.journal.current.attempts)
                )
            elif isinstance(result.value, FilterRecord):
                accepted[item.id] = result.value
            elif isinstance(result.value, RejectedRecord):
                rejected[item.id] = result.value
            else:
                raise ValueError('invalid filter operation result')
            ordered_ok = [accepted[pr.id] for pr in predecessor.tasks if pr.id in accepted]
            ordered_no = [rejected[pr.id] for pr in predecessor.tasks if pr.id in rejected]
            ordered_failed = [failed[pr.id] for pr in predecessor.tasks if pr.id in failed]
            doc = doc.model_copy(update={'tasks': ordered_ok, 'rejected': ordered_no, 'failed': ordered_failed})
            doc = self._update(
                doc,
                counts=_counts(len(predecessor.tasks), len(ordered_ok), len(ordered_no), len(ordered_failed)),
                status='running',
            )
            update(doc)
            self._checkpoint(store, doc, active_state)
            if isinstance(result.value, RejectedRecord):
                self._print_history_rejection(result.value, store.work_path)

        self._run_items(pending, self.config.filter.workers, filter_item, accept, runtime.poll)
        return doc

    def _filter(self, overwrite: bool, retry_failed: bool = False) -> FilterDocument:
        """Resolve PR changes, select a stable population, and publish the result."""
        predecessor = self._verified_predecessors('filter')[0]
        assert isinstance(predecessor, CollectDocument)
        store = StageStore(self.output_dir, 'filter')
        old, state = self._prior_stage(
            store,
            FilterDocument,
            effective_settings(self.config, 'filter'),
            overwrite,
        )
        completed_pass = old is not None and (state is None or old.meta.counts.pending == 0)
        if old is not None and old.meta.input != expected_input(predecessor):
            old, state = self._rebase_filter(old, state or store.operation_state(), predecessor, store)
        if old is not None and completed_pass and retry_failed and old.failed:
            state = state or store.operation_state()
            runtime = self._record_runtime('filter', state, lambda: None)
            runtime.retry([item.record.id for item in old.failed])
            old = old.model_copy(update={'failed': [], 'selection': None})
            old = self._update(
                old, counts=_counts(len(predecessor.tasks), len(old.tasks), len(old.rejected)), status='running'
            )
            state['phase'] = 'local'
        if old is not None and state is None:
            proposal = self._select_filter(old, predecessor)
            if proposal.selection == old.selection:
                self.stage_modes['filter'] = 'reused'
                return old
            old = proposal
            state = {**store.operation_state(), 'phase': 'selection'}
        if old is None:
            self.stage_modes['filter'] = 'new'
            counts = _counts(len(predecessor.tasks), 0)
            fields = self._meta_fields('filter', predecessor.meta.repository, self._lineage([predecessor]), counts)
            doc = FilterDocument(meta=FilterMeta(**fields), tasks=[], rejected=[])
            state = {'phase': 'local'}
            self._checkpoint(store, doc, state)
        else:
            self.stage_modes['filter'] = 'resumed'
            doc = self._update(old, counts=old.meta.counts, status='running')
            assert state is not None
        self._observe_document(doc, state)
        if state['phase'] == 'ready':
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            self._verified_predecessors('filter')
            self._publish(store, doc, state, 'collect')
            return doc
        try:
            if state['phase'] == 'local':

                def keep_progress(document: FilterDocument) -> None:
                    """Keep acknowledged results if a later candidate stops the stage."""
                    nonlocal doc
                    doc = document

                doc = self._filter_records(doc, state, store, predecessor, keep_progress)
                if self.shutdown.sync():
                    doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                    self._checkpoint(store, doc, state)
                    raise PipelineInterrupted()
                if doc.meta.counts.pending:
                    raise DecisionRequired('filter: interrupted records need a new attempt budget; use RETRY_FAILED=1')
                state = {**state, 'phase': 'selection'}
                self._checkpoint(store, doc, state)
            if doc.selection is None:
                completed_candidates = self._update(doc, counts=doc.meta.counts, status='completed')
                self._publish(store, completed_candidates, state, 'collect')
            doc = self._select_filter(doc, predecessor)
            state = {**state, 'phase': 'ready'}
            self._checkpoint(store, doc, state)
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            self._verified_predecessors('filter')
            self._publish(store, doc, state, 'collect')
            return doc
        except DecisionRequired:
            raise
        except PipelineInterrupted:
            if self.shutdown.requested:
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
            raise
        except Exception as exc:
            if self.shutdown.requested:
                self.shutdown.sync()
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted() from exc
            self.shutdown.stop_event.set()
            self._checkpoint(
                store,
                self._update(doc, counts=doc.meta.counts, status='failed', error=_stage_error('filter', exc)),
                state,
            )
            raise

    def _prepare_generate(
        self,
        old: TasksDocument | None,
        state: dict[str, Any] | None,
        source: FilterDocument,
        predecessors: list[CollectDocument | FilterDocument | TasksDocument],
        store: StageStore,
        template_snapshot: TemplateSnapshot | None,
        retry_failed: bool,
    ) -> tuple[TasksDocument, dict[str, Any] | None]:
        """Initialize or resume task progress without repeating completed work."""
        assert source.selection is not None
        total = len(source.selection.selected_ids)
        if old is None:
            self.stage_modes['generate'] = 'new'
            fields = self._meta_fields(
                'generate',
                source.meta.repository,
                self._lineage(predecessors),
                _counts(total, 0),
                template_snapshot=template_snapshot,
            )
            document = TasksDocument(
                meta=GenerateMeta(**fields), tasks=[], failed=[], selection=selection_receipt(source.selection)
            )
            state = {'phase': 'records', 'attempts': {}, 'retry_ids': []}
            self._checkpoint(store, document, state)
        elif state is None:
            if not retry_failed or not old.failed:
                self.stage_modes['generate'] = 'reused'
                return old, None
            self.stage_modes['generate'] = 'resumed'
            state = store.operation_state()
            runtime = self._record_runtime('generate', state, lambda: None)
            runtime.retry([entry.record.id for entry in old.failed])
            document = old.model_copy(update={'failed': []})
            document = self._update(
                document, counts=_counts(total, len(document.tasks), len(document.rejected)), status='running'
            )
            state['phase'] = 'records'
            self._checkpoint(store, document, state)
        else:
            self.stage_modes['generate'] = 'resumed'
            document = self._update(old, counts=old.meta.counts, status='running')
        return document, state

    def _generation_history(
        self,
        previous: TasksDocument,
        keys: dict[str, str],
        retained: set[str],
    ) -> tuple[dict[str, TaskRecord], dict[str, RejectedRecord], dict[str, dict[str, Any]]]:
        """Restore completed historical outcomes and rejection journals with exact input keys."""
        store = StageStore(self.output_dir, 'generate')
        successes: dict[str, TaskRecord] = {}
        rejections: dict[str, RejectedRecord] = {}
        journals: dict[str, dict[str, Any]] = {}
        seen = set(retained)
        for payload in store.publication.history('generate'):
            historical = TasksDocument.model_validate(payload)
            if historical.meta.producer != self.producer or historical.meta.settings != previous.meta.settings:
                continue
            assert historical.meta.input is not None
            source = FilterDocument.model_validate(store.publication.lookup('filter', historical.meta.input.sha256))
            validate_checkpoint_input(historical, source, None)
            origin = stage_sha256(historical)
            for item in historical.tasks:
                if item.id not in seen and keys.get(item.id) == item.generation.input_sha256:
                    generation = item.generation.model_copy(update={'reused_from': origin})
                    successes[item.id] = item.model_copy(update={'generation': generation})
                    seen.add(item.id)
            for rejected in historical.rejected:
                if rejected.id in seen or keys.get(rejected.id) != historical.selection.generation_keys[rejected.id]:
                    continue
                state = store.publication.historical_state('generate', origin)
                journal = AttemptJournal.model_validate(state.get('attempts', {}).get(rejected.id))
                if (
                    journal.input_sha256 != keys[rejected.id]
                    or not journal.current.attempts
                    or journal.current.attempts[-1].outcome != 'rejected'
                ):
                    raise StorageError('historical rejection journal disagrees with generation input')
                journal.reused_from = origin
                journals[rejected.id] = journal.model_dump(mode='json')
                rejections[rejected.id] = rejected
                seen.add(rejected.id)
        return successes, rejections, journals

    def _rebase_generation(
        self,
        previous: TasksDocument,
        state: dict[str, Any],
        source: FilterDocument,
        predecessors: list[CollectDocument | FilterDocument | TasksDocument],
    ) -> tuple[TasksDocument, dict[str, Any]]:
        """Reuse outcomes only when full generation keys match the newly accepted selection."""
        assert source.selection is not None
        inputs = selected_records(source)
        keys = source.selection.generation_keys
        retained = {
            identity for identity, key in keys.items() if previous.selection.generation_keys.get(identity) == key
        }
        successes = {item.id: item for item in previous.tasks if item.id in retained}
        origins = {item.id: stage_sha256(previous) for item in successes.values()}
        cached, excluded, journals = self._generation_history(previous, keys, retained)
        successes.update(cached)
        for identity, item in cached.items():
            assert item.generation.reused_from is not None
            origins[identity] = item.generation.reused_from
        failures = {item.record.id: item for item in previous.failed if item.record.id in retained}
        rejections = {item.id: item for item in previous.rejected if item.id in retained}
        rejections.update(excluded)
        good, bad, rejected = [], [], []
        for item in inputs:
            if item.id in successes:
                prior = successes[item.id]
                generation = prior.generation.model_copy(update={'reused_from': origins[item.id]})
                good.append(
                    TaskRecord.model_validate(
                        {
                            **item.model_dump(mode='json'),
                            'task': prior.task.model_dump(mode='json'),
                            'generation': generation.model_dump(mode='json'),
                        }
                    )
                )
            elif item.id in failures:
                bad.append(failures[item.id].model_copy(update={'record': item}))
            elif item.id in rejections:
                rejected.append(rejections[item.id])
        fields = self._meta_fields(
            'generate',
            source.meta.repository,
            self._lineage(predecessors),
            _counts(len(inputs), len(good), len(rejected), len(bad)),
            template_snapshot=self._generation_snapshot,
        )
        document = TasksDocument(
            meta=GenerateMeta(**fields),
            selection=selection_receipt(source.selection),
            tasks=good,
            failed=bad,
            rejected=rejected,
        )
        state = {
            **state,
            'phase': 'records',
            'attempts': {
                **{key: value for key, value in state.get('attempts', {}).items() if key in retained},
                **journals,
            },
            'retry_ids': [key for key in state.get('retry_ids', []) if key in retained],
        }
        return document, state

    def _generate_one(
        self,
        item: FilterRecord,
        journal: AttemptJournal,
        client: LLMClient | None,
        template_snapshot: TemplateSnapshot | None,
        key: str,
        save: Callable[[AttemptJournal], None],
    ) -> TaskRecord | RejectedRecord:
        """Bind a generated task to its complete input and cumulative actual request count."""

        def reserve() -> None:
            """Persist actual request launches independently from local record validation attempts."""
            journal.current.request_count += 1
            save(journal)

        with generation_requests(reserve):
            result = generate_record(
                item,
                self.config.generation,
                llm_client=client,
                stop_event=self.shutdown.stop_event,
                template_snapshot=template_snapshot,
            )
        if isinstance(result, TaskRecord):
            generation = result.generation.model_copy(
                update={
                    'input_sha256': key,
                    'attempts': (
                        sum(round.request_count for round in journal.rounds)
                        if self.config.generation.source == 'llm'
                        else 0
                    ),
                }
            )
            result = result.model_copy(update={'generation': generation})
        return result

    def _generate_records(
        self,
        doc: TasksDocument,
        active_state: dict[str, Any],
        store: StageStore,
        inputs: list[FilterRecord],
        template_snapshot: TemplateSnapshot | None,
        retry_failed: bool,
        update: Callable[[TasksDocument], None],
    ) -> TasksDocument:
        """Execute selected records while committing each task and its attempt journal together."""
        state = active_state
        completed = (
            {item.id for item in doc.tasks}
            | {item.record.id for item in doc.failed}
            | {item.id for item in doc.rejected}
        )
        pending = [item for item in inputs if item.id not in completed]
        self.progress.update(
            retry_batch=len(pending) if retry_failed else None,
            kept_successful=len(doc.tasks) if retry_failed else None,
        )
        succeeded = {item.id: item for item in doc.tasks}
        failed = {item.record.id: item for item in doc.failed}
        rejected = {item.id: item for item in doc.rejected}
        runtime = self._record_runtime('generate', state, lambda: self._checkpoint(store, doc, active_state))

        def accept(item: FilterRecord, result: RecordResult[TaskRecord | RejectedRecord]) -> None:
            """Merge one task result in source order and persist the checkpoint."""
            nonlocal doc
            runtime.accept(item.id, result)
            if result.needs_budget:
                self._checkpoint(store, doc, active_state)
                return
            if result.error is not None:
                assert result.journal is not None
                failed[item.id] = FailedRecord(
                    record=item, error=result.error, attempts=len(result.journal.current.attempts)
                )
            elif isinstance(result.value, TaskRecord):
                succeeded[item.id] = result.value
            elif isinstance(result.value, RejectedRecord):
                rejected[item.id] = result.value
            else:
                raise ValueError('invalid generation operation result')
            good = [succeeded[pr.id] for pr in inputs if pr.id in succeeded]
            bad = [failed[pr.id] for pr in inputs if pr.id in failed]
            excluded = [rejected[pr.id] for pr in inputs if pr.id in rejected]
            doc = doc.model_copy(update={'tasks': good, 'failed': bad, 'rejected': excluded})
            doc = self._update(doc, counts=_counts(len(inputs), len(good), len(excluded), len(bad)), status='running')
            update(doc)
            self._checkpoint(store, doc, active_state)

        if pending:
            client = self._llm() if self.config.generation.source == 'llm' else None

            def generate(item: FilterRecord) -> RecordResult[TaskRecord | RejectedRecord]:
                """Bind one complete generation operation to its full frozen input key."""
                key = doc.selection.generation_keys[item.id]

                return runtime.run(
                    item.id,
                    key,
                    lambda journal: self._generate_one(
                        item, journal, client, template_snapshot, key, lambda value: runtime.save(item.id, value)
                    ),
                )

            self._run_items(
                pending,
                self.config.generation.workers,
                generate,
                accept,
                runtime.poll,
            )
        return doc

    def _rebind_generation(self) -> None:
        """Rebind only generation inputs for explicit recalculation, preserving candidate checks and selected IDs."""
        collected = self._verified_predecessors('filter')[0]
        assert isinstance(collected, CollectDocument)
        store = StageStore(self.output_dir, 'filter')
        filtered, state = store.load(
            FilterDocument,
            settings=effective_settings(self.config, 'filter'),
            producer=self.producer,
            input_receipt=expected_input(collected),
            validate_loaded=lambda document, checkpoint: validate_checkpoint_input(document, collected, checkpoint),
        )
        if filtered is None or state is not None or filtered.selection is None:
            raise DecisionRequired('generate: complete filter and accept its selection before recalculating generation')
        self._generation_snapshot = load_template_snapshot(self.config.generation)
        proposal = build_selection(filtered, collected, self.config, self.producer, self._generation_snapshot)
        previous = filtered.selection
        if proposal == previous:
            return
        if (
            proposal.input_sha256 != previous.input_sha256
            or proposal.selected_ids != previous.selected_ids
            or proposal.settings != previous.settings
            or proposal.omissions != previous.omissions
        ):
            raise DecisionRequired(
                'generate: population or selection changed; accept it through filter or basket first'
            )
        rebound = filtered.model_copy(update={'selection': proposal})
        rebound = self._update(rebound, counts=rebound.meta.counts, status='completed')
        if self.shutdown.sync():
            raise PipelineInterrupted()
        store.publish(rebound, current_input=expected_input(collected), operation_state=store.operation_state())

    def _generate(self, overwrite: bool, retry_failed: bool) -> TasksDocument:
        """Generate tasks from one template snapshot while preserving retry progress."""
        if overwrite:
            self._rebind_generation()
        predecessors = self._verified_predecessors('generate')
        source = predecessors[-1]
        assert isinstance(source, FilterDocument)
        inputs = selected_records(source)
        self.progress.basket_update(available=len(inputs))
        store = StageStore(self.output_dir, 'generate')
        template_snapshot = self._generation_snapshot or load_template_snapshot(self.config.generation)
        self._generation_snapshot = template_snapshot
        old, state = self._prior_stage(
            store,
            TasksDocument,
            effective_settings(self.config, 'generate', template_snapshot=template_snapshot),
            overwrite,
        )
        completed_pass = old is not None and state is None
        if old is not None and old.meta.input != expected_input(source):
            old, state = self._rebase_generation(old, state or store.operation_state(), source, predecessors)
            if completed_pass and retry_failed and old.failed:
                runtime = self._record_runtime('generate', state, lambda: None)
                runtime.retry([item.record.id for item in old.failed])
                old = old.model_copy(update={'failed': []})
                old = self._update(
                    old, counts=_counts(len(inputs), len(old.tasks), len(old.rejected)), status='running'
                )
            self._checkpoint(store, old, state)
        if old is not None and state is None and not retry_failed:
            self.stage_modes['generate'] = 'reused'
            return old
        doc, state = self._prepare_generate(old, state, source, predecessors, store, template_snapshot, retry_failed)
        if state is None:
            return doc
        if state.get('phase') == 'ready':
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            self._verified_predecessors('generate')
            self._publish(store, doc, state, 'filter')
            return doc
        try:

            def keep_progress(document: TasksDocument) -> None:
                """Keep acknowledged outcomes when another selected record stops the stage."""
                nonlocal doc
                doc = document

            doc = self._generate_records(doc, state, store, inputs, template_snapshot, retry_failed, keep_progress)
            if self.shutdown.sync():
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted()
            counts = _counts(len(inputs), len(doc.tasks), len(doc.rejected), len(doc.failed))
            if counts.pending:
                raise DecisionRequired('generate: interrupted records need a new attempt budget; use RETRY_FAILED=1')
            doc = self._update(doc, counts=counts, status='completed')
            self._verified_predecessors('generate')
            state = {**state, 'phase': 'ready'}
            self._publish(store, doc, state, 'filter')
            return doc
        except DecisionRequired:
            raise
        except PipelineInterrupted:
            raise
        except Exception as exc:
            if self.shutdown.requested:
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted() from exc
            self.shutdown.stop_event.set()
            self._checkpoint(
                store,
                self._update(doc, counts=doc.meta.counts, status='failed', error=_stage_error('generate', exc)),
                state,
            )
            raise

    def _export(self, overwrite: bool) -> BasketDocument:
        """Project completed tasks into the final self-contained basket."""
        predecessors = self._verified_predecessors('export')
        tasks = predecessors[-1]
        assert isinstance(tasks, TasksDocument)
        self.progress.basket_update(available=tasks.meta.counts.input)
        self.progress.update(phase='preparing', total=len(tasks.tasks))
        store = StageStore(self.output_dir, 'export')
        old, state = self._prior_stage(
            store,
            BasketDocument,
            effective_settings(self.config, 'export'),
            overwrite,
        )
        if old is not None and old.meta.input != expected_input(tasks):
            old, state = None, None
        if old is not None and state is None:
            self.stage_modes['export'] = 'reused'
            return old
        if not tasks.tasks:
            raise DecisionRequired('export: no successful tasks; an empty basket cannot be published')
        if (tasks.failed or tasks.rejected) and not self.allow_partial:
            raise DecisionRequired('export: generation omissions require ALLOW_PARTIAL=1 or retry failed tasks')
        self.stage_modes['export'] = 'resumed' if state is not None else 'new'
        if old is not None and state is not None:
            if state.get('phase') != 'ready':
                raise StorageError('invalid export checkpoint phase')
            document = self._update(old, counts=old.meta.counts, status='completed')
            self._verified_predecessors('export')
            self._publish(store, document, state, 'generate')
            return document
        if self.shutdown.sync():
            raise PipelineInterrupted()
        collected, filtered = predecessors[:2]
        assert isinstance(collected, CollectDocument) and isinstance(filtered, FilterDocument)
        omitted = [*omissions(collected), *omissions(filtered), *omissions(tasks)]
        fields = self._meta_fields(
            'export',
            tasks.meta.repository,
            self._lineage(predecessors),
            _counts(len(tasks.tasks), len(tasks.tasks)),
            status='completed',
        )
        meta = BasketMeta(
            **fields,
            name=self.config.dataset.name,
            description=self.config.dataset.description,
            maintainers=self.config.dataset.maintainers,
            partial=bool(omitted),
            omissions=omitted,
            selection=tasks.selection,
            coverage=Coverage(
                discovery=collected.meta.discovery,
                collect=collected.meta.counts,
                filter=filtered.meta.counts,
                selected=len(tasks.selection.selected_ids),
                generate=tasks.meta.counts,
            ),
        )
        self.progress.update(phase='validating')
        document = export_tasks(tasks, meta)
        self._verified_predecessors('export')
        self._publish(store, document, {'phase': 'ready'}, 'generate')
        return document

    def _print_partial_export(self, document: BasketDocument) -> None:
        """Explain omitted generation failures and how to retry a partial basket."""
        if document.meta.partial:
            tasks_path = StageStore(self.output_dir, 'generate').path
            if all(item.kind == 'rejected' for item in document.meta.omissions):
                self._message(
                    f'export: partial=true omissions={len(document.meta.omissions)}; '
                    f'generation rejection reasons: {tasks_path}; RETRY_FAILED does not repeat rejected records.',
                    level='WARNING',
                )
                return
            self._message(
                f'export: partial=true omissions={len(document.meta.omissions)}; '
                f'failure reasons: {tasks_path}; '
                'retry: make basket RETRY_FAILED=1 with the same CONFIG; '
                'Python CLI: use basket --retry-failed 1 with the same --config',
                level='WARNING',
            )

    def _message(self, text: str, *, level: str = 'INFO') -> None:
        """Route stage summaries through the invocation's console/file policy."""
        if self.logs is None:
            print(text, flush=True)
        else:
            self.logs.message(text, level=level)

    def _print_history_rejection(self, record: RejectedRecord, path: Path) -> None:
        """Report one saved history rejection using only safe structured evidence."""
        for reason in record.reasons:
            if reason.code != 'source_history_unavailable':
                continue
            details = ''
            if isinstance(reason.observed, dict):
                missing = reason.observed.get('missing_commit')
                depth = reason.observed.get('depth')
                if (
                    isinstance(missing, str)
                    and re.fullmatch(r'[0-9a-f]{40}', missing)
                    and type(depth) is int
                    and depth > 0
                ):
                    details = f' missing_commit={missing} depth={depth};'
            change = record.change_request
            prefix = '!' if change.kind == 'merge_request' else '#'
            self._message(
                f'filter: {change.kind} {change.repository} {prefix}{change.number} '
                f'rejected: source_history_unavailable;{details} path={path}',
                level='WARNING',
            )
            return

    def _print_history_rejection_summary(self, document: FilterDocument) -> None:
        """Count persisted history rejections on both completed and reused stages."""
        count = sum(
            any(reason.code == 'source_history_unavailable' for reason in record.reasons)
            for record in document.rejected
        )
        if count:
            path = StageStore(self.output_dir, 'filter').path
            self._message(
                f'filter: source_history_unavailable={count}; rejection reasons: {path}',
                level='WARNING',
            )

    @staticmethod
    def _validate_command(command: str, overwrite: bool, retry_failed: bool, reselect: bool) -> None:
        """Reject conflicting intents and flags outside a command's scope before work begins."""
        if command not in (*STAGES, 'basket'):
            raise ValueError(f'unknown command: {command}')
        if overwrite and retry_failed:
            raise ValueError('OVERWRITE=1 and RETRY_FAILED=1 cannot be combined')
        if overwrite and reselect:
            raise ValueError('OVERWRITE=1 and RESELECT=1 cannot be combined')
        if reselect and command not in ('filter', 'basket'):
            raise ValueError('RESELECT=1 applies only to filter and basket')
        if retry_failed and command == 'export':
            raise ValueError('RETRY_FAILED=1 does not apply to export')

    def run(
        self,
        command: str,
        *,
        overwrite: bool = False,
        retry_failed: bool = False,
        allow_partial: bool = False,
        reselect: bool = False,
    ) -> int:
        """Execute one stage or the ordered basket under an exclusive output lock.

        Args:
                command: A stage name or ``basket``.
                overwrite: Rebuild selected stages from their inputs.
                retry_failed: Retry only completed per-PR generation failures.
                allow_partial: Accept the exact known omissions at selection or export.
                reselect: Accept a changed ordered selection after processing candidates.

        Returns:
                Zero on success or three when a direct generate run has failed PRs.

        Raises:
                PipelineInterrupted: If a signal stops the run after checkpointing.
        StorageError: If an input, checkpoint, or stage identity is inconsistent.

        """
        self._validate_command(command, overwrite, retry_failed, reselect)
        self._generation_snapshot = None
        self.allow_partial, self.reselect, self.overwrite = allow_partial, reselect, overwrite
        self.retry_failed = retry_failed
        selected = STAGES if command == 'basket' else (command,)
        if command == 'basket':
            self.progress.basket(self.config.filter.selection.limit)
        with self.shutdown.installed(), single_writer(self.output_dir):
            try:
                final: CollectDocument | FilterDocument | TasksDocument | BasketDocument | None = None
                for stage in selected:
                    if self.shutdown.sync():
                        raise PipelineInterrupted()
                    self.progress.start(stage, 'repository' if stage == 'collect' else 'predecessors')
                    try:
                        if stage == 'collect':
                            final = self._collect(overwrite, retry_failed)
                        elif stage == 'filter':
                            final = self._filter(overwrite, retry_failed)
                        elif stage == 'generate':
                            final = self._generate(overwrite, retry_failed)
                        else:
                            final = self._export(overwrite)
                    except DecisionRequired:
                        self.progress.finish('blocked')
                        raise
                    except PipelineInterrupted:
                        self.progress.finish('interrupted')
                        raise
                    except Exception:
                        self.progress.finish('failed')
                        raise
                    self._observe_document(final)
                    self.progress.finish(final.meta.status, mode=self.stage_modes[stage])
                    if self.shutdown.sync():
                        raise PipelineInterrupted()
                    self._message(
                        f'{stage}: {final.meta.status}; input={final.meta.counts.input} '
                        f'succeeded={final.meta.counts.succeeded} rejected={final.meta.counts.rejected} '
                        f'failed={final.meta.counts.failed} path={StageStore(self.output_dir, stage).path} '
                        f'mode={self.stage_modes[stage]}',
                    )
                    if isinstance(final, FilterDocument):
                        self._print_history_rejection_summary(final)
                    if isinstance(final, BasketDocument):
                        self._print_partial_export(final)
                if command == 'basket':
                    self.progress.finish_basket('completed')
                if isinstance(final, (CollectDocument, FilterDocument, TasksDocument)):
                    if not final.tasks:
                        self._message(f'{final.meta.stage}: no successful records remain', level='WARNING')
                        return 3
                    if not allow_partial and (final.failed or isinstance(final, TasksDocument) and final.rejected):
                        return 3
                return 0
            except DecisionRequired as error:
                self._message(str(error), level='WARNING')
                self.progress.finish_basket('blocked')
                return 3
            except PipelineInterrupted:
                self.progress.finish_basket('interrupted')
                raise
            except Exception:
                self.progress.finish_basket('failed')
                raise
            finally:
                self.close()


class _SafeParser(argparse.ArgumentParser):
    """Reject arguments without echoing possible credential values."""

    def error(self, message: str) -> None:
        """Keep rejected argument payloads out of the terminal."""
        self.exit(2, 'Invalid arguments; use --help for accepted options. Keep credentials in the environment.\n')


def _cli_problem(error: BaseException, prefix: str, directory: Path, level: str, logs: InvocationLogs | None) -> None:
    """Persist diagnostic detail while always showing a safe console remedy."""
    owns = logs is None
    if logs is None:
        try:
            logs = InvocationLogs(directory, level)
        except OSError:
            print('Cannot create the DEBUG file log; check output permissions and disk space.', file=sys.stderr)
    message = prefix + ': ' + friendly_error(error)
    if logs is None:
        print(message, file=sys.stderr)
        return
    try:
        logs.diagnostic(error)
        logs.message(message + '; DEBUG log: ' + str(logs.path), level='ERROR', stream=sys.stderr)
    finally:
        if owns:
            logs.close()


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with DEBUG files and safe, independently filtered user messages."""
    parser = _SafeParser(prog='basket_collector')
    parser.add_argument('command', choices=(*STAGES, 'basket', 'smoke', 'smoke-live'))
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--overwrite', choices=('0', '1'), default='0')
    parser.add_argument('--retry-failed', choices=('0', '1'), default='0')
    parser.add_argument('--allow-partial', choices=('0', '1'), default='0')
    parser.add_argument('--reselect', choices=('0', '1'), default='0')
    parser.add_argument(
        '--log-level', choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'), default=os.environ.get('LOG_LEVEL', 'INFO')
    )
    parser.add_argument('--progress', choices=MODES, default=os.environ.get('BASKET_PROGRESS_MODE', 'auto'))
    args = parser.parse_args(argv)
    if args.command == 'smoke':
        pytest = importlib.import_module('pytest')
        tests = Path(__file__).resolve().parents[1] / 'tests'
        return int(
            pytest.main(
                [
                    str(tests / name)
                    for name in ('test_end_to_end.py', 'test_make_smoke.py', 'test_pipeline.py', 'test_shutdown.py')
                ]
            )
        )
    logs = None
    setup = True
    # Invalid/missing configs still get an invocation log next to their default output.
    directory = Path(args.config).expanduser().absolute().parent / 'output'
    try:
        config = load_config(args.config)
        load_environment(config)
        directory = Path(config.output_dir)
        secret_names = [config.source.token_env]
        if config.generation.llm is not None:
            secret_names.append(config.generation.llm.api_key_env)
        logs = InvocationLogs(directory, args.log_level, secret_names=secret_names)
        if args.command == 'smoke-live':
            ids = config.collect.change_ids
            if config.collect.selection != 'change_ids' or ids is None or not 1 <= len(ids) <= 3:
                raise ValueError('smoke-live requires collect.selection=change_ids with 1-3 changes')
        command = 'basket' if args.command == 'smoke-live' else args.command
        pipeline = Pipeline(config, logs=logs, progress_mode=args.progress)
        setup = False
        return pipeline.run(
            command,
            overwrite=args.overwrite == '1',
            retry_failed=args.retry_failed == '1',
            allow_partial=args.allow_partial == '1',
            reselect=args.reselect == '1',
        )
    except PipelineInterrupted:
        assert logs is not None
        logs.message('interrupted; checkpoint saved; resume with the same CONFIG.', level='WARNING', stream=sys.stderr)
        return 130
    except EmptyStageError as error:
        _cli_problem(error, error.code, directory, args.log_level, logs)
        return 3 if error.stage == 'generate' else 2
    except (ValueError, StorageError, LLMConfigurationError) as error:
        _cli_problem(error, 'configuration or artifact error', directory, args.log_level, logs)
        return 2
    except OSError as error:
        _cli_problem(
            error, 'configuration or artifact error' if setup else 'pipeline failed', directory, args.log_level, logs
        )
        return 2 if setup else 4
    except Exception as error:
        _cli_problem(error, 'pipeline failed', directory, args.log_level, logs)
        return 4
    finally:
        if logs is not None:
            logs.close()
