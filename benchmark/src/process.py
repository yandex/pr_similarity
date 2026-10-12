"""Bounded process lifecycle and redacted streaming output for task phases."""

import os
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, cast

from benchmark.errors import BenchmarkError


class Redactor:
    """Keep enough bytes between chunks to mask secrets crossing read boundaries."""

    def __init__(self, secrets: list[str]) -> None:
        """Initialize a streaming buffer for known secret spans."""
        values = sorted({value.encode('utf-8') for value in secrets if value}, key=len, reverse=True)
        self.pattern = re.compile(b'|'.join(re.escape(value) for value in values)) if values else None
        self.reserve = max((len(value) for value in values), default=1) - 1
        self.pending = b''

    def feed(self, content: bytes, *, final: bool = False) -> bytes:
        """Redact complete secret spans across arbitrary stream chunks."""
        self.pending += content
        boundary = len(self.pending) if final else max(0, len(self.pending) - self.reserve)
        if self.pattern is None:
            result, self.pending = self.pending[:boundary], self.pending[boundary:]
            return result
        for match in self.pattern.finditer(self.pending):
            if match.start() < boundary:
                boundary = max(boundary, match.end())
        prefix, self.pending = self.pending[:boundary], self.pending[boundary:]
        return self.pattern.sub(b'<redacted>', prefix)


@dataclass(frozen=True)
class ProcessResult:
    """Describe exit status, measured duration and stream warnings."""

    status: str
    exit_code: int | None
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)


def _signal_group(process: subprocess.Popen, number: int) -> None:
    """Signal only the owned process group if it still exists."""
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


class _Output:
    """Redact stream chunks and buffer native event lines independently."""

    def __init__(
        self,
        output: BinaryIO,
        secrets: list[str],
        callback: Callable[[str], None] | None,
        routing: Callable[[str, str], None] | None = None,
        log_stdout: bool = True,
    ) -> None:
        """Initialize per-stream redactors and callback diagnostics."""
        self.output = output
        self.redactors = {name: Redactor(secrets) for name in ('stdout', 'stderr')}
        self.line_buffer = b''
        self.routing = routing
        self.routed_buffers = {'stdout': b'', 'stderr': b''}
        self.callback = callback
        self.log_stdout = log_stdout
        self.warnings: list[str] = []

    def deliver(self, name: str, content: bytes, *, final: bool = False) -> None:
        """Log sanitized bytes and deliver complete stdout event lines."""
        safe = self.redactors[name].feed(content, final=final)
        if self.routing is not None:
            self.routed_buffers[name] += safe
            while b'\n' in self.routed_buffers[name]:
                line, self.routed_buffers[name] = self.routed_buffers[name].split(b'\n', 1)
                self._route(name, line)
            if final and self.routed_buffers[name]:
                self._route(name, self.routed_buffers[name])
                self.routed_buffers[name] = b''
            return
        if name != 'stdout' or self.log_stdout:
            self.output.write(safe)
            self.output.flush()
        if name != 'stdout' or self.callback is None:
            return
        self.line_buffer += safe
        while b'\n' in self.line_buffer:
            line, self.line_buffer = self.line_buffer.split(b'\n', 1)
            try:
                self.callback(line.decode('utf-8', errors='replace'))
            except (BenchmarkError, ValueError, TypeError, KeyError, OSError, RuntimeError):
                self.warnings.append('event_callback_failed')
                self.callback = None
                self.line_buffer = b''
                break

    def _route(self, name: str, line: bytes) -> None:
        """Routing failure is explicit and never disables subsequent native lines."""
        assert self.routing is not None
        try:
            self.routing(name, line.decode('utf-8', errors='replace'))
        except Exception:
            if 'output_callback_failed' not in self.warnings:
                self.warnings.append('output_callback_failed')


def _finish_output(streams: _Output, warnings: list[str]) -> None:
    """Deliver a final unterminated event without losing callback warnings."""
    if streams.line_buffer and streams.callback is not None:
        try:
            streams.callback(streams.line_buffer.decode('utf-8', errors='replace'))
        except (BenchmarkError, ValueError, TypeError, KeyError, OSError, RuntimeError):
            warnings.append('event_callback_failed')
    warnings.extend(streams.warnings)


def run_process(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    grace: float,
    cancel: threading.Event,
    log: Path,
    secrets: list[str] | None = None,
    stdin: bytes = b'',
    on_stdout: Callable[[str], None] | None = None,
    on_start: Callable[[], None] | None = None,
    on_output: Callable[[str, str], None] | None = None,
    log_stdout: bool = True,
) -> ProcessResult:
    """Preserve real return codes and partial files; cancellation is not a timeout."""
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    if cancel.is_set():
        log.write_bytes(b'')
        return ProcessResult('cancelled', None, 0.0)
    warnings = []
    process: subprocess.Popen | None = None
    with tempfile.TemporaryFile() as input_file, log.open('wb') as output, selectors.DefaultSelector() as selector:
        input_file.write(stdin)
        input_file.seek(0)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                umask=0o077,
            )
        except (OSError, ValueError) as exc:
            raise BenchmarkError(
                'process_start_failed', 'Cannot start the task process; check runtime and setup paths.', exit_code=1
            ) from exc
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        streams = _Output(output, secrets or [], on_stdout, on_output, log_stdout)
        deliver = streams.deliver
        reason = None
        kill_at = None
        exit_seen_at = None
        deadline = started + timeout
        try:
            if on_start is not None:
                on_start()
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if reason is None and cancel.is_set():
                    reason = 'cancelled'
                    kill_at = now + grace
                    _signal_group(process, signal.SIGTERM)
                elif reason is None and now >= deadline:
                    reason = 'timeout'
                    kill_at = now + grace
                    _signal_group(process, signal.SIGTERM)
                if kill_at is not None and now >= kill_at:
                    _signal_group(process, signal.SIGKILL)
                    kill_at = None
                for key, _mask in selector.select(timeout=0.05):
                    content = os.read(key.fd, 65536)
                    if content:
                        deliver(key.data, content)
                    else:
                        deliver(key.data, b'', final=True)
                        selector.unregister(key.fileobj)
                        cast(BinaryIO, key.fileobj).close()
                if process.poll() is not None:
                    # A setup phase leaves installed files, never unowned background processes.
                    _signal_group(process, signal.SIGKILL)
                    if exit_seen_at is None:
                        exit_seen_at = time.monotonic()
                    elif time.monotonic() - exit_seen_at >= max(grace, 0.1):
                        for key in list(selector.get_map().values()):
                            deliver(key.data, b'', final=True)
                            selector.unregister(key.fileobj)
                            cast(BinaryIO, key.fileobj).close()
                        warnings.append('output_pipe_not_closed')
                    if selector.get_map() and kill_at is None:
                        kill_at = time.monotonic() + grace
            exit_code = process.wait()
        finally:
            if process.poll() is None:
                _signal_group(process, signal.SIGKILL)
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        _finish_output(streams, warnings)
        status = reason or ('completed' if exit_code == 0 else 'error')
        return ProcessResult(status, exit_code, time.monotonic() - started, warnings)
