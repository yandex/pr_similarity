"""Console diagnostics are friendly; tracebacks and secrets stay in DEBUG files."""

import io
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from benchmark import commands
from benchmark.commands import Command
from benchmark.runner import Stop
from benchmark.storage import read_json


def test_warning_at_error_threshold_trace_only_in_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify warning at error threshold trace only in file."""
    command = Command(tmp_path, 'rescore', level='ERROR', secrets=['PRIVATE_VALUE'])
    try:
        try:
            raise RuntimeError('PRIVATE_VALUE full diagnostic')
        except RuntimeError as error:
            command.diagnostic(error)
        command.message('Readable warning; check the file log.', level='WARNING')
    finally:
        command.finish('completed')
    console = capsys.readouterr().err
    assert 'Readable warning' in console
    assert 'Traceback' not in console and 'PRIVATE_VALUE' not in console
    logs = (command.logs_dir / 'benchmark.log').read_text()
    assert 'Traceback' in logs and 'PRIVATE_VALUE' not in logs and '<redacted>' in logs


def test_log_write_failure_does_not_expose_traceback_or_raw_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify log write failure does not expose traceback or raw secret."""

    class Broken(io.StringIO):
        """Simulate a log stream that raises an I/O error on every write."""

        def write(self, value: str) -> int:
            """Raise the fixture write failure before any data reaches the stream."""
            raise OSError('fixture disk failure')

    command = Command(tmp_path, 'fixture', level='ERROR', secrets=['fixture_hidden_secret'])
    assert command.handler.stream is not None
    command.handler.stream.close()
    monkeypatch.setattr(command.handler, 'stream', Broken())
    try:
        logging.getLogger('fixture').debug('private details fixture_hidden_secret')
        logging.getLogger('fixture').debug('another failure')
        output = capsys.readouterr().err
        assert output.count('DEBUG file log could not be written') == 1
        assert 'Traceback' not in output and 'fixture_hidden_secret' not in output
        assert 'private details' not in output
    finally:
        command.close()


def test_explicit_invocation_id_owns_new_metadata_layout(tmp_path: Path) -> None:
    """A coordinator-frozen UUID is the exact directory and metadata identity."""
    identifier = 'aa86d222-b728-4592-b8fd-15e83c10d2d9'
    command = Command(tmp_path, 'run', invocation_id=identifier, config={'safe': 'fixture'})
    command.finish('completed')
    expected = tmp_path / '_meta/invocations' / identifier
    assert command.id == identifier and command.logs_dir == expected
    assert command.path == expected / 'invocation.json'
    assert read_json(command.path)['config_snapshot'] == f'_meta/invocations/{identifier}/config.json'
    assert read_json(expected / 'config.json') == {'safe': 'fixture'}
    assert (expected / 'benchmark.log').exists()
    assert not (tmp_path / 'invocations').exists()
    assert not list(tmp_path.glob('logs_*'))


def test_repeated_interrupt_notice_while_coordinator_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observer can print a repeat notice at ERROR level without polling the scheduler."""
    noticed = threading.Event()

    class Output(io.StringIO):
        """Observe the mandatory notice using an event instead of a timing sleep."""

        def write(self, value: str) -> int:
            """Expose the mandatory shutdown notice to the waiting test."""
            size = super().write(value)
            if 'Shutdown is already in progress' in value:
                noticed.set()
            return size

    output = Output()
    monkeypatch.setattr(commands.sys, 'stderr', output)
    stop = Stop()
    command = Command(tmp_path, 'run', level='ERROR')
    command.watch_interrupts(lambda: stop.interrupt_count)
    try:
        stop.signal(signal.SIGINT, None)
        stop.signal(signal.SIGINT, None)
        assert noticed.wait(2)
        assert 'Please wait.' in output.getvalue()
        assert stop.signal_number == signal.SIGINT
    finally:
        command.close()
    assert command._observer is not None and not command._observer.is_alive()


def test_blocked_renderer_cannot_hold_coordinator_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled stderr sink may lose display output, but cannot hold the run writer."""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Blocked(io.StringIO):
        """Hold terminal output until the test releases the renderer."""

        def write(self, value: str) -> int:
            """Block the renderer without acquiring scheduler or storage locks."""
            entered.set()
            assert release.wait(5)
            return super().write(value)

    monkeypatch.setattr(commands.sys, 'stderr', Blocked())
    command = Command(tmp_path, 'run', progress_mode='plain', progress_total=1)
    command.watch_interrupts(lambda: 0)
    closer = None
    try:
        assert entered.wait(2)

        def close() -> None:
            """Verify final persistence completes while output remains blocked."""
            command.finish('completed')
            finished.set()

        closer = threading.Thread(target=close)
        closer.start()
        assert finished.wait(1.5)
    finally:
        release.set()
        if closer is not None:
            closer.join(timeout=3)
        command.close()
        if command._observer is not None:
            command._observer.join(timeout=3)


def test_blocked_debug_stream_cannot_hold_command_or_logging_shutdown(tmp_path: Path) -> None:
    """Heartbeat logging must not hold the command or Python's atexit logging shutdown."""
    script = textwrap.dedent('''
        import io
        import logging
        import sys
        import threading
        import time
        from pathlib import Path

        from benchmark.commands import Command


        class Blocked(io.StringIO):
            def write(self, value: str) -> int:
                threading.Event().wait(30)
                return len(value)


        root = Path(sys.argv[1])
        command = Command(root, 'run', secrets=['fixture-secret'], progress_mode='plain', progress_total=1)
        command.handler.stream = Blocked()
        command.phase('fixture-secret')
        command.watch_interrupts(lambda: 0)
        deadline = time.monotonic() + 2
        log = command.logs_dir / 'benchmark.log'
        while time.monotonic() < deadline and '<redacted>' not in log.read_text():
            time.sleep(0.01)
        assert '<redacted>' in log.read_text()
        command.finish('completed')
        logging.shutdown()
        print('finished', flush=True)
        ''')
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path)],
        cwd=Path(__file__).parents[1],
        env={**os.environ, 'PYTHONHASHSEED': '42'},
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0 and result.stdout == 'finished\n'


def test_task_facade_normalizes_observer_and_writes_safe_human_log(tmp_path: Path) -> None:
    """Worker observations remain presentation and carry no opaque IDs into human logs."""
    observed = []
    command = Command(
        tmp_path,
        'run',
        secrets=['fixture-secret'],
        progress_mode='off',
        progress_total=100,
        phase_observer=lambda task_id, phase: observed.append((task_id, phase)),
    )
    try:
        command.task_start('opaque/task/hash', ordinal=3, attempt_sequence=1)
        command.phase('git', 'opaque/task/hash')
        command.phase('agent', 'opaque/task/hash')
        command.task_phase('opaque/task/hash', 'capture')
        command.task_phase('opaque/task/hash', 'scoring-recovery', attempt_sequence=2)
        command.task_finish('opaque/task/hash', execution='timeout', scoring='scored')
        command.message('fixture-secret\x1b[31m readable notice', level='WARNING')
    finally:
        command.finish('completed')
    assert observed[-3:] == [
        ('opaque/task/hash', 'running'),
        ('opaque/task/hash', 'saving'),
        ('opaque/task/hash', 'recovering score'),
    ]
    human = (tmp_path / 'run.log').read_text()
    assert 'Task 003/100' in human and 'attempt 2' in human and 'timeout' in human and 'scored' in human
    assert 'opaque/task/hash' not in human and 'fixture-secret' not in human and '\x1b' not in human
    assert '<redacted>' in human and 'readable notice' in human


def test_human_log_failure_and_observer_failure_do_not_stop_lifecycle(tmp_path: Path) -> None:
    """Presentation errors cannot prevent invocation state or later task outcomes."""
    (tmp_path / 'run.log').mkdir()

    def broken(task_id: str, phase: str) -> None:
        """Simulate a full observer queue without involving the scheduler."""
        raise RuntimeError('fixture queue full')

    command = Command(tmp_path, 'run', phase_observer=broken)
    command.task_start('opaque', ordinal=1)
    command.task_phase('opaque', 'agent')
    command.task_finish('opaque', execution='completed', scoring='scored')
    command.message('Still visible', level='WARNING')
    command.finish('completed')
    assert read_json(command.path)['status'] == 'completed'
    assert 'run_log_failed' in command.presentation_warnings
    assert 'phase_observer_failed' in command.presentation_warnings


def test_config_snapshot_and_dynamic_secrets_are_redacted(tmp_path: Path) -> None:
    """Snapshot redaction handles decoded JSON strings and later console credentials."""
    secret = 'quoted"secret'
    command = Command(tmp_path, 'run', config={'nested': [{'key': secret}]}, secrets=[secret])
    try:
        command.add_secret('later-secret')
        command.message('later-secret ' + secret, level='WARNING')
    finally:
        command.finish('completed')
    assert read_json(command.logs_dir / 'config.json') == {'nested': [{'key': '<redacted>'}]}
    for path in (command.logs_dir / 'benchmark.log', tmp_path / 'run.log'):
        content = path.read_text()
        assert secret not in content and 'later-secret' not in content


def test_deferred_debug_file_open_failure_is_presentation_only(tmp_path: Path) -> None:
    """Unavailable optional diagnostics do not prevent durable invocation metadata."""
    identifier = 'cc86d222-b728-4592-b8fd-15e83c10d2d9'
    directory = tmp_path / '_meta/invocations' / identifier
    command = Command(tmp_path, 'run', invocation_id=identifier)
    command.handler.close()
    (directory / 'benchmark.log').unlink()
    (directory / 'benchmark.log').mkdir()
    command.handler.stream = None
    command.message('Runtime still progresses', level='WARNING')
    command.finish('completed')
    assert read_json(command.path)['status'] == 'completed'
    assert 'Runtime still progresses' in (tmp_path / 'run.log').read_text()
