"""Invocation history and private DEBUG logs shared by report operations."""

import logging
import re
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from benchmark.basket import Basket, load_basket
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.progress import Progress
from benchmark.storage import utc_now, write_json


class _FileFormatter(logging.Formatter):
    """Format complete diagnostics while redacting known secret values."""

    def __init__(self, secrets: list[str]) -> None:
        """Configure DEBUG formatting and in-memory redaction."""
        super().__init__('%(asctime)s %(levelname)s %(name)s: %(message)s')
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        """Redact secret values after formatting the full diagnostic record."""
        value = super().format(record)
        for secret in self.secrets:
            if secret:
                value = value.replace(secret, '<redacted>')
        return value


class _SafeFileHandler(logging.FileHandler):
    """Logging I/O failures must never dump raw records or tracebacks to the terminal."""

    warned = False

    def emit(self, record: logging.LogRecord) -> None:
        """Keep deferred file-open failures inside the disposable diagnostic sink."""
        try:
            super().emit(record)
        except (OSError, ValueError):
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        """Warn once about log I/O without exposing raw records or tracebacks."""
        if not self.warned:
            self.warned = True
            try:
                print(
                    'DEBUG file log could not be written; check disk space and permissions.',
                    file=sys.stderr,
                    flush=True,
                )
            except (OSError, ValueError):
                pass


class Command:
    """One invocation owns a unique log folder, metadata and user-facing messages."""

    def __init__(
        self,
        run_dir: Path,
        kind: str,
        *,
        config: dict | None = None,
        level: str = 'INFO',
        previous: str | None = None,
        secrets: list[str] | None = None,
        progress_mode: str | None = None,
        progress_total: int = 0,
        resumed: bool = False,
        invocation_id: str | None = None,
        phase_observer: Callable[[str, str], None] | None = None,
        console_writer: Callable[[str], None] | None = None,
    ) -> None:
        """Create one invocation with separate metadata and a private log."""
        self.run_dir = run_dir
        self._console_writer = console_writer
        self._closed = False
        self._observer_stop = threading.Event()
        self._observer: threading.Thread | None = None
        self._interrupt_count: Callable[[], int] | None = None
        self._last_interrupt = 0
        self.progress: Progress | None = None
        self.id = str(uuid.uuid4()) if invocation_id is None else invocation_id
        try:
            if str(uuid.UUID(self.id)) != self.id:
                raise ValueError('invocation ID must be canonical')
        except (ValueError, AttributeError) as error:
            raise BenchmarkError('invalid_invocation_id', 'Invocation ID must be a canonical UUID.') from error
        self.logs_dir = run_dir / '_meta' / 'invocations' / self.id
        self.logs_dir.mkdir(parents=True)
        self.level = level
        self.path = self.logs_dir / 'invocation.json'
        self.presentation_warnings: list[str] = []
        self._warned_baskets: set[str] = set()
        self._secrets = list(secrets or [])
        self._phase_observer = phase_observer
        self._task_labels: dict[str, tuple[int, int]] = {}
        self._progress_total = progress_total
        snapshot = None
        if config is not None:
            snapshot = f'_meta/invocations/{self.id}/config.json'
            safe_config = self._redact_snapshot(config)
            assert isinstance(safe_config, dict)
            write_json(run_dir / snapshot, safe_config, preserve_order=True)
        self.metadata = {
            'id': self.id,
            'kind': kind,
            'started_at': utc_now(),
            'finished_at': None,
            'status': 'running',
            'logs_dir': self.logs_dir.relative_to(run_dir).as_posix(),
            'config_snapshot': snapshot,
            'console_log_level': level,
            'previous_invocation_id': previous,
            'pricing_request': None,
        }
        if progress_mode is not None:
            self.metadata['progress_mode'] = progress_mode
        try:
            self.handler = _SafeFileHandler(self.logs_dir / 'benchmark.log', encoding='utf-8')
        except OSError:
            self.handler = _SafeFileHandler(self.logs_dir / 'benchmark.log', encoding='utf-8', delay=True)
            self._presentation_warning(
                'debug_log_failed', 'DEBUG file log could not be created; check disk space and permissions.'
            )
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(_FileFormatter(self._secrets))
        self.root = logging.getLogger()
        self.old_level = self.root.level
        self.root.setLevel(logging.DEBUG)
        self.root.addHandler(self.handler)
        try:
            if progress_mode is not None:
                self.progress = Progress(
                    progress_mode,
                    total=progress_total,
                    level=level,
                    resumed=resumed,
                    redact=self.redact,
                    log=self._observer_log,
                )
            self.update()
            self._human_log(f'Invocation {kind} started')
            basket_path = run_dir / 'basket.json'
            if basket_path.is_file():
                self.warn_basket(load_basket(basket_path))
        except OPERATION_ERRORS:
            self.close()
            raise

    def warn_basket(self, basket: Basket) -> None:
        """Emit each frozen basket's warnings only once in this invocation."""
        if basket.sha256 not in self._warned_baskets:
            self._warned_baskets.add(basket.sha256)
            for warning in basket.warnings():
                self.message(warning, level='WARNING')

    def update(self) -> None:
        """Persist the current invocation metadata."""
        write_json(self.path, self.metadata)

    def message(self, text: str, *, level: str = 'INFO') -> None:
        """Write a user message to DEBUG storage and its allowed console level."""
        safe = self._safe(text)
        logging.getLogger('benchmark.user').log(getattr(logging, level), safe)
        self._human_log(f'{level}: {safe}')
        # Mandatory warnings/errors bypass the configured console threshold.
        if level in ('WARNING', 'ERROR') or getattr(logging, level) >= getattr(logging, self.level):
            if self.progress is not None:
                self.progress.write_message(safe, enqueue=True)
            else:
                self._console(safe)

    def redact(self, text: str) -> str:
        """Use the live in-memory credential list for console and display output too."""
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, '<redacted>')
        return text

    def _redact_snapshot(self, value: object) -> object:
        """Redact decoded config values so escaped credentials cannot reach snapshots."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {str(self._redact_snapshot(key)): self._redact_snapshot(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_snapshot(item) for item in value]
        return value

    def _safe(self, text: str) -> str:
        """Remove terminal controls from redacted human messages."""
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.redact(text))
        return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text).replace('\r', '')

    def _console(self, text: str) -> None:
        """Keep a detached console from becoming a lifecycle failure."""
        try:
            if self._console_writer is not None:
                self._console_writer(self._safe(text))
            else:
                print(self._safe(text), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass

    def _presentation_warning(self, code: str, text: str) -> None:
        """Report each optional sink failure without routing recursively into that sink."""
        if code not in self.presentation_warnings:
            self.presentation_warnings.append(code)
            if self.progress is not None:
                self.progress.write_message(text, enqueue=True)
            else:
                self._console(text)

    def _human_log(self, text: str) -> None:
        """Append a live UTC lifecycle block without coupling it to experiment evidence."""
        lines = self._safe(text).splitlines() or ['']
        record = f'[{utc_now()}] {lines[0]}\n' + ''.join(f'    {line}\n' for line in lines[1:])
        try:
            with (self.run_dir / 'run.log').open('a', encoding='utf-8') as output:
                output.write(record)
                output.flush()
        except (OSError, ValueError):
            self._presentation_warning(
                'run_log_failed', 'Readable run log could not be written; check disk space and permissions.'
            )

    def _observer_log(self, text: str, *, level: str = 'INFO', name: str = 'benchmark.progress') -> None:
        """Append observer diagnostics without acquiring the command handler lock."""
        formatter = self.handler.formatter
        record = logging.LogRecord(name, getattr(logging, level), __file__, 0, text, (), None)
        rendered = formatter.format(record) if formatter is not None else text
        try:
            with (self.logs_dir / 'benchmark.log').open('ab', buffering=0) as output:
                output.write((rendered + '\n').encode('utf-8'))
        except (OSError, ValueError):
            self._presentation_warning(
                'debug_log_failed', 'DEBUG file log could not be written; check disk space and permissions.'
            )

    @staticmethod
    def _normalize_phase(name: str) -> str:
        """Map internal task phases onto the bounded English presentation vocabulary."""
        aliases = {
            'git': 'preparing',
            'config': 'preparing',
            'agent': 'running',
            'capture': 'saving',
            'telemetry': 'saving',
            'score': 'scoring',
            'scoring-recovery': 'recovering score',
        }
        phase = aliases.get(name, name)
        allowed = {
            'preparing',
            'setup',
            'running',
            'saving',
            'scoring',
            'retry pending',
            'stopping',
            'recovering score',
        }
        return phase if phase in allowed else 'preparing'

    def _task_label(self, task_id: str) -> str | None:
        """Resolve internal task identity to its public ordinal and attempt label."""
        value = self._task_labels.get(task_id)
        if value is None:
            return None
        ordinal, attempt = value
        return f'Task {ordinal:0{max(3, len(str(self._progress_total)))}d}/{self._progress_total} · attempt {attempt}'

    def phase(self, name: str, task_id: str | None = None) -> None:
        """Update observation only; worker callbacks enqueue bounded parent snapshots."""
        if task_id is not None:
            self.task_phase(task_id, name)
        elif self.progress is not None:
            self.progress.phase(name)

    def task_start(self, task_id: str, *, ordinal: int, attempt_sequence: int = 1) -> None:
        """Present a task only after the coordinator has durably saved its start."""
        self._task_labels[task_id] = (ordinal, attempt_sequence)
        if self.progress is not None:
            self.progress.task_start(task_id, ordinal=ordinal, attempt_sequence=attempt_sequence)
        self._human_log(f'{self._task_label(task_id)} · preparing')

    def task_phase(self, task_id: str, phase: str, *, attempt_sequence: int | None = None) -> None:
        """Keep a logical task row across attempts and publish an optional observation."""
        phase = self._normalize_phase(phase)
        if attempt_sequence is not None and task_id in self._task_labels:
            self._task_labels[task_id] = (self._task_labels[task_id][0], attempt_sequence)
        if self.progress is not None:
            self.progress.task_phase(task_id, phase, attempt_sequence=attempt_sequence)
            self.progress.phase(phase, task_id)
        label = self._task_label(task_id)
        if label is not None:
            self._human_log(f'{label} · {phase}')
        if self._phase_observer is not None:
            try:
                self._phase_observer(task_id, phase)
            except Exception:  # noqa: BLE001 - optional presentation observer
                self._presentation_warning('phase_observer_failed', 'Task phase observer could not accept an update.')

    def task_finish(self, task_id: str, *, execution: str, scoring: str, attempt_sequence: int | None = None) -> None:
        """Append a terminal outcome after durable selection, then release the task label."""
        if attempt_sequence is not None and task_id in self._task_labels:
            self._task_labels[task_id] = (self._task_labels[task_id][0], attempt_sequence)
        label = self._task_label(task_id)
        if label is not None:
            self._human_log(f'{label} · {execution} · {scoring}')
        if self.progress is not None:
            self.progress.task_finish(task_id, execution=execution, scoring=scoring, attempt_sequence=attempt_sequence)
            self.progress.worker_done(task_id)
        self._task_labels.pop(task_id, None)

    def worker_done(self, task_id: str) -> None:
        """Release an attempt's ephemeral phase."""
        if self.progress is not None:
            self.progress.worker_done(task_id)

    def diagnostic(self, error: BaseException) -> None:
        """Store a complete exception and traceback only in the DEBUG log."""
        logging.getLogger('benchmark.diagnostics').debug(
            'Full diagnostic: %s', error, exc_info=(type(error), error, error.__traceback__)
        )

    def watch_interrupts(self, count: Callable[[], int]) -> None:
        """Notify outside the signal handler, even while the coordinator saves artifacts."""
        self._interrupt_count = count
        self._observer = threading.Thread(target=self._observe, name='benchmark-display', daemon=True)
        self._observer.start()

    def _observe(self) -> None:
        """Keep repeated-signal notices independent of the run's console verbosity."""
        while not self._observer_stop.wait(0.05):
            count = 0 if self._interrupt_count is None else self._interrupt_count()
            if count != self._last_interrupt:
                self._last_interrupt = count
                try:
                    notice = (
                        'Shutdown is already in progress and may take some time. Please wait.'
                        if count > 1
                        else 'Stopping tasks and saving partial artifacts; please wait.'
                    )
                    self._human_log(f'WARNING: {notice}')
                    try:
                        self._observer_log(notice, level='WARNING', name='benchmark.user')
                    except (OSError, ValueError):
                        pass
                    if self.progress is not None:
                        self.progress.write_message(notice, enqueue=True)
                    else:
                        self._console(notice)
                except (OSError, ValueError):
                    pass
            if self.progress is not None:
                if count:
                    self.progress.draining()
                self.progress.poll()
        if self.progress is not None:
            self.progress.poll(final=True)
            self.progress.close()

    def add_secret(self, value: str) -> None:
        """Dynamic interactive credentials stay only in the in-memory redactor."""
        if value:
            self._secrets.append(value)

    def finish(self, status: str) -> None:
        """Save the terminal invocation state and release its logger."""
        self.metadata.update(status=status, finished_at=utc_now())
        try:
            self.update()
            self._human_log(f'Invocation {self.metadata['kind']} {status}')
            if self.progress is not None:
                self.progress.finish(status, render=False)
        finally:
            self.close()

    def close(self) -> None:
        """Restore process logging and close the invocation file handler."""
        if self._closed:
            return
        self._observer_stop.set()
        if self._observer is not None:
            self._observer.join(timeout=0.5)
        if self.progress is not None:
            self.progress.close(blocking=False)
        self._closed = True
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.old_level)
        self.handler.close()
