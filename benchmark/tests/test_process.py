"""Timeout/cancellation and streaming redaction are independent of agent correctness."""

import signal
import sys
import threading
from pathlib import Path

from benchmark.execution.process import run_process
from benchmark.runtime.runner import Stop


def test_machine_stdout_is_not_duplicated_in_diagnostic_log(tmp_path: Path) -> None:
    """Structured stdout reaches its consumer; stderr remains redacted diagnostics."""
    output = tmp_path / 'diagnostic.log'
    lines = []
    result = run_process(
        [sys.executable, '-c', 'import sys; print("{\\"ok\\":true}"); print("warning SECRET", file=sys.stderr)'],
        env={},
        cwd=tmp_path,
        timeout=5,
        grace=0.1,
        cancel=threading.Event(),
        log=output,
        secrets=['SECRET'],
        on_stdout=lines.append,
        log_stdout=False,
    )
    assert result.status == 'completed'
    assert lines == ['{"ok":true}']
    assert output.read_text() == 'warning <redacted>\n'


def test_exit_code_and_secret_redaction(tmp_path: Path) -> None:
    """Verify exit code and secret redaction."""
    output = tmp_path / 'agent.log'
    code = 'import os,sys; print(os.environ["KEY"]); print("safe"); sys.exit(7)'
    result = run_process(
        [sys.executable, '-c', code],
        env={'KEY': 'SECRET_ONLY_IN_ENV'},
        cwd=tmp_path,
        timeout=5,
        grace=0.1,
        cancel=threading.Event(),
        log=output,
        secrets=['SECRET_ONLY_IN_ENV'],
    )
    assert result.status == 'error' and result.exit_code == 7
    assert 'SECRET_ONLY_IN_ENV' not in output.read_text()
    assert '<redacted>' in output.read_text() and 'safe' in output.read_text()


def test_secret_split_across_stream_chunks(tmp_path: Path) -> None:
    """Verify secret split across stream chunks."""
    output = tmp_path / 'agent.log'
    code = 'import os,time; os.write(1,b"SECRET_"); time.sleep(.1); os.write(1,b"VALUE\\n")'
    run_process(
        [sys.executable, '-c', code],
        env={},
        cwd=tmp_path,
        timeout=5,
        grace=0.1,
        cancel=threading.Event(),
        log=output,
        secrets=['SECRET_VALUE'],
    )
    assert output.read_text().strip() == '<redacted>'


def test_timeout_preserves_written_partial_file(tmp_path: Path) -> None:
    """Verify timeout preserves written partial file."""
    result = run_process(
        [
            sys.executable,
            '-c',
            'from pathlib import Path; import time; Path("changed.py").write_text("partial"); time.sleep(10)',
        ],
        env={},
        cwd=tmp_path,
        # Allow interpreter startup and the write before interrupting the ten-second sleep.
        timeout=2,
        grace=0.1,
        cancel=threading.Event(),
        log=tmp_path / 'agent.log',
    )
    assert result.status == 'timeout'
    assert (tmp_path / 'changed.py').read_text() == 'partial'
    assert result.duration_seconds < 5


def test_cancel_is_not_timeout(tmp_path: Path) -> None:
    """Verify cancel is not timeout."""
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    try:
        result = run_process(
            [sys.executable, '-c', 'import time;time.sleep(10)'],
            env={},
            cwd=tmp_path,
            timeout=5,
            grace=0.1,
            cancel=stop,
            log=tmp_path / 'agent.log',
        )
        assert result.status == 'cancelled'
    finally:
        timer.join()


def test_second_interrupt_keeps_configured_grace_for_process_ignoring_term(tmp_path: Path) -> None:
    """Repeated user interrupts do not replace the code-owned termination deadline."""
    stop = Stop()
    timers = []

    def ready(line: str) -> None:
        """Send two interrupts only after the TERM-resistant child reports readiness."""
        if line == 'ready':
            stop.signal(signal.SIGINT, None)
            timer = threading.Timer(0.05, lambda: stop.signal(signal.SIGINT, None))
            timers.append(timer)
            timer.start()

    try:
        result = run_process(
            [
                sys.executable,
                '-c',
                (
                    'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                    'print("ready",flush=True); time.sleep(10)'
                ),
            ],
            env={},
            cwd=tmp_path,
            timeout=10,
            grace=0.4,
            cancel=stop,
            log=tmp_path / 'process.log',
            on_stdout=ready,
        )
        assert result.status == 'cancelled' and result.exit_code == -signal.SIGKILL
        assert 0.35 <= result.duration_seconds < 2
        assert stop.signal_number == signal.SIGINT
    finally:
        for timer in timers:
            timer.join()


def test_first_user_signal_after_fatal_stop_still_records_interrupt() -> None:
    """A credentials stop is not itself a previously delivered user signal."""
    stop = Stop()
    stop.set()
    stop.signal(signal.SIGINT, None)
    stop.signal(signal.SIGTERM, None)
    assert stop.signal_number == signal.SIGINT
    assert not getattr(stop, 'force', False)


def test_cancelled_before_process_start_still_has_owned_log(tmp_path: Path) -> None:
    """All referenced attempt logs exist even if cancellation wins before Popen."""
    stop = Stop()
    stop.set()
    log = tmp_path / 'attempt' / 'agent.log'
    launched = []
    result = run_process(
        ['/definitely/missing/executable'],
        env={},
        cwd=tmp_path,
        timeout=1,
        grace=0.1,
        cancel=stop,
        log=log,
        on_start=lambda: launched.append(True),
    )
    assert result.status == 'cancelled' and log.read_bytes() == b''
    assert launched == []


def test_signal_handler_never_acquires_event_condition_lock(tmp_path: Path) -> None:
    """A signal during Event.set must not deadlock the main Python thread."""
    result = run_process(
        [
            sys.executable,
            '-c',
            (
                'import signal; from benchmark.runtime.runner import Stop; s=Stop(); '
                's._cond.acquire(); s.signal(signal.SIGINT,None); s._cond.release(); '
                'assert s.is_set(); assert s.observed_interrupts()==1'
            ),
        ],
        env={'PATH': '/usr/bin:/bin', 'PYTHONHASHSEED': '42'},
        cwd=tmp_path,
        timeout=3,
        grace=0.1,
        cancel=threading.Event(),
        log=tmp_path / 'signal.log',
    )
    assert result.status == 'completed'


def test_native_routing_redacts_all_streams_and_keeps_later_callbacks(tmp_path: Path) -> None:
    """An optional native route preserves stream identity without duplicating mixed JSON."""
    delivered = []

    def route(stream: str, line: str) -> None:
        """Fail once and verify later stream callbacks continue unchanged."""
        delivered.append((stream, line))
        if line == 'first':
            raise ValueError('fixture presentation failure')

    result = run_process(
        [
            sys.executable,
            '-c',
            'import os; os.write(1,b"first\\nSECRET_VALUE\\nlast"); os.write(2,b"stderr SECRET_VALUE")',
        ],
        env={},
        cwd=tmp_path,
        timeout=5,
        grace=0.1,
        cancel=threading.Event(),
        log=tmp_path / 'runtime.log',
        secrets=['SECRET_VALUE'],
        on_output=route,
    )
    assert ('stdout', 'last') in delivered
    assert ('stderr', 'stderr <redacted>') in delivered
    assert ('stdout', '<redacted>') in delivered
    assert result.warnings == ['output_callback_failed']
    assert (tmp_path / 'runtime.log').read_bytes() == b''
