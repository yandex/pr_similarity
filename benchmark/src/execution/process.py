"""Bounded process lifecycle and redacted streaming output for task phases."""

import os
import re
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, BinaryIO, Literal

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

    status: Literal['completed', 'timeout', 'error', 'cancelled']
    exit_code: int | None
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)


def _signal_group(process: subprocess.Popen[bytes], number: int) -> None:
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


def _start_process(argv: list[str], cwd: Path, env: dict[str, str], input_file: BinaryIO) -> subprocess.Popen[bytes]:
    """Start an owned process group with private files and captured output."""
    try:
        return subprocess.Popen(
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


def _wait_process(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    readers: Mapping[int, IO[bytes]],
    streams: _Output,
    cancel: threading.Event,
    deadline: float,
    grace: float,
    warnings: list[str],
) -> tuple[Literal['timeout', 'cancelled'] | None, int]:
    """Drain owned pipes while enforcing cancellation, timeout and descendant cleanup."""
    deliver = streams.deliver
    reason: Literal['timeout', 'cancelled'] | None = None
    kill_at = None
    exit_seen_at = None
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
                readers[key.fd].close()
        if process.poll() is not None:
            # A setup phase leaves installed files, never unowned background processes.
            _signal_group(process, signal.SIGKILL)
            if exit_seen_at is None:
                exit_seen_at = time.monotonic()
            elif time.monotonic() - exit_seen_at >= max(grace, 0.1):
                for key in list(selector.get_map().values()):
                    deliver(key.data, b'', final=True)
                    selector.unregister(key.fileobj)
                    readers[key.fd].close()
                warnings.append('output_pipe_not_closed')
            if selector.get_map() and kill_at is None:
                kill_at = time.monotonic() + grace
    exit_code = process.wait()
    return reason, exit_code


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
    """Run an owned process group and preserve partial, redacted output.

    Args:
        argv: Executable and arguments, passed without a shell.
        env: Complete child environment.
        cwd: Child working directory.
        timeout: Seconds from function entry until termination is requested.
        grace: Seconds between SIGTERM and SIGKILL after timeout or cancellation.
        cancel: Event requesting cancellation; a preset event prevents launch.
        log: Output file, truncated on entry; parent directories are created.
        secrets: Literal values to redact across stream chunk boundaries.
        stdin: Bytes supplied through a temporary file as child standard input.
        on_stdout: Callback for decoded, redacted stdout lines without newlines.
            Expected callback errors disable it and add a result warning.
        on_start: Callback invoked after launch; errors propagate after cleanup.
        on_output: Optional callback receiving stream name and sanitized line.
            Replaces log and on_stdout delivery; failures add warnings and routing
            continues, including final unterminated lines.
        log_stdout: Include stdout in the log when on_output is absent.

    Returns:
        Exit code, elapsed seconds and warnings, with status completed, error,
        timeout or cancelled. Pre-launch cancellation has no exit code. Child
        processes are terminated before returning; partial logs remain available.

    Raises:
        BenchmarkError: If the process cannot be started.
        OSError: If temporary input, logs or process streams cannot be accessed.
        Exception: Errors from on_start propagate after terminating the child.

    """
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    if cancel.is_set():
        log.write_bytes(b'')
        return ProcessResult('cancelled', None, 0.0)
    warnings = []
    process: subprocess.Popen[bytes] | None = None
    with tempfile.TemporaryFile() as input_file, log.open('wb') as output, selectors.DefaultSelector() as selector:
        input_file.write(stdin)
        input_file.seek(0)
        process = _start_process(argv, cwd, env, input_file)
        assert process.stdout is not None and process.stderr is not None
        readers = {pipe.fileno(): pipe for pipe in (process.stdout, process.stderr)}
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        streams = _Output(output, secrets or [], on_stdout, on_output, log_stdout)
        try:
            if on_start is not None:
                on_start()
            reason, exit_code = _wait_process(
                process, selector, readers, streams, cancel, started + timeout, grace, warnings
            )
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
