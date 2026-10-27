"""Disposable progress over saved task counters and ephemeral worker phases."""

import io
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TextIO

MODES = ('auto', 'plain', 'off')
DISPLAY_LOCK = threading.RLock()
DISPLAY_ERRORS = (OSError, ValueError, RuntimeError, TypeError, AttributeError, AssertionError)


@dataclass
class TaskProgress:
    """Describe one ephemeral task row independently from persisted run state."""

    ordinal: int
    attempt: int
    phase: str
    started: float
    elapsed: float | None = None
    mf: float | None = None
    lcf: float | None = None


class Progress:
    """The coordinator owns counters; a display observer renders bounded snapshots."""

    def __init__(
        self,
        mode: str,
        *,
        total: int,
        level: str = 'INFO',
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        redact: Callable[[str], str] = lambda value: value,
        log: Callable[[str], None] | None = None,
        resumed: bool = False,
    ) -> None:
        """Select TTY/plain/off without persisting state or doing external work."""
        if mode not in MODES:
            raise ValueError('PROGRESS must be auto, plain or off.')
        self.stream = sys.stderr if stream is None else stream
        try:
            tty = self.stream.isatty()
        except (OSError, ValueError):
            tty = False
        self.mode = ('tty' if tty else 'plain') if mode == 'auto' else mode
        self.level = getattr(logging, level)
        self.clock, self.redact, self.log = clock, redact, log
        self.started = clock()
        self.total, self.completed, self.active, self.retries, self.errors = total, 0, 0, 0, 0
        self.phase_name, self.status = 'preflight', 'running'
        self.resumed = resumed
        self.workers: dict[str, str] = {}
        self._lock = threading.RLock()
        self._bar = None
        self.tasks: dict[str, TaskProgress] = {}
        self._finished: set[str] = set()
        self._task_messages: deque[tuple[str, bool]] = deque()
        self._viewport_lines = 0
        self._rendered = float('-inf')
        self._logged = float('-inf')
        self._last_logged_line: str | None = None
        self._messages: deque[str] = deque(maxlen=256)
        self._force = True
        self._disabled = False
        self._closed = False

    def counts(self, *, completed: int, active: int, retries: int, errors: int) -> None:
        """Accept coordinator counters only after durable persistence succeeds."""
        with self._lock:
            self._force |= (self.completed, self.active, self.retries, self.errors) != (
                completed,
                active,
                retries,
                errors,
            )
            self.completed, self.active, self.retries, self.errors = completed, active, retries, errors

    def phase(self, name: str, task_id: str | None = None) -> None:
        """Coalesce at most one phase per active task, without rendering in a worker."""
        with self._lock:
            if task_id is None:
                self._force |= self.phase_name != name
                self.phase_name = name
            else:
                self._force |= self.workers.get(task_id) != name
                self.workers[task_id] = name
                self.task_phase(task_id, name)

    def task_start(self, task_id: str, *, ordinal: int, attempt_sequence: int = 1) -> None:
        """Create a logical row only after its durable task start; retries reuse it."""
        with self._lock:
            if task_id in self._finished:
                return
            previous = self.tasks.get(task_id)
            started = (
                previous.started if previous is not None and previous.attempt == attempt_sequence else self.clock()
            )
            self.tasks[task_id] = TaskProgress(ordinal, attempt_sequence, 'preparing', started)
            self._force = True
            if previous != self.tasks[task_id]:
                self._task_messages.append((self._task_line(self.tasks[task_id]), False))

    def task_phase(self, task_id: str, phase: str, *, attempt_sequence: int | None = None) -> None:
        """Update only an already-started task, preserving its ordinal across retries."""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            previous = replace(task)
            aliases = {'git': 'preparing', 'agent': 'running', 'capture': 'saving', 'score': 'scoring'}
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
            task.phase = aliases.get(phase, phase) if aliases.get(phase, phase) in allowed else 'preparing'
            if attempt_sequence is not None:
                task.attempt = attempt_sequence
            if previous != task:
                self._force = True
                self._task_messages.append((self._task_line(task), False))

    def task_result(self, task_id: str, *, mf: float | None, lcf: float | None) -> None:
        """Freeze this attempt's elapsed time and already-persisted quality scores."""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            if task.elapsed is None:
                task.elapsed = max(0, self.clock() - task.started)
            task.mf, task.lcf = mf, lcf
            self._force = True

    def task_finish(
        self,
        task_id: str,
        *,
        execution: str,
        scoring: str,
        attempt_sequence: int | None = None,
    ) -> None:
        """Freeze a single final row only after the coordinator saved its outcome."""
        with self._lock:
            task = self.tasks.pop(task_id, None)
            if task is None or task_id in self._finished:
                return
            self._finished.add(task_id)
            if attempt_sequence is not None:
                task.attempt = attempt_sequence
            execution = (
                execution
                if execution in {'completed', 'timeout', 'error', 'cancelled', 'not_started'}
                else 'unavailable'
            )
            scoring = (
                scoring
                if scoring in {'scored', 'completed', 'unavailable', 'failed', 'cancelled', 'not_started'}
                else 'unavailable'
            )
            if task.elapsed is None:
                task.elapsed = max(0, self.clock() - task.started)
            task.phase = f'{execution} · {scoring}'
            self._task_messages.append((self._task_line(task), True))
            self._force = True

    def _task_line(self, task: TaskProgress, width: int | None = None) -> str:
        """Format public ordinals, attempts and statuses without opaque identifiers."""
        ordinal = f'{task.ordinal:0{max(3, len(str(self.total)))}d}'
        elapsed = task.elapsed if task.elapsed is not None else self.clock() - task.started
        details = f'{task.phase} · {tqdm_interval(elapsed)}'
        if task.mf is not None and task.lcf is not None:
            details += f' · Mf={task.mf:.3f} LCf={task.lcf:.3f}'
        line = f'Task {ordinal}/{self.total} · attempt {task.attempt} · {details}'
        if width is not None and len(line) > width:
            line = f'T{ordinal} a{task.attempt} {details}'
            if len(line) > width:
                line = line[:width]
        return self._safe(line)

    def _fit_task_message(self, message: str, width: int) -> str:
        """Preserve attempt and outcome labels when terminal width is restricted."""
        if len(message) <= width:
            return message
        match = re.fullmatch(r'Task (\d+)/\d+ · attempt (\d+) · (.*)', message)
        if match:
            message = f'T{match[1]} a{match[2]} {match[3]}'
        return message[:width]

    def _terminal_size(self) -> os.terminal_size:
        """Read the actual output terminal, falling back for detached observers."""
        try:
            return os.get_terminal_size(self.stream.fileno())
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            return shutil.get_terminal_size(fallback=(80, 24))

    def _clear_viewport(self) -> None:
        """Erase only the active region, leaving frozen history intact."""
        if self._viewport_lines:
            self.stream.write(f'\x1b[{self._viewport_lines}A\r\x1b[J')
            self._viewport_lines = 0

    def worker_done(self, task_id: str) -> None:
        """Release task display state without retaining historical event streams."""
        with self._lock:
            self._force |= task_id in self.workers
            self.workers.pop(task_id, None)

    def draining(self) -> None:
        """Expose cancellation without inventing a new saved task state."""
        with self._lock:
            self._force |= self.status != 'draining'
            self.status = 'draining'

    def _safe(self, text: str) -> str:
        """Redact known credentials and remove terminal control characters."""
        return re.sub(r'[\x00-\x1f\x7f]', ' ', self.redact(text))

    def _line(self) -> str:
        """Summarize durable counters and ephemeral phase counts for heartbeats."""
        phases = ' '.join(f'{name}={count}' for name, count in sorted(Counter(self.workers.values()).items()))
        return self._safe(
            f"run {self.phase_name} {self.status} mode={'resumed' if self.resumed else 'new'} "
            f"completed={self.completed}/{self.total} active={self.active} "
            f"pending={max(0, self.total - self.completed - self.active)} "
            f"retries={self.retries} errors={self.errors} "
            f"elapsed={max(0, self.clock() - self.started):.0f}s {phases}"
        ).rstrip()

    def poll(self, *, final: bool = False) -> None:
        """Snapshot under the model lock, then perform disposable I/O only in the observer."""
        with self._lock:
            if self._closed or self._disabled:
                return
            now, line = self.clock(), self._line()
            force = self._force or final
            messages = list(self._messages)
            task_messages = list(self._task_messages)
            task_rows = [replace(task) for task in self.tasks.values()]
            summary = self._tty_summary(now, bool(task_rows))
            self._messages.clear()
            write_log = self.log is not None and ((force and line != self._last_logged_line) or now - self._logged >= 5)
            if write_log:
                self._logged, self._last_logged_line = now, line
            self._force = False
            visible = (self.mode != 'off' and self.level <= logging.INFO) or final
            interval = 0.25 if self.mode == 'tty' else 5
            render = visible and (force or now - self._rendered >= interval)
            if self.mode == 'tty' and not final and now - self._rendered < 0.25:
                render = False
                self._force = force
            if render:
                self._rendered = now
                self._task_messages.clear()
            elif not visible:
                self._task_messages.clear()
        # No coordinator or worker needs a lock held across log/terminal I/O.
        if write_log and self.log is not None:
            try:
                self.log(line)
            except DISPLAY_ERRORS:
                self.log = None
        try:
            with DISPLAY_LOCK:
                for text in messages:
                    self.write_message(text)
                if not render:
                    return
                if self.mode == 'tty':
                    self._render_tty(task_messages, task_rows, summary, line, final)
                else:
                    for message, _terminal in task_messages:
                        print(message, file=self.stream, flush=True)
                    print(line, file=self.stream, flush=True)
        except DISPLAY_ERRORS:
            self._disabled = True

    def _tty_summary(self, now: float, has_tasks: bool) -> str:
        """Snapshot counters and phases while the model lock protects worker updates."""
        prefix = 'resume ' if self.resumed else ''
        counts = f'{self.completed}/{self.total}'
        counters = f'active={self.active} retries={self.retries} errors={self.errors}'
        elapsed = tqdm_interval(now - self.started)
        summary = f'{prefix}Completed {counts} · {counters} · {elapsed}'
        if not has_tasks:
            phases = ','.join(f'{name}:{count}' for name, count in sorted(Counter(self.workers.values()).items()))
            summary = f'{prefix}{phases or self.phase_name} {counts} · {counters} · {elapsed}'
        return summary

    def _render_tty(
        self, task_messages: list[tuple[str, bool]], task_rows: list[TaskProgress], summary: str, line: str, final: bool
    ) -> None:
        """Render bounded active rows and append frozen outcomes to terminal history."""
        size = self._terminal_size()
        width = max(1, size.columns)
        self._clear_viewport()
        # Only terminal outcomes enter frozen history in TTY mode.
        for message, terminal in task_messages:
            if terminal:
                self.stream.write(self._fit_task_message(message, width) + '\n')
        capacity = max(0, size.lines - 3)
        hidden = max(0, len(task_rows) - capacity)
        if hidden and capacity:
            capacity -= 1
            hidden = len(task_rows) - capacity
        lines = [summary[:width]] + [self._task_line(task, width) for task in task_rows[:capacity]]
        if hidden:
            lines.append(f'{hidden} active tasks hidden'[:width])
        if final:
            lines.append(line[:width])
        self.stream.write(''.join(value + '\n' for value in lines))
        self.stream.flush()
        self._viewport_lines = 0 if final else len(lines)

    def write_message(self, text: str, *, enqueue: bool = False) -> None:
        """Run writers enqueue bounded messages; only the display owner performs I/O."""
        if enqueue:
            with self._lock:
                self._messages.append(self._safe(text))
            return
        try:
            with DISPLAY_LOCK:
                safe = self._safe(text)
                if self.mode == 'tty' and not self._disabled:
                    self._clear_viewport()
                    self._force = True
                print(safe, file=self.stream, flush=True)
        except DISPLAY_ERRORS:
            self._disabled = True

    def finish(self, status: str, *, render: bool = True) -> None:
        """Publish final saved counters; Command delegates final I/O to its observer."""
        with self._lock:
            self.status, self.active = status, 0
            self.workers.clear()
            self._force = True
        if render:
            self.poll(final=True)
            self.close()

    def close(self, *, blocking: bool = True) -> None:
        """Close without holding coordinator cleanup behind a blocked observer."""
        with self._lock:
            self._closed = True
        if not DISPLAY_LOCK.acquire(blocking=blocking):
            return
        try:
            self._viewport_lines = 0
        finally:
            DISPLAY_LOCK.release()


def tqdm_interval(seconds: float) -> str:
    """Format elapsed time independently of terminal rendering."""
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'
