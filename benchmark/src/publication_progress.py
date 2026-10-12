"""Disposable elapsed feedback; never changes the publication journal or HTTP outcome."""

import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO


class PublicationProgress:
    """Render bounded heartbeats independently of blocking HTTP and hidden prompts."""

    def __init__(
        self, mode: str = 'auto', *, stream: TextIO | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.stream = sys.stderr if stream is None else stream
        self.clock = clock
        try:
            self.tty = mode == 'auto' and self.stream.isatty()
        except (OSError, ValueError):
            self.tty = False
        self.enabled = mode != 'off'
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.active = False
        self.phase_name = ''
        self.started = self.clock()
        self.last = self.started
        self.line = False
        self.thread: threading.Thread | None = None
        if not self.enabled:
            return
        self.thread = threading.Thread(target=self._run, name='publication-progress', daemon=True)
        try:
            self.thread.start()
        except (OSError, RuntimeError):
            self.enabled = False
            self.thread = None

    def phase(self, name: str, *, active: bool = True, reset: bool = True) -> None:
        with self.lock:
            self._clear()
            self.phase_name, self.active = name, active
            if reset:
                self.started = self.clock()
            self.last = self.clock()
            self._render(transient=active)

    def _render(self, *, transient: bool = True) -> None:
        if not self.enabled:
            return
        try:
            elapsed = max(0, int(self.clock() - self.started))
            minutes, seconds = divmod(elapsed, 60)
            text = f'{self.phase_name} · elapsed {minutes}m {seconds:02d}s'
            self.stream.write(
                ('\r\x1b[2K' if self.tty and transient else '') + text + ('' if self.tty and transient else '\n')
            )
            self.stream.flush()
            self.line = self.tty and transient
        except (OSError, ValueError):
            self.enabled = False

    def tick(self) -> None:
        """Clock injection permits bounded delayed-response verification without sleeping."""
        with self.lock:
            if self.active and self.clock() - self.last >= (1 if self.tty else 10):
                self._render()
                self.last = self.clock()

    def _run(self) -> None:
        while not self.stop.wait(0.25):
            self.tick()

    def _clear(self) -> None:
        if self.line:
            try:
                self.stream.write('\r\x1b[2K')
                self.stream.flush()
            except (OSError, ValueError):
                self.enabled = False
            self.line = False

    def pause(self) -> None:
        with self.lock:
            self.active = False
            self._clear()

    def write_message(self, text: str) -> None:
        """Keep already-redacted foreground messages separate from the TTY status row."""
        with self.lock:
            self._clear()
            self.stream.write(text + '\n')
            self.stream.flush()
            if self.active:
                self._render()

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.pause()
