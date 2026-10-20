"""Progress describes work without altering stage artifacts or business outcomes."""

import io

import pytest
from basket_collector.progress import Progress


def test_unknown_discovery_total_and_heartbeat() -> None:
    """Show pages and matches while an unknown-sized discovery is waiting."""
    stream = io.StringIO()
    now = [0.0]
    progress = Progress('plain', stream=stream, clock=lambda: now[0])
    progress.start('collect', 'discovery', mode='resumed')
    progress.update(n=39, matched=3786, next_page=40)
    now[0] = 5.1
    progress.poll()
    lines = stream.getvalue()
    assert 'collect discovery running mode=resumed' in lines
    assert 'pages=39' in lines and 'matched=3786' in lines and 'next_page=40' in lines
    assert '%' not in lines and '\x1b' not in lines and '\r' not in lines


def test_short_basket_keeps_requested_total() -> None:
    """Candidate exhaustion leaves a final 8/10 instead of a synthetic 100%."""
    stream = io.StringIO()
    progress = Progress('plain', stream=stream)
    progress.basket(10)
    progress.basket_update(available=8, successful=8, failed=0)
    progress.finish_basket('completed')
    assert 'tasks=8/10' in stream.getvalue()
    assert 'candidate_shortfall=2' in stream.getvalue()
    assert 'failed=0' in stream.getvalue()


def test_failed_tasks_are_distinct_from_candidate_shortfall() -> None:
    """A failed task is not counted as another exhausted candidate."""
    stream = io.StringIO()
    progress = Progress('plain', stream=stream)
    progress.basket(10)
    progress.basket_update(available=8, successful=7, failed=1)
    progress.finish_basket('completed')
    assert 'tasks=7/10' in stream.getvalue()
    assert 'candidate_shortfall=2' in stream.getvalue() and 'failed=1' in stream.getvalue()


def test_off_and_warning_levels_suppress_intermediate_output() -> None:
    """Keep progress settings independent from mandatory warning messages."""
    for mode, level in [('off', 'INFO'), ('plain', 'WARNING')]:
        stream = io.StringIO()
        progress = Progress(mode, level=level, stream=stream)
        progress.start('collect', 'records')
        progress.update(n=1, total=2)
        progress.finish('completed')
        assert stream.getvalue() == ''
        progress.write_message('Warning: fixture', stream=stream)
        assert stream.getvalue() == 'Warning: fixture\n'


def test_renderer_failure_disables_only_progress() -> None:
    """An unavailable terminal cannot escape into the pipeline."""

    class Broken(io.StringIO):
        """Simulate a closed pipe without sending anything to real stderr."""

        def write(self, text: str) -> int:
            """Fail the display operation."""
            raise BrokenPipeError()

    progress = Progress('plain', stream=Broken())
    progress.start('collect', 'records')
    progress.update(n=1, total=2)
    progress.finish('interrupted')
    progress.close()
    assert progress.disabled


def test_auto_without_tty_uses_plain_and_redacts() -> None:
    """Redirected output stays readable and uses the invocation redactor."""
    stream = io.StringIO()
    snapshots = []
    progress = Progress(
        'auto', stream=stream, redact=lambda text: text.replace('fixture-secret', '<redacted>'), log=snapshots.append
    )
    progress.start('collect', 'discovery')
    progress.update(wait='fixture-secret')
    progress.finish('interrupted')
    assert 'fixture-secret' not in stream.getvalue() and '\r' not in stream.getvalue()
    assert all('fixture-secret' not in line for line in snapshots)


def test_failed_panel_writer_preserves_mandatory_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A display failure must still print the first warning to a healthy stream."""

    class Terminal(io.StringIO):
        """Select TTY mode without a real terminal or external requests."""

        def isatty(self) -> bool:
            """Expose a healthy interactive stream."""
            return True

    def broken_writer(*args: object, **kwargs: object) -> None:
        """Fail panel redraw while the underlying stream remains writable."""
        raise BrokenPipeError('fixture renderer failure')

    stream = Terminal()
    progress = Progress('auto', stream=stream)
    progress.start('collect', 'records')
    monkeypatch.setattr(progress, '_draw_frame', broken_writer)
    progress.write_message('MANDATORY_WARNING', stream=stream)
    progress.close()
    assert progress.disabled and 'MANDATORY_WARNING' in stream.getvalue()
