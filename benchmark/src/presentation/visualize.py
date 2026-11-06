"""Run Report pages with the same selection and measurements as the viewer."""

import subprocess
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.models.report import Report
from benchmark.persistence.storage import atomic_write, run_lock
from benchmark.presentation.report import read_report
from benchmark.presentation.run_page import render
from benchmark.presentation.visualize_data import embedded_data
from benchmark.runtime.commands import Command
from benchmark.validation.basket import Basket, load_basket


@contextmanager
def _view_command(run_dir: Path, kind: str, level: str) -> Iterator[Command]:
    """Own a view invocation and its safe diagnostic lifecycle."""
    with run_lock(run_dir):
        command = Command(run_dir, kind, level=level)
        try:
            yield command
            command.finish('completed')
        except OPERATION_ERRORS as error:
            command.diagnostic(error)
            command.finish('failed')
            raise
        finally:
            command.close()


def render_page(report: Report, basket: Basket, data: dict[str, Any] | None = None) -> bytes:
    """Render validated snapshots without reading or changing the saved run."""
    meta = basket.data.meta
    generation = next((row.settings for row in reversed(meta.lineage or []) if row.stage == 'generate'), {})
    source = generation.get('source') if generation.get('source') in ('issue', 'llm') else None
    context = (
        generation.get('context')
        if source == 'llm' and generation.get('context') in ('diff', 'issue', 'diff+issue')
        else None
    )
    return render(
        report,
        {'name': meta.name, 'description': meta.description, 'task_source': {'source': source, 'context': context}},
        dict(data or {}),
    )


def _open_browser(path: Path) -> None:
    """Report browser controller failures without discarding the saved HTML."""
    message = f'Could not open a browser. HTML is saved at {path}; open this file manually.'
    try:
        opened = webbrowser.open(path.as_uri())
    except (webbrowser.Error, OSError, subprocess.SubprocessError) as error:
        raise BenchmarkError('browser_open_failed', message, exit_code=1) from error
    if not opened:
        raise BenchmarkError('browser_open_failed', message, exit_code=1)


def visualize(run_dir: Path, *, open_browser: bool = False, level: str = 'INFO') -> Path:
    """Write an offline page for one canonical run report."""
    run_dir = run_dir.expanduser().resolve()
    with _view_command(run_dir, 'visualize', level):
        report = read_report(run_dir)
        basket = load_basket(run_dir / 'basket.json')
        path = run_dir / 'report.html'
        atomic_write(
            path,
            render_page(report, basket, embedded_data(run_dir, report)),
        )
        if open_browser:
            _open_browser(path)
    return path
