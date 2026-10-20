"""Offline, self-contained v1 report tables and ordered run comparisons."""

import html
import uuid
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from benchmark.basket import load_basket
from benchmark.commands import Command
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.report import read_report
from benchmark.scoring import diagnostic_metrics
from benchmark.storage import atomic_write, run_lock

_STYLE = '''body{font:15px system-ui,sans-serif;
color:#17212b;
background:#fafbfc;
margin:24px}

h1{font-size:24px}
table{border-collapse:collapse;
width:100%;
background:white}
th,td{border:1px solid #dae0e5;
padding:9px;
text-align:left;
vertical-align:top}

th{cursor:pointer;
background:#eef2f6;
position:sticky;
top:0}
input{padding:8px;
min-width:280px;
margin:12px 0}
code{white-space:pre-wrap}
details{max-width:900px}
summary{cursor:pointer}
.muted{color:#647384}
.error{color:#9d2424}
small{display:block}
'''
_SCRIPT = '''const field=document.querySelector('#search');
field.addEventListener('input',()=>{const q=field.value.toLowerCase();
document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))}
);
document.querySelectorAll('th').forEach((h,i)=>h.addEventListener('click',()=>{const b=document.querySelector('tbody');
const rows=[...b.rows];
const d=h.dataset.direction==='up'?-1:1;
rows.sort((a,c)=>d*a.cells[i].textContent.localeCompare(c.cells[i].textContent,undefined,{numeric:true}
));
rows.forEach(r=>b.append(r));
h.dataset.direction=d===1?'up':'down'}
));
'''


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


def _number(value: float | None, places: int = 4) -> str:
    """Format a measured value or the unknown marker."""
    return '—' if value is None else format(value, f'.{places}f')


def _tokens(telemetry: dict) -> str:
    """Display independent token categories without replacing unknowns."""
    return ' / '.join(
        '—' if telemetry['tokens'][key] is None else str(telemetry['tokens'][key])
        for key in ('input', 'output', 'cache_read', 'cache_write')
    )


def _cost(value: dict | None) -> str:
    """Format original or derived cost without conflating their sources."""
    return '—' if value is None else _number(value['amount'], 8)


def _page(title: str, intro: str, headings: list[str], rows: list[list[str]]) -> bytes:
    """Build a self-contained sortable and filterable report document."""
    headers = ''.join('<th scope="col">' + _text(name) + '</th>' for name in headings)
    body = ''.join('<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows)
    document = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>'
        + _text(title)
        + '</title><style>'
        + _STYLE
        + '</style><body><h1>'
        + _text(title)
        + '</h1>'
        + intro
        + '<label>Filter tasks <input id="search" type="search" placeholder="PR, status, path"></label>'
        '<p class="muted">Click a column to sort. — means unknown or unavailable. Scores use the final attempt.</p>'
        '<table><thead><tr>'
        + headers
        + '</tr></thead><tbody>'
        + body
        + '</tbody></table><script>'
        + _SCRIPT
        + '</script></body></html>'
    )
    return document.encode('utf-8')


def _task(row: dict) -> str:
    """Render escaped task details and per-file diagnostics."""
    files = ''.join(
        '<li>' + _text(item['path']) + ': MLP=' + _number(item['mlp']) + ', branch=' + _text(item['branch']) + '</li>'
        for item in row['scoring']['files']
    )
    error = row['infrastructure_error']
    diagnostic = '' if error is None else '<p class="error">' + _text(error['message']) + '</p>'
    return (
        '<details><summary>'
        + _text(row['id'])
        + ' — '
        + _text(row['title'])
        + '</summary><p>Workspace: '
        + _text(row['workspace'])
        + '</p><code>'
        + _text(row['agent_task'])
        + '</code>'
        + diagnostic
        + '<ul>'
        + files
        + '</ul></details>'
    )


def visualize(run_dir: Path, *, no_browser: bool = False, level: str = 'INFO') -> Path:
    """Write an offline table for one canonical run report."""
    run_dir = run_dir.expanduser().resolve()
    with _view_command(run_dir, 'visualize', level):
        report = read_report(run_dir)
        title = report['agent']['name'] + ' — ' + report['run']['id']
        metrics = diagnostic_metrics(report['tasks'])
        intro = (
            '<p>Run status: '
            + _text(report['run']['status'])
            + '; scored '
            + str(metrics['scored_tasks'])
            + '/'
            + str(len(report['tasks']))
            + '. Basket: <code>'
            + _text(report['basket']['sha256'])
            + '</code>.</p>'
            '<p>Mpr/LCf: '
            + _number(metrics['Mpr_LCf_mlp'])
            + '; LCpr/LCf: '
            + _number(metrics['LCpr_LCf_mlp'])
            + '; global F1: '
            + _number(metrics['global_F1'])
            + '.</p>'
        )
        rows = []
        for row in report['tasks']:
            attempt = row['final_attempt']
            rows.append(
                [
                    _task(row),
                    _text(row['processing_status']),
                    _text(row['execution']['status']),
                    _number(row['execution']['duration_seconds'], 2),
                    _text(row['changes']['status']),
                    _number(row['scoring']['Mf_mlp']),
                    _number(row['scoring']['LCf_mlp']),
                    _text(_tokens(row['telemetry'])),
                    _cost(row['telemetry']['cost']),
                    _cost(row['telemetry']['api_equivalent']),
                    '—' if attempt is None else str(attempt['sequence']),
                ]
            )
        path = run_dir / 'report.html'
        atomic_write(
            path,
            _page(
                title,
                intro,
                [
                    'Task',
                    'Processing',
                    'Execution',
                    'Seconds',
                    'Changes',
                    'Mf MLP',
                    'LCf MLP',
                    'Tokens: input / output / cache read / cache write',
                    'OpenCode USD',
                    'API-equivalent USD',
                    'Attempt',
                ],
                rows,
            ),
        )
    if not no_browser:
        webbrowser.open(path.as_uri())
    return path


def compare(run_dirs: list[Path], *, no_browser: bool = False, level: str = 'INFO') -> Path:
    """Compare ordered run snapshots without changing their reports."""
    if len(run_dirs) < 2:
        raise BenchmarkError('compare_runs', 'Compare requires at least two run directories in RUNS_JSON.')
    paths = [path.expanduser().resolve() for path in run_dirs]
    with _view_command(paths[0], 'compare', level) as command:
        reports = [read_report(path) for path in paths]
        for path in paths:
            command.warn_basket(load_basket(path / 'basket.json'))
        names = [report['agent']['name'] + ' / ' + report['run']['id'][:8] for report in reports]
        intro = (
            '<ul>'
            + ''.join(
                '<li>'
                + _text(name)
                + ': basket <code>'
                + _text(report['basket']['sha256'])
                + '</code>, status '
                + _text(report['run']['status'])
                + '</li>'
                for name, report in zip(names, reports, strict=True)
            )
            + '</ul>'
        )
        if len({report['basket']['sha256'] for report in reports}) > 1:
            command.message(
                'Comparing different baskets; missing tasks are shown separately in the HTML.', level='WARNING'
            )
            intro += (
                '<p class="error">Different baskets: missing tasks are shown as —; '
                'scores are not directly comparable.</p>'
            )
        indexed = [{row['id']: row for row in report['tasks']} for report in reports]
        ids = sorted({key for index in indexed for key in index})
        rows = []
        for key in ids:
            cells = [_text(key)]
            for index in indexed:
                row = index.get(key)
                cells.append(
                    '—'
                    if row is None
                    else _number(row['scoring']['LCf_mlp'])
                    + '<small>'
                    + _text(row['execution']['status'])
                    + ' / diff '
                    + _text(row['changes']['status'])
                    + '</small><small>Tokens: '
                    + _text(_tokens(row['telemetry']))
                    + '</small>'
                )
            rows.append(cells)
        path = paths[0].parent / ('compare_' + str(uuid.uuid4()) + '.html')
        atomic_write(path, _page('Benchmark run comparison', intro, ['Task', *names], rows))
    if not no_browser:
        webbrowser.open(path.as_uri())
    return path
