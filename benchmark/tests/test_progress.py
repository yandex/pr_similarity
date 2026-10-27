"""Progress is an observer of durable state, with bounded terminal output."""

import fcntl
import io
import os
import pty
import select
import struct
import termios
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_runner import config_and_basket, fake_attempt, provision_fixture

from benchmark.errors import BenchmarkError
from benchmark.presentation import progress as progress_module
from benchmark.presentation.progress import Progress
from benchmark.runtime import coordinator
from benchmark.runtime.runner import run


def test_plain_heartbeat_uses_frozen_total_and_saved_completed_count() -> None:
    """Retries and worker phase changes do not count as completed tasks."""
    clock = [0.0]
    output = io.StringIO()
    logs = []
    progress = Progress('plain', total=10, clock=lambda: clock[0], stream=output, log=logs.append)
    progress.counts(completed=3, active=2, retries=4, errors=1)
    progress.phase('agent', task_id='first')
    progress.phase('git', task_id='second')
    progress.poll()
    assert 'completed=3/10' in output.getvalue()
    assert 'agent=1' in output.getvalue() and 'git=1' in output.getvalue()
    progress.poll()
    assert len(output.getvalue().splitlines()) == 1
    clock[0] = 5.0
    progress.poll()
    assert len(output.getvalue().splitlines()) == 2
    assert 'elapsed=5s' in output.getvalue()
    assert len(logs) == 2
    progress.finish('interrupted')
    assert 'interrupted' in output.getvalue() and 'completed=3/10' in output.getvalue()


def test_plain_worker_transition_is_not_delayed_until_heartbeat() -> None:
    """Meaningful transitions render immediately while idle polling stays bounded."""
    clock = [0.0]
    output = io.StringIO()
    progress = Progress('plain', total=1, stream=output, clock=lambda: clock[0])
    progress.poll()
    clock[0] = 0.1
    progress.phase('agent', task_id='fixture')
    progress.poll()
    assert len(output.getvalue().splitlines()) == 2
    assert 'agent=1' in output.getvalue()


@pytest.mark.parametrize(('mode', 'level'), [('off', 'INFO'), ('plain', 'ERROR')])
def test_hidden_progress_does_not_hide_warning_or_file_snapshot(mode: str, level: str) -> None:
    """Visibility settings suppress only progress while preserving mandatory diagnostics."""
    output = io.StringIO()
    logs = []
    progress = Progress(mode, total=1, level=level, stream=output, log=logs.append)
    progress.poll()
    assert output.getvalue() == '' and len(logs) == 1
    progress.write_message('Readable warning')
    assert output.getvalue() == 'Readable warning\n'


def test_broken_stream_and_redaction_do_not_change_run_state() -> None:
    """Rendering is disposable and never controls the number of completed tasks."""

    class Broken(io.StringIO):
        """Simulate a detached or broken terminal."""

        def write(self, value: str) -> int:
            """Reject output as a broken pipe would."""
            raise OSError('fixture broken pipe')

    progress = Progress('plain', total=2, stream=Broken())
    progress.counts(completed=1, active=0, retries=0, errors=0)
    progress.poll()
    progress.finish('completed')
    output = io.StringIO()
    safe = Progress('plain', total=1, stream=output, redact=lambda s: s.replace('fixture-secret', '<redacted>'))
    safe.write_message('fixture-secret\x1b\nprovider payload')
    assert 'fixture-secret' not in output.getvalue() and '\x1b' not in output.getvalue()


def test_tty_shows_worker_phases_and_keeps_actual_count_on_interrupt() -> None:
    """A terminal bar shows actual work and does not invent 100% on cancellation."""

    class Terminal(io.StringIO):
        """Expose deterministic terminal capability for observer assertions."""

        def isatty(self) -> bool:
            """Report interactive output without a physical terminal."""
            return True

    output = Terminal()
    progress = Progress('auto', total=10, stream=output)
    progress.counts(completed=2, active=1, retries=1, errors=0)
    progress.phase('agent', task_id='fixture')
    progress.poll()
    assert 'agent:1 2/10' in output.getvalue()
    assert '0/10' not in output.getvalue()
    progress.finish('interrupted')
    assert 'completed=2/10' in output.getvalue()
    assert 'completed=10/10' not in output.getvalue()


def test_tty_reserves_80_columns_for_counts_before_phase_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typical terminal shows total, elapsed and every counter during long phases."""

    class Terminal(io.StringIO):
        """Expose deterministic terminal capability for observer assertions."""

        def isatty(self) -> bool:
            """Report interactive output without a physical terminal."""
            return True

    monkeypatch.setattr(progress_module.shutil, 'get_terminal_size', lambda **_kwargs: os.terminal_size((80, 24)))
    output = Terminal()
    progress = Progress('auto', total=1000, stream=output, resumed=True)
    progress.counts(completed=200, active=4, retries=20, errors=3)
    progress.phase('provisioning/cache-wait')
    progress.poll()
    line = output.getvalue().split('\r')[-1]
    assert len(line) <= 80
    assert all(value in line for value in ('resume', '200/1000', '00:00', 'active=4', 'retries=20', 'errors=3'))
    progress.finish('interrupted')


def test_pricing_validation_before_allocating_run(tmp_path: Path) -> None:
    """A wrong pricing path must not leave a UUID that looks like a saved run."""
    config, basket = config_and_basket(tmp_path)
    output = tmp_path / 'runs'
    with pytest.raises(BenchmarkError, match='pricing'):
        run(config, basket, output=output, pricing=tmp_path / 'missing-pricing.yaml')
    assert not output.exists() or not list(output.iterdir())


def test_resume_progress_starts_at_saved_completed_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A completed resume reports reuse and does not create a new agent attempt."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    assert code == 0
    capsys.readouterr()
    assert run(config, basket, resume=directory, progress='plain', attempt_function=fake_attempt([]))[0] == 0
    assert 'completed=1/1' in capsys.readouterr().err
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1


def test_empty_owned_allocation_is_cleaned_after_early_lock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initialization failure cleans only the UUID allocated by this call."""
    config, basket = config_and_basket(tmp_path)
    output = tmp_path / 'runs'

    @contextmanager
    def broken_lock(_directory: Path) -> Iterator[None]:
        """Reject initialization before a task or native process exists."""
        raise BenchmarkError('lock_failed', 'Fixture lock failure.')
        yield

    monkeypatch.setattr(coordinator, 'run_lock', broken_lock)
    with pytest.raises(BenchmarkError, match='Fixture lock'):
        run(config, basket, output=output)
    assert not list(output.iterdir())


def test_task_row_appears_on_demand_and_finishes_once() -> None:
    """Retries update the same task identity and terminal history is frozen once."""
    output = io.StringIO()
    progress = Progress('plain', total=100, stream=output)
    progress.poll()
    assert 'Task ' not in output.getvalue()
    progress.task_start('opaque-secret-path', ordinal=3, attempt_sequence=1)
    progress.poll()
    progress.task_phase('opaque-secret-path', 'retry pending')
    progress.poll()
    progress.task_start('opaque-secret-path', ordinal=3, attempt_sequence=2)
    progress.poll()
    progress.task_finish('opaque-secret-path', execution='timeout', scoring='scored')
    progress.poll()
    progress.task_finish('opaque-secret-path', execution='timeout', scoring='scored')
    progress.poll()
    text = output.getvalue()
    assert 'opaque-secret-path' not in text
    assert 'Task 003/100 · attempt 2 · timeout · scored' in text
    assert text.count('timeout · scored') == 1


def test_real_pty_viewport_resize_hidden_count_and_frozen_history() -> None:
    """A real terminal bounds active rows, survives resize, and emits one final row."""

    def read_terminal(fd: int) -> str:
        """Drain all currently queued PTY output including split kernel writes."""
        data = b''
        while select.select([fd], [], [], 0.05)[0]:
            data += os.read(fd, 65536)
        return data.decode()

    master, slave = pty.openpty()
    os.set_blocking(master, False)
    stream = os.fdopen(os.dup(slave), 'w', buffering=1)
    clock = [0.0]
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 6, 80, 0, 0))
        progress = Progress('auto', total=100, stream=stream, clock=lambda: clock[0])
        for ordinal in range(1, 6):
            progress.task_start(str(ordinal), ordinal=ordinal)
        progress.counts(completed=0, active=5, retries=0, errors=0)
        progress.poll()
        initial = read_terminal(master)
        assert '3 active tasks hidden' in initial
        assert 'Task 001/100' in initial and 'Task 005/100' not in initial
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 5, 24, 0, 0))
        clock[0] = 0.3
        progress.task_phase('1', 'running')
        progress.poll()
        resized = read_terminal(master)
        assert 'T001 a1 running' in resized
        assert '4 active tasks hidden' in resized
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 5, 80, 0, 0))
        clock[0] = 65.6
        progress.task_result('1', mf=0.12356, lcf=0.0)
        progress.task_finish('1', execution='timeout', scoring='scored')
        progress.poll()
        final = read_terminal(master)
        clock[0] = 70.0
        progress.task_finish('1', execution='timeout', scoring='scored')
        progress.poll()
        repeated = read_terminal(master)
        assert final.count('timeout') == 1 and 'timeout' not in repeated
        assert '01:05' in final and 'Mf=0.124 LCf=0.000' in final
        progress.close()
    finally:
        stream.close()
        os.close(master)
        os.close(slave)


def test_attempt_elapsed_is_independent_frozen_and_reset_on_retry() -> None:
    """Each attempt owns a clock; retries cannot inflate its final duration."""
    clock = [100.0]
    output = io.StringIO()
    progress = Progress('plain', total=2, stream=output, clock=lambda: clock[0])
    progress.task_start('first', ordinal=1)
    clock[0] = 110.0
    progress.task_start('second', ordinal=2)
    clock[0] = 175.0
    assert '01:15' in progress._task_line(progress.tasks['first'])
    assert '01:05' in progress._task_line(progress.tasks['second'])
    progress.task_result('first', mf=0.12356, lcf=0.0)
    progress.task_phase('first', 'retry pending')
    clock[0] = 200.0
    line = progress._task_line(progress.tasks['first'])
    assert '01:15' in line and 'Mf=0.124 LCf=0.000' in line
    progress.task_start('first', ordinal=1, attempt_sequence=1)
    assert '01:40' in progress._task_line(progress.tasks['first'])
    progress.task_start('first', ordinal=1, attempt_sequence=2)
    line = progress._task_line(progress.tasks['first'])
    assert '00:00' in line and 'Mf=' not in line
    clock[0] = 203.0
    progress.task_result('first', mf=1.0, lcf=0.9996)
    clock[0] = 250.0
    progress.task_finish('first', execution='completed', scoring='scored')
    progress.poll()
    final = [line for line in output.getvalue().splitlines() if 'completed · scored' in line][0]
    assert '00:03' in final and 'Mf=1.000 LCf=1.000' in final
    compact = progress._fit_task_message(final, 80)
    assert '00:03' in compact and 'Mf=1.000 LCf=1.000' in compact


def test_unavailable_scores_are_not_rendered_as_zero() -> None:
    """An unscored attempt freezes elapsed time without fabricated quality."""
    clock = [0.0]
    output = io.StringIO()
    progress = Progress('plain', total=1, stream=output, clock=lambda: clock[0])
    progress.task_start('task', ordinal=1)
    clock[0] = 12.0
    progress.task_result('task', mf=None, lcf=None)
    progress.task_finish('task', execution='error', scoring='unavailable')
    clock[0] = 100.0
    progress.poll()
    final = [line for line in output.getvalue().splitlines() if 'error · unavailable' in line][0]
    assert '00:12' in final and 'Mf=' not in final and 'LCf=' not in final


def test_saved_scores_reach_final_progress_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The coordinator forwards committed scores to the visible task outcome."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    code, _ = run(
        config, basket, output=tmp_path / 'runs', progress='plain', attempt_function=fake_attempt(['success'])
    )
    assert code == 0
    final = [line for line in capsys.readouterr().err.splitlines() if 'completed · scored' in line]
    assert any('Mf=1.000 LCf=1.000' in line for line in final)
