"""Run Report pages with the same selection and measurements as the viewer."""

import html
import json
import subprocess
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.json_types import JsonObject
from benchmark.models.report import (
    Report,
    ReportTask,
    ReportTelemetry,
    ReportTelemetryApiEquivalent,
    ReportTelemetryCost,
)
from benchmark.persistence.storage import atomic_write, run_lock
from benchmark.presentation.report import read_report
from benchmark.presentation.visualize_assets import SCRIPT, STYLE
from benchmark.presentation.visualize_data import embedded_data
from benchmark.presentation.visualize_dialogs import DIALOG_SCRIPT, DIALOG_STYLE, DIALOGS
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


def _text(value: object) -> str:
    """Escape untrusted values before embedding them in HTML."""
    return html.escape(str(value), quote=True)


def _number(value: float | None, places: int = 3) -> str:
    """Format a measured value or the unknown marker."""
    return '—' if value is None else format(value, f'.{places}f')


def _tokens(telemetry: ReportTelemetry) -> str:
    """Display independent token categories without replacing unknowns."""
    return ' / '.join(
        '—' if value is None else str(value)
        for value in (
            telemetry.tokens.input,
            telemetry.tokens.output,
            telemetry.tokens.cache_read,
            telemetry.tokens.cache_write,
        )
    )


def _cost(value: ReportTelemetryCost | ReportTelemetryApiEquivalent | None) -> str:
    """Format original or derived cost without conflating their sources."""
    if value is None:
        return '—'
    amount = _number(value.amount, 8)
    return amount + (
        ' (partial)' if isinstance(value, ReportTelemetryApiEquivalent) and value.status != 'complete' else ''
    )


def _page(title: str, intro: str, tasks: list[str], report: Report, data: JsonObject | None = None) -> bytes:
    """Build a self-contained run page using the viewer's visual components."""
    payload = json.dumps(report.to_wire(), ensure_ascii=True).replace('<', '\\u003c')
    details = json.dumps(data or {}, ensure_ascii=True).replace('<', '\\u003c')
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>' + _text(title) + '</title><style>' + STYLE + DIALOG_STYLE + '</style></head><body>'
        '<header class="topbar"><a class="brand" href="#overview">PR Similarity</a>'
        '<span class="muted">Run Report</span><nav><a href="#overview">Overview</a> '
        '<a href="#tasks">Tasks</a></nav></header><main>'
        '<section class="controls"><div class="selectors" id="selection">'
        '<fieldset><legend>Task selection</legend>'
        '<label><input type="checkbox" value="OK" checked>OK</label>'
        '<label><input type="checkbox" value="no-op" checked>no-op</label>'
        '<label><input type="checkbox" value="timeout" checked>timeout</label>'
        '<label><input type="checkbox" value="fail">fail</label></fieldset>'
        '<label>PR aggregation <select id="pr"><option>Mpr</option><option>FCpr</option>'
        '<option selected>LCpr</option></select></label>'
        '<label>File aggregation <select id="file"><option>Mf</option><option selected>LCf</option>'
        '</select></label></div></section>'
        '<section id="overview" class="run-header">'
        + intro
        + '<div id="stats" class="stat-grid"></div><p id="tokens" class="muted"></p>'
        '<p id="statuses" class="muted"></p></section><h2 id="tasks">Tasks</h2>'
        '<section class="controls task-controls"><label>Filter tasks '
        '<input id="search" type="search" placeholder="PR, status, path"></label>'
        '<label>Sort by <select id="task-sort"><option value="ordinal">Task order</option>'
        '<option value="score">Score</option><option value="time">Agent time</option>'
        '<option value="total_tokens">Total tokens</option></select></label>'
        '<label>Direction <select id="task-direction"><option value="asc">Ascending</option>'
        '<option value="desc">Descending</option></select></label>'
        '<button id="task-order-reset" type="button">Original task order</button>'
        '<span id="task-count" class="muted" aria-live="polite"></span></section>'
        '<p class="muted">— means unknown or unavailable. Scores use the final attempt.</p>'
        '<div id="task-list">'
        + ''.join(tasks)
        + '</div>'
        + DIALOGS
        + '<script type="application/json" id="view-data">'
        + details
        + '</script>'
        '<script type="application/json" id="report-data">'
        + payload
        + '</script><script>'
        + SCRIPT
        + DIALOG_SCRIPT
        + '</script></main></body></html>'
    )
    return document.encode('utf-8')


def _status(row: ReportTask) -> str:
    """Use the viewer's final-outcome categories independently of scoring."""
    if row.infrastructure_error or row.processing_status != 'completed':
        return 'fail'
    if row.execution.status == 'timeout':
        return 'timeout'
    if row.execution.status == 'completed':
        return {'empty': 'no-op', 'nonempty': 'OK'}.get(row.changes.status, 'fail')
    return 'fail'


def _files(row: ReportTask, available: bool) -> str:
    """Expose embedded file diffs as buttons without implying missing evidence exists."""
    return ''.join(
        '<button type="button" class="file" data-file-index="'
        + str(index)
        + '"'
        + ('' if available else ' disabled')
        + ' aria-label="View diff for '
        + _text(item.path)
        + '">'
        '<span class="file-path">'
        + _text(item.path)
        + '</span><span>'
        + _number(item.mlp)
        + '</span><span class="file-action">'
        + ('View diff' if available else 'Diff unavailable')
        + '</span></button>'
        for index, item in enumerate(row.scoring.files)
    )


def _task(row: ReportTask, index: int, *, has_diffs: bool = False) -> str:
    """Render an expandable task with its original PR/MR link and source diagnostics."""
    files = _files(row, has_diffs)
    parsed = urlsplit(row.url)
    url = row.url if parsed.scheme in ('https', 'http') and not parsed.username and not parsed.password else None
    caption = _text(f'{row.repository} #{row.number} · {row.title}')
    title = '<span class="title">' + caption + '</span>'
    label = 'Original MR' if row.kind == 'merge_request' else 'Original PR'
    source = (
        ''
        if url is None
        else (
            '<a class="task-source-link" target="_blank" rel="noopener noreferrer" href="'
            + _text(url)
            + '">'
            + label
            + '</a>'
        )
    )
    error = row.infrastructure_error
    diagnostic = '' if error is None else '<p class="error">' + _text(error.message) + '</p>'
    status = _status(row)
    attempt = '—' if row.final_attempt is None else str(row.final_attempt.sequence)
    counts = row.scoring.counts
    denominator = counts.human_files + counts.agent_files if counts is not None else 0
    f1 = (2 * counts.matched_files / denominator if denominator else 1.0) if counts is not None else None
    return (
        '<details class="task" data-index="' + str(index) + '"><summary>'
        '<span class="badge '
        + status
        + '">'
        + status
        + '</span>'
        + title
        + '<span class="task-score numeric">'
        + _number(row.scoring.LCf_mlp)
        + '</span>'
        '<span class="task-metrics">Total Tokens: '
        + _text(row.telemetry.tokens.total if row.telemetry.tokens.total is not None else '—')
        + ' · Agent Time: '
        + _number(row.execution.duration_seconds, 2)
        + ' s</span></summary>'
        '<div class="task-body">'
        + source
        + '<h3>Task condition</h3><pre class="condition">'
        + _text(row.agent_task)
        + '</pre>'
        + diagnostic
        + '<p class="muted">F1 files: '
        + _number(f1)
        + ' · Mf: '
        + _number(row.scoring.Mf_mlp)
        + ' · LCf: '
        + _number(row.scoring.LCf_mlp)
        + ' · Attempt: '
        + attempt
        + ' · Workspace: '
        + _text(row.workspace)
        + '</p>'
        '<p class="muted">Processing: '
        + _text(row.processing_status)
        + ' · Execution: '
        + _text(row.execution.status)
        + ' · Changes: '
        + _text(row.changes.status)
        + '</p>'
        '<p class="muted">Tokens: input / output / cache read / cache write: '
        + _text(_tokens(row.telemetry))
        + '</p><p class="muted">OpenCode USD: '
        + _cost(row.telemetry.cost)
        + ' · API-equivalent USD: '
        + _cost(row.telemetry.api_equivalent)
        + '</p>'
        '<h3>Files ('
        + str(len(row.scoring.files))
        + ')</h3>'
        + (files or '<p class="muted">No file scores available.</p>')
        + '</div></details>'
    )


def _model_tag(report: Report) -> str:
    """Match the viewer's configured model/effort label and evidence tooltip."""
    configured = report.inference.configured
    model, effort = configured.model, configured.effort
    label = model.value if model.status == 'known' else model.status
    if model.status == 'known' and effort.status == 'known':
        label = f'{label} [{effort.value}]'
    notes = []
    for name, fact in (('model', model), ('effort', effort)):
        note = f'Configured {name}: {fact.status}'
        if fact.reason:
            note += f' ({fact.reason})'
        if fact.source:
            note += f' · Source: {fact.source.ref} · {fact.source.field}'
        notes.append(note)
    return '<span class="tag" title="' + _text(' · '.join(notes)) + '">' + _text(label) + '</span>'


def render_page(report: Report, basket: Basket, data: JsonObject | None = None) -> bytes:
    """Render validated snapshots without reading or changing the saved run."""
    title = report.agent.name + ' — ' + report.run.id
    intro = (
        '<h1>'
        + _text(report.agent.name)
        + '</h1><div class="run-actions"><button id="show-config" type="button">See Run Config</button></div>'
        '<div class="metadata">'
        + _text(report.agent.runtime)
        + ' · '
        + _text(report.run.status)
        + ' · '
        + _text(report.run.created_at)
        + '</div><p class="hash">Run: '
        + _text(report.run.id)
        + ' · Basket: '
        + _text(report.basket.sha256)
        + '</p>'
        '<section class="basket-info"><h2>'
        + _text(basket.data.meta.name)
        + '</h2><p class="basket-description">'
        + _text(basket.data.meta.description)
        + '</p><p id="task-source" class="muted"></p></section>'
        '<div class="run-tags"><div><span class="muted">Model / effort</span><div class="tag-group">'
        + _model_tag(report)
        + '</div></div>'
        '<div><span class="muted">Setup</span><div class="tag-group">'
        + ''.join('<span class="tag">' + _text(tag) + '</span>' for tag in report.run.tags)
        + '</div></div></div><section id="benchmark-settings" class="benchmark-metadata"></section>'
    )
    details = dict(data or {})
    generation = next(
        (item.settings for item in reversed(basket.data.meta.lineage or []) if item.stage == 'generate'), {}
    )
    source, context = generation.get('source'), generation.get('context')
    details['task_source'] = {
        'source': source if source in ('issue', 'llm') else None,
        'context': context if source == 'llm' and context in ('diff', 'issue', 'diff+issue') else None,
    }
    diffs = details.get('diffs', {})
    tasks = [
        _task(row, index, has_diffs=isinstance(diffs, dict) and str(index) in diffs)
        for index, row in enumerate(report.tasks)
    ]
    return _page(title, intro, tasks, report, details)


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
