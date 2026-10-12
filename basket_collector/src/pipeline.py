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
    CollectMeta,
    CollectRecord,
    Counts,
    Discovery,
    FailedRecord,
    FilterDocument,
    FilterMeta,
    FilterRecord,
    GenerateMeta,
    InputReceipt,
    LineageReceipt,
    Producer,
    RejectedRecord,
    Repository,
    StageError,
    TaskRecord,
    TasksDocument,
)
from basket_collector.filters import apply_population
from basket_collector.git import GitInfrastructureError, GitRepository
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.gitlab_source import GitLabRemoteVCS
from basket_collector.llm import (
    GenerationInterrupted,
    LLMAuthenticationError,
    LLMClient,
    LLMConfigurationError,
)
from basket_collector.logs import InvocationLogs, friendly_error
from basket_collector.progress import MODES, Progress
from basket_collector.remote import ChangeRejected, RemoteError, RemoteInterrupted, RemoteVCS
from basket_collector.sources import SourceDescriptor, source_hash
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
    utc_now,
)
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
            reason = 'Git operation' if self.progress.current == 'filter' else 'LLM request'
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
            'schema_version': 1,
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
            if not loaded.tasks:
                raise EmptyStageError(stage, predecessor=name)
            if loaded.meta.lineage != self._lineage(documents):
                raise StorageError(f'{name} lineage differs from current predecessors; use OVERWRITE=1')
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
            self.progress.basket_update(successful=counts.succeeded, failed=counts.failed)
        else:
            assert isinstance(document, BasketDocument)
            phase = 'writing'
            self.progress.basket_update(successful=len(document.tasks), failed=len(document.meta.omitted_failed_ids))
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
        store.publish(document, current_input=current_input)
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

    def _collect(self, overwrite: bool) -> CollectDocument:
        """Resume or build the collected PR snapshot with durable checkpoints."""
        stage = 'collect'
        store = StageStore(self.output_dir, stage)
        settings = effective_settings(self.config, stage)
        old, state = store.load(
            CollectDocument, settings=settings, producer=self.producer, input_receipt=None, overwrite=overwrite
        )
        if old is not None and state is None:
            self.stage_modes[stage] = 'reused'
            return old
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
            done = {item.change_request.number for item in doc.tasks}
            done.update(item.change_request.number for item in doc.rejected)
            for number in numbers:
                if self.shutdown.sync():
                    break
                if number in done:
                    continue
                try:
                    record = self._run_one(
                        lambda n=number: collect_change(self._remote(), repository, n, self.config.collect)
                    )
                except RemoteInterrupted:
                    if self.shutdown.sync():
                        break
                    raise
                except ChangeRejected as exc:
                    if exc.record.change_request.number != number:
                        raise RemoteError('Source rejected another change.', code='invalid_source_metadata') from exc
                    doc = doc.model_copy(update={'rejected': [*doc.rejected, exc.record]})
                else:
                    doc = doc.model_copy(update={'tasks': [*doc.tasks, record]})
                doc = self._update(
                    doc, counts=_counts(len(numbers), len(doc.tasks), len(doc.rejected)), status='running'
                )
                self._checkpoint(store, doc, state)
            if self.shutdown.sync():
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted()
            if not doc.tasks:
                raise EmptyStageError('collect', details=_empty_collect_details(doc))
            doc = self._update(doc, counts=_counts(len(numbers), len(doc.tasks), len(doc.rejected)), status='completed')
            state = {**state, 'phase': 'ready'}
            self._publish(store, doc, state)
            return doc
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

    def _filter_repository(self, source: SourceDescriptor) -> VCS:
        """Use the injected Git cache or create one tied to the stop event."""
        if self.git_repository is not None:
            return self.git_repository
        return GitRepository(
            Path(self.config.cache_dir) / source_hash(source),
            source.fetch_url,
            self.config.git.timeout_seconds,
            stop_event=self.shutdown.stop_event,
            initial_depth=self.config.git.initial_depth,
            max_depth=self.config.git.max_depth,
            max_transfer_bytes=self.config.git.max_transfer_bytes,
        )

    def _select_filter(self, document: FilterDocument, predecessor: CollectDocument) -> FilterDocument:
        """Apply population rules once while retaining all rejection reasons."""
        self.progress.update(phase='selection')
        selected, population_rejected = apply_population(document.tasks, self.config.filter)
        rejected_by_id = {item.id: item for item in [*document.rejected, *population_rejected]}
        ordered_rejected = [rejected_by_id[item.id] for item in predecessor.tasks if item.id in rejected_by_id]
        document = document.model_copy(update={'tasks': selected, 'rejected': ordered_rejected})
        return self._update(
            document,
            counts=_counts(len(predecessor.tasks), len(selected), len(ordered_rejected)),
            status='running',
        )

    def _filter_one(
        self, item: CollectRecord, remote: RemoteVCS, hosting_repository: Repository
    ) -> FilterRecord | RejectedRecord:
        """Prepare one descriptor-specific cache and run the hosting-independent filter."""
        repository = self._filter_repository(item.source)
        repository.prepare()
        return process_one(
            item,
            repository,
            remote,
            self.config.filter,
            self.shutdown.stop_event,
            hosting_repository=hosting_repository,
        )

    def _filter(self, overwrite: bool) -> FilterDocument:
        """Resolve PR changes, select a stable population, and publish the result."""
        predecessor = self._verified_predecessors('filter')[0]
        assert isinstance(predecessor, CollectDocument)
        store = StageStore(self.output_dir, 'filter')
        old, state = store.load(
            FilterDocument,
            settings=effective_settings(self.config, 'filter'),
            producer=self.producer,
            input_receipt=expected_input(predecessor),
            overwrite=overwrite,
            validate_loaded=lambda saved, checkpoint: validate_checkpoint_input(saved, predecessor, checkpoint),
        )
        if old is not None and state is None:
            self.stage_modes['filter'] = 'reused'
            return old
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
                remote = self._remote()
                processed = {item.id for item in doc.tasks} | {item.id for item in doc.rejected}
                pending = [item for item in predecessor.tasks if item.id not in processed]
                accepted = {item.id: item for item in doc.tasks}
                rejected = {item.id: item for item in doc.rejected}
                active_state = state

                def filter_item(item: CollectRecord) -> FilterRecord | RejectedRecord:
                    """Bind this queue item to its hosting identity and source descriptor."""
                    return self._filter_one(item, remote, predecessor.meta.repository)

                def accept(item: CollectRecord, result: FilterRecord | RejectedRecord) -> None:
                    """Merge one filter result in input order and persist the checkpoint."""
                    nonlocal doc
                    if isinstance(result, FilterRecord):
                        accepted[item.id] = result
                    else:
                        rejected[item.id] = result
                    ordered_ok = [accepted[pr.id] for pr in predecessor.tasks if pr.id in accepted]
                    ordered_no = [rejected[pr.id] for pr in predecessor.tasks if pr.id in rejected]
                    doc = doc.model_copy(update={'tasks': ordered_ok, 'rejected': ordered_no})
                    doc = self._update(
                        doc,
                        counts=_counts(len(predecessor.tasks), len(ordered_ok), len(ordered_no)),
                        status='running',
                    )
                    self._checkpoint(store, doc, active_state)
                    if isinstance(result, RejectedRecord):
                        self._print_history_rejection(result, store.work_path)

                self._run_items(pending, self.config.filter.workers, filter_item, accept)
                if self.shutdown.sync():
                    doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                    self._checkpoint(store, doc, state)
                    raise PipelineInterrupted()
                state = {'phase': 'selection'}
                self._checkpoint(store, doc, state)
            doc = self._select_filter(doc, predecessor)
            if not doc.tasks:
                raise EmptyStageError('filter', details=_empty_filter_details(doc))
            state = {'phase': 'ready'}
            self._checkpoint(store, doc, state)
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            self._verified_predecessors('filter')
            self._publish(store, doc, state, 'collect')
            return doc
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
    ) -> tuple[TasksDocument, dict[str, Any] | None, dict[str, int]]:
        """Initialize or resume task progress without repeating completed work."""
        retry_attempts: dict[str, int] = {}
        if old is None:
            self.stage_modes['generate'] = 'new'
            fields = self._meta_fields(
                'generate',
                source.meta.repository,
                self._lineage(predecessors),
                _counts(len(source.tasks), 0),
                template_snapshot=template_snapshot,
            )
            document = TasksDocument(meta=GenerateMeta(**fields), tasks=[], failed=[])
            state = {'retry_attempts': {}}
            self._checkpoint(store, document, state)
        elif state is None:
            retry_attempts = {entry.record.id: entry.attempts for entry in old.failed}
            if not retry_attempts:
                self.stage_modes['generate'] = 'reused'
                return old, None, retry_attempts
            self.stage_modes['generate'] = 'resumed'
            document = old.model_copy(update={'failed': []})
            document = self._update(document, counts=_counts(len(source.tasks), len(document.tasks)), status='running')
            state = {'retry_attempts': retry_attempts}
            self._checkpoint(store, document, state)
        else:
            self.stage_modes['generate'] = 'resumed'
            retry_attempts = dict(state.get('retry_attempts', {}))
            if set(retry_attempts) - {record.id for record in source.tasks}:
                raise StorageError('invalid checkpoint retry_attempts: PR is absent from generation input')
            document = self._update(old, counts=old.meta.counts, status='running')
            if retry_failed and document.failed:
                retry_attempts.update({entry.record.id: entry.attempts for entry in document.failed})
                document = document.model_copy(update={'failed': []})
                document = self._update(
                    document, counts=_counts(len(source.tasks), len(document.tasks)), status='running'
                )
                state = {'retry_attempts': retry_attempts}
                self._checkpoint(store, document, state)
        return document, state, retry_attempts

    def _generate(self, overwrite: bool, retry_failed: bool) -> TasksDocument:
        """Generate tasks from one template snapshot while preserving retry progress."""
        predecessors = self._verified_predecessors('generate')
        source = predecessors[-1]
        assert isinstance(source, FilterDocument)
        self.progress.basket_update(available=len(source.tasks))
        store = StageStore(self.output_dir, 'generate')
        template_snapshot = load_template_snapshot(self.config.generation)
        self._generation_snapshot = template_snapshot
        old, state = store.load(
            TasksDocument,
            settings=effective_settings(self.config, 'generate', template_snapshot=template_snapshot),
            producer=self.producer,
            input_receipt=expected_input(source),
            overwrite=overwrite,
            allow_empty_retry=retry_failed,
            validate_loaded=lambda saved, checkpoint: validate_checkpoint_input(saved, source, checkpoint),
        )
        if old is not None and state is None and not retry_failed:
            self.stage_modes['generate'] = 'reused'
            return old
        doc, state, retry_attempts = self._prepare_generate(
            old, state, source, predecessors, store, template_snapshot, retry_failed
        )
        if state is None:
            return doc
        if state.get('phase') == 'ready':
            doc = self._update(doc, counts=doc.meta.counts, status='completed')
            self._verified_predecessors('generate')
            self._publish(store, doc, state, 'filter')
            return doc
        active_state = state
        try:
            completed = {item.id for item in doc.tasks} | {item.record.id for item in doc.failed}
            pending = [item for item in source.tasks if item.id not in completed]
            self.progress.update(
                retry_batch=len(pending) if retry_failed else None,
                kept_successful=len(doc.tasks) if retry_failed else None,
            )
            succeeded = {item.id: item for item in doc.tasks}
            failed = {item.record.id: item for item in doc.failed}

            def accept(item: FilterRecord, result: TaskRecord | FailedRecord) -> None:
                """Merge one task result in source order and persist the checkpoint."""
                nonlocal doc
                if isinstance(result, TaskRecord):
                    succeeded[item.id] = result
                else:
                    failed[item.id] = result
                good = [succeeded[pr.id] for pr in source.tasks if pr.id in succeeded]
                bad = [failed[pr.id] for pr in source.tasks if pr.id in failed]
                doc = doc.model_copy(update={'tasks': good, 'failed': bad})
                doc = self._update(doc, counts=_counts(len(source.tasks), len(good), failed=len(bad)), status='running')
                self._checkpoint(store, doc, active_state)

            if pending:
                client = self._llm() if self.config.generation.source == 'llm' else None
                self._run_items(
                    pending,
                    self.config.generation.workers,
                    lambda item: generate_record(
                        item,
                        self.config.generation,
                        llm_client=client,
                        stop_event=self.shutdown.stop_event,
                        previous_attempts=retry_attempts.get(item.id, 0),
                        template_snapshot=template_snapshot,
                    ),
                    accept,
                )
            if self.shutdown.sync():
                doc = self._update(doc, counts=doc.meta.counts, status='interrupted')
                self._checkpoint(store, doc, state)
                raise PipelineInterrupted()
            if not doc.tasks:
                raise EmptyStageError(
                    'generate', details=f'Failed={len(doc.failed)}; repair the cause and use RETRY_FAILED=1.'
                )
            counts = _counts(len(source.tasks), len(doc.tasks), failed=len(doc.failed))
            doc = self._update(doc, counts=counts, status='completed')
            self._verified_predecessors('generate')
            state = {'phase': 'ready'}
            self._publish(store, doc, state, 'filter')
            return doc
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
        old, state = store.load(
            BasketDocument,
            settings=effective_settings(self.config, 'export'),
            producer=self.producer,
            input_receipt=expected_input(tasks),
            overwrite=overwrite,
            validate_loaded=lambda saved, checkpoint: validate_checkpoint_input(saved, tasks, checkpoint),
        )
        if old is not None and state is None:
            self.stage_modes['export'] = 'reused'
            return old
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
        failed_ids = [item.record.id for item in tasks.failed]
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
            partial=bool(failed_ids),
            omitted_failed_ids=failed_ids,
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
            self._message(
                f'export: partial=true omitted_failed={len(document.meta.omitted_failed_ids)}; '
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

    def run(self, command: str, *, overwrite: bool = False, retry_failed: bool = False) -> int:
        """Execute one stage or the ordered basket under an exclusive output lock.

        Args:
                command: A stage name or ``basket``.
                overwrite: Rebuild selected stages from their inputs.
                retry_failed: Retry only completed per-PR generation failures.

        Returns:
                Zero on success or three when a direct generate run has failed PRs.

        Raises:
                PipelineInterrupted: If a signal stops the run after checkpointing.
        StorageError: If an input, checkpoint, or stage identity is inconsistent.

        """
        if command not in (*STAGES, 'basket'):
            raise ValueError(f'unknown command: {command}')
        if overwrite and retry_failed:
            raise ValueError('OVERWRITE=1 and RETRY_FAILED=1 cannot be combined')
        if retry_failed and command not in ('generate', 'basket'):
            raise ValueError('RETRY_FAILED=1 applies only to generate and basket')
        self._generation_snapshot = None
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
                            final = self._collect(overwrite)
                        elif stage == 'filter':
                            final = self._filter(overwrite)
                        elif stage == 'generate':
                            final = self._generate(overwrite, retry_failed)
                        else:
                            final = self._export(overwrite or retry_failed)
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
                if command == 'generate' and isinstance(final, TasksDocument) and final.failed:
                    return 3
                return 0
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
        return pipeline.run(command, overwrite=args.overwrite == '1', retry_failed=args.retry_failed == '1')
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
