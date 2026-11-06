"""Render the viewer-owned interface from an immutable offline data snapshot."""

import base64
import html
import json
import re
from datetime import datetime
from hashlib import sha256
from importlib.resources import files
from typing import Any

from benchmark import __version__
from benchmark.models.report import Report
from benchmark.presentation.run_metrics import aggregate, aggregate_snapshot, tag_groups, task_status
from benchmark.presentation.visualize_data import display_metrics


def page_data(report: Report, basket: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Project the same run/task fields consumed by the server renderer."""
    measurements = data.get('measurements') or display_metrics(report)
    aggregates = data.get('aggregates') or aggregate_snapshot(report.tasks)
    # Synthetic snapshots may supply validated request-level measurements separately.
    for mask, metric in aggregates.items():
        normalized = measurements['selections'][str(mask)]
        metric['cost'].update(normalized['cost'])
        metric['tokens'] = normalized['tokens']
    run = report.run
    duration = None
    if run.finished_at:
        duration = (
            datetime.fromisoformat(run.finished_at.replace('Z', '+00:00'))
            - datetime.fromisoformat(run.created_at.replace('Z', '+00:00'))
        ).total_seconds()
        if duration < 0:
            duration = None
    row = {
        'run_id': run.id,
        'report_sha256': data.get('report_sha256'),
        'publication_sha256': None,
        'user': 'Local',
        'auth_mode': data.get('auth_mode'),
        'basket_hash': report.basket.sha256,
        'basket': basket,
        'wall_time_seconds': duration,
        'display_name': report.agent.name,
        'tags': list(run.tags),
        'pinned': False,
        'created_at': run.created_at,
        'tag_groups': tag_groups(list(run.tags), report.inference),
        'inference': report.inference.to_wire(),
        'runtime': report.agent.runtime,
        'status': run.status,
        'benchmark': data.get('benchmark'),
    }
    tasks = []
    for task in report.tasks:
        metrics = data.get('task_metrics', {}).get(task.id) or {
            kind: aggregate([task], [task_status(task)], 'Mpr', kind) for kind in ('Mf', 'LCf')
        }
        for metric in metrics.values():
            metric['cost'].update(measurements['tasks'][task.id]['cost'])
            metric['tokens'] = measurements['tasks'][task.id]['tokens']
        public_files = [
            {
                'file_id': sha256(file.path.encode()).hexdigest(),
                'path': file.path,
                'score': file.mlp,
                'changed_human': file.changed_human,
                'changed_agent': file.changed_agent,
                'exists': file.exists.to_wire(),
            }
            for file in task.scoring.files
        ]
        tasks.append(
            {
                'task_id': task.id,
                'kind': task.kind,
                'repository': task.repository,
                'number': task.number,
                'url': task.url,
                'title': task.title,
                'status': task_status(task),
                'scoring_status': task.scoring.status,
                'condition': task.agent_task,
                'files': public_files,
                'metrics': metrics,
                'diagnostic': task.infrastructure_error.message if task.infrastructure_error else None,
            }
        )
    return {
        'run': row,
        'aggregates': aggregates,
        'tasks': tasks,
        'config': data.get('config', {'supported': False}),
        'file_views': data.get('file_views', {}),
        'file_diffs': data.get('file_diffs', {}),
        'changes': data.get('changes', {}),
    }


def render(report: Report, basket: dict[str, Any], data: dict[str, Any]) -> bytes:
    """Embed verified data and packaged resources without external script or CSS URLs."""
    root = files('benchmark').joinpath('presentation/run_page_assets')
    document = root.joinpath('index.html').read_text()
    document = document.replace('{{BUILD_INFO}}', html.escape('v' + __version__ + ' · Local offline report'))
    document = re.sub(r'  <script src="[^"\n]+" defer></script>\n', '', document)
    document = re.sub(r'  <link[^>]+>\n', '', document)
    document = re.sub(
        r'<title>.*?</title>',
        lambda _: '<title>' + html.escape(report.agent.name) + ' · PR Similarity</title>',
        document,
    )
    document = document.replace('<body>', '<body class="offline-run">').replace('href="/"', 'href="#content"')
    icon = base64.b64encode(root.joinpath('favicon.svg').read_bytes()).decode()
    document = document.replace('src="/assets/favicon.svg"', 'src="data:image/svg+xml;base64,' + icon + '"')
    document = document.replace('</head>', '<style>' + root.joinpath('style.css').read_text() + '</style></head>')
    scripts = (
        '<script type="application/json" id="local-data">'
        + json.dumps(page_data(report, basket, data), ensure_ascii=True).replace('<', '\\u003c')
        + '</script>'
    )
    adapter = files('benchmark').joinpath('presentation/run_page_assets/offline.js').read_text()
    for source in (adapter, root.joinpath('config.js').read_text(), root.joinpath('app.js').read_text()):
        scripts += '<script>' + source.replace('</script', '<\\/script') + '</script>'
    document = document.replace('</body>', scripts + '</body>')
    return document.encode()
