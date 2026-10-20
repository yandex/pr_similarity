"""Ephemeral stage progress with bounded terminal rendering and plain heartbeats."""

import logging
import os
import sys
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, TextIO

DISPLAY_LOCK = threading.RLock()
MODES = ('auto', 'plain', 'off')
_UNSET = object()


@dataclass(frozen=True)
class Snapshot:
    """Display data that never enters saved stage documents or settings."""

    stage: str
    phase: str
    status: str = 'running'
    mode: str = 'new'
    n: int = 0
    total: int | None = None
    started: float = 0
    metrics: dict[str, Any] = field(default_factory=dict)


class Progress:
    """The coordinator owns counters; workers submit only coalesced wait reasons."""

    def __init__(
        self,
        mode: str = 'auto',
        *,
        level: str | int = 'INFO',
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        redact: Callable[[str], str] = lambda text: text,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """Choose terminal/plain/off without opening files or changing semantic inputs."""
        if mode not in MODES:
            raise ValueError('invalid progress mode; choose auto, plain or off')
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock
        self.redact = redact
        self.log = log
        self.level = getattr(logging, level) if isinstance(level, str) else level
        try:
            tty = self.stream.isatty()
        except (OSError, ValueError):
            tty = False
        self.mode = 'tty' if mode == 'auto' and tty else 'plain' if mode == 'auto' else mode
        self.disabled = False
        self.snapshots: dict[str, Snapshot] = {}
        self.current: str | None = None
        self._frame_rows = 0
        self._finished: set[str] = set()
        self._rendered: dict[str, float] = {}
        self._logged: dict[str, float] = {}
        self._worker_lock = threading.Lock()
        self._workers: dict[int, tuple[str, str]] = {}
        self._had_workers = False

    def start(self, stage: str, phase: str, *, mode: str = 'new') -> None:
        """Announce a stage before its first potentially slow operation."""
        self.current = stage
        self._finished.discard(stage)
        self.snapshots[stage] = Snapshot(stage, phase, mode=mode, started=self.clock())
        self._render(stage, force=True)

    def update(
        self,
        *,
        phase: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        n: int | None = None,
        total: object = _UNSET,
        **metrics: object,
    ) -> None:
        """Update the current stage from successfully saved coordinator state."""
        if self.current is None:
            return
        old = self.snapshots[self.current]
        if total is _UNSET:
            total = old.total
        if total is not None and not isinstance(total, int):
            raise ValueError('progress total must be an integer or None')
        changed_phase = phase is not None and phase != old.phase
        new = replace(
            old,
            phase=phase if phase is not None else old.phase,
            status=status if status is not None else old.status,
            mode=mode if mode is not None else old.mode,
            n=n if n is not None else old.n,
            total=total,
            metrics={**old.metrics, **metrics},
        )
        self.snapshots[self.current] = new
        self._render(self.current, force=changed_phase or (status == 'draining' and status != old.status))

    def worker_started(self, reason: str = 'processing') -> None:
        """Register at most one ephemeral wait entry per scheduled worker."""
        with self._worker_lock:
            self._workers[threading.get_ident()] = (reason, reason)

    def wait(self, reason: str) -> None:
        """Coalesce source-client observations without rendering on a worker thread."""
        with self._worker_lock:
            identity = threading.get_ident()
            if identity in self._workers:
                default, _previous = self._workers[identity]
                self._workers[identity] = (default, reason or default)

    def worker_finished(self) -> None:
        """Release a worker's display state even when the operation raises."""
        with self._worker_lock:
            self._workers.pop(threading.get_ident(), None)

    def poll(self) -> None:
        """Refresh a heartbeat from existing coordinator polling loops."""
        with self._worker_lock:
            reasons = Counter(reason for _default, reason in self._workers.values())
        active = sum(reasons.values())
        if active or self._had_workers:
            waiting = active > 0 and 'processing' not in reasons
            summary = ', '.join(f'{reason}:{count}' for reason, count in sorted(reasons.items()))
            current = self.snapshots.get(self.current or '')
            status = (
                'draining'
                if current is not None and current.status == 'draining'
                else 'waiting' if waiting else 'running'
            )
            self.update(status=status, active=active, wait=summary)
        else:
            if self.current is not None:
                self._render(self.current)
        self._had_workers = active > 0
        if 'basket' in self.snapshots:
            self._render('basket')

    def finish(self, status: str, *, mode: str | None = None) -> None:
        """Retain the actual final value instead of forcing a complete bar."""
        if self.current is None:
            return
        key = self.current
        old = self.snapshots[key]
        self.snapshots[key] = replace(
            old, status=status, mode=mode or old.mode, metrics={**old.metrics, 'active': 0, 'wait': ''}
        )
        self._render(key, force=True)
        self._close_bar(key)

    def basket(self, requested: int | None) -> None:
        """Keep the requested result limit separate from stage work queues."""
        self.snapshots['basket'] = Snapshot('basket', '', mode='', total=requested, started=self.clock())
        self._render('basket', force=True)

    def basket_update(
        self,
        *,
        available: int | None = None,
        successful: int | None = None,
        failed: int | None = None,
        omitted: int | None = None,
    ) -> None:
        """Count only persisted successful tasks while keeping candidate shortfall explicit."""
        old = self.snapshots.get('basket')
        if old is None:
            return
        metrics = dict(old.metrics)
        total = old.total
        if available is not None:
            metrics['available'] = available
            if total is None:
                total = available
            metrics['candidate_shortfall'] = max(0, total - available)
        if failed is not None:
            metrics['failed'] = failed
        if omitted is not None:
            metrics['omissions'] = omitted
        self.snapshots['basket'] = replace(
            old, total=total, n=old.n if successful is None else successful, metrics=metrics
        )
        self._render('basket')

    def finish_basket(self, status: str) -> None:
        """Leave undersized, partial and interrupted baskets at their actual counts."""
        old = self.snapshots.get('basket')
        if old is None:
            return
        self.snapshots['basket'] = replace(old, status=status)
        self._render('basket', force=True)
        self._close_bar('basket')

    def _line(self, snapshot: Snapshot) -> str:
        """Build a redacted, escape-free snapshot for plain output and DEBUG logging."""
        prefix = ' '.join(value for value in (snapshot.stage, snapshot.phase, snapshot.status) if value)
        if snapshot.mode:
            prefix += f' mode={snapshot.mode}'
        unit = 'tasks' if snapshot.stage == 'basket' else 'pages' if snapshot.phase == 'discovery' else 'processed'
        count = str(snapshot.n) if snapshot.total is None else f'{snapshot.n}/{snapshot.total}'
        parts = [prefix, f'{unit}={count}']
        parts.extend(f'{key}={value}' for key, value in snapshot.metrics.items() if value is not None and value != '')
        parts.append(f'elapsed={max(0, self.clock() - snapshot.started):.0f}s')
        return self._safe(' '.join(parts))

    def _safe(self, text: str) -> str:
        """Remove control characters before terminal rendering after credential redaction."""
        return ''.join(' ' if unicodedata.category(char).startswith('C') else char for char in self.redact(text))

    def _terminal_size(self) -> tuple[int, int]:
        """Read and re-read dimensions from the actual output stream, including zero-size PTYs."""
        try:
            size = os.get_terminal_size(self.stream.fileno())
            return max(2, size.columns or 80), max(2, size.lines or 24)
        except (OSError, ValueError, AttributeError):
            return 80, 24

    @staticmethod
    def _clip(text: str, width: int) -> str:
        """Bound physical terminal cells, preserving combining marks and wide Unicode characters."""
        used = 0
        result = []
        for char in text:
            cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            if used + cells > width:
                return ''.join(result)
            result.append(char)
            used += cells
        return ''.join(result)

    def _panel(self, width: int, height: int) -> list[str]:
        """Separate status and counters into a bounded viewport without duplicated postfixes."""
        rows = []
        keys = ['basket', self.current] if self.current != 'basket' else ['basket']
        for key in keys:
            if key is None or key not in self.snapshots or key in self._finished:
                continue
            snapshot = self.snapshots[key]
            count = str(snapshot.n) if snapshot.total is None else f'{snapshot.n}/{snapshot.total}'
            rows.append(self._safe(f'{snapshot.stage} {snapshot.phase} {snapshot.status} {count}'))
            tokens = [
                f'{name}={value}' for name, value in snapshot.metrics.items() if value is not None and value != ''
            ]
            tokens.append(f'elapsed={max(0, self.clock() - snapshot.started):.0f}s')
            line = '  '
            for token in tokens:
                token = self._safe(token)
                candidate = line + token + ' '
                if self._clip(candidate, width) != candidate and line.strip():
                    rows.append(line.rstrip())
                    line = '  '
                line += token + ' '
            if line.strip():
                rows.append(line.rstrip())
        if len(rows) > height:
            hidden = len(rows) - height + 1
            rows = [*rows[: height - 1], f'... {hidden} detail rows hidden']
        return [self._clip(row, width) for row in rows]

    def _summary_lines(self, text: str) -> list[str]:
        """Wrap durable TTY messages without reaching the terminal's auto-wrap column."""
        width = self._terminal_size()[0] - 1
        rows = []
        line = ''
        for token in self._safe(text).split():
            token = self._clip(token, width)
            candidate = (line + ' ' + token).strip()
            if line and self._clip(candidate, width) != candidate:
                rows.append(line)
                line = token
            else:
                line = candidate
        if line:
            rows.append(line)
        return rows

    def _clear_frame(self) -> None:
        """Erase exactly the previously drawn physical rows and leave the cursor at their start."""
        if self._frame_rows:
            self.stream.write(f'\x1b[{self._frame_rows}A')
            for _ in range(self._frame_rows):
                self.stream.write('\r\x1b[2K\n')
            self.stream.write(f'\x1b[{self._frame_rows}A\r')
            self._frame_rows = 0

    def _draw_frame(self) -> None:
        """Refresh the complete panel under the single display lock."""
        columns, lines = self._terminal_size()
        self._clear_frame()
        rows = self._panel(columns - 1, min(8, lines - 1))
        for row in rows:
            self.stream.write(row + '\n')
        self._frame_rows = len(rows)
        self.stream.flush()

    def _render(self, key: str, *, force: bool = False) -> None:
        """Rate-limit display and file snapshots, degrading safely on terminal failure."""
        if self.disabled:
            return
        try:
            now = self.clock()
            snapshot = self.snapshots[key]
            line = self._line(snapshot)
            if self.log is not None and (force or now - self._logged.get(key, float('-inf')) >= 5):
                self.log(line)
                self._logged[key] = now
            if self.mode == 'off' or self.level > logging.INFO:
                return
            interval = 0.25 if self.mode == 'tty' else 5
            if not force and now - self._rendered.get(key, float('-inf')) < interval:
                return
            with DISPLAY_LOCK:
                if self.mode == 'tty':
                    self._draw_frame()
                else:
                    print(line, file=self.stream, flush=True)
            self._rendered[key] = now
        except Exception:
            self.disabled = True
            self.close()

    def write_message(self, text: str, *, stream: TextIO) -> None:
        """Write mandatory messages under the same lock as bars without leaking failures."""
        try:
            with DISPLAY_LOCK:
                if self.mode == 'tty' and not self.disabled:
                    self._clear_frame()
                    for line in self._summary_lines(text):
                        print(line, file=stream, flush=True)
                    self._draw_frame()
                else:
                    print(text, file=stream, flush=True)
        except Exception:
            self.disabled = True
            self.close()
            try:
                with DISPLAY_LOCK:
                    print(text, file=stream, flush=True)
            except Exception:
                # InvocationLogs has retained the safe message. A broken stream
                # must not turn a display failure into a stage failure.
                pass

    def _close_bar(self, key: str) -> None:
        """Close an existing bar without creating an artificial final update."""
        if key in self._finished:
            return
        self._finished.add(key)
        if self.mode != 'tty' or self.disabled or self.level > logging.INFO:
            return
        try:
            with DISPLAY_LOCK:
                self._clear_frame()
                for line in self._summary_lines(self._line(self.snapshots[key])):
                    self.stream.write(line + '\n')
                self._draw_frame()
        except Exception:
            self.disabled = True

    def close(self) -> None:
        """Close up to four stage bars and one result bar."""
        try:
            with DISPLAY_LOCK:
                self._clear_frame()
                self.stream.flush()
        except Exception:
            self.disabled = True
