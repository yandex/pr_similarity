"""One producer archive drives identical server and offline run-page projections."""

import itertools
import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import expect
from test_producer_roundtrip import _producer_archive
from test_ui import BrowserPage
from viewer.validation.archive import validate_archive

pytest_plugins = ['test_ui']


def test_actual_archive_has_online_offline_run_page_parity(browser_page: BrowserPage, tmp_path: Path) -> None:
    """Compare all selections, config and full-file views; then exercise local dialogs without HTTP."""
    page, origin, app, errors = browser_page
    archive = _producer_archive(tmp_path)
    imported = validate_archive(archive, tmp_path / 'validated', app.state.service.config.limits)
    receipt = app.state.service.publish(imported, 'alice', 'run-page-parity')
    rid = receipt['run_id']
    benchmark = Path(__file__).parents[2] / 'benchmark'
    result = subprocess.run(
        [str(benchmark / '.venv/bin/python'), '-m', 'benchmark', 'visualize', '--run', str(imported.directory)],
        cwd=benchmark,
        env={**os.environ, 'PYTHONHASHSEED': '42'},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    page.goto(origin + '/run?run_id=' + rid)
    expect(page.locator('.run-overview')).to_be_visible()
    expect(page.locator('.task')).to_have_count(len(imported.report.tasks))
    server_metrics = page.locator('.run-summary-grid').inner_text()
    assert page.context.browser is not None
    local_context = page.context.browser.new_context(viewport={'width': 1600, 'height': 1100})
    local = local_context.new_page()
    local_errors, network = [], []
    local.on('pageerror', lambda error: local_errors.append(str(error)))
    local.on(
        'request', lambda request: network.append(request.url) if request.url.startswith(('http:', 'https:')) else None
    )
    try:
        local.goto((imported.directory / 'report.html').as_uri())
        expect(local.locator('.run-overview')).to_be_visible()
        expect(local.locator('.task')).to_have_count(len(imported.report.tasks))
        assert local.locator('.run-summary-grid').inner_text() == server_metrics
        assert local.locator('.pin:visible').count() == 0
        assert local.get_by_role('button', name='Delete Run', exact=True).count() == 0
        for mask, pr, file in itertools.product(range(16), ('Mpr', 'FCpr', 'LCpr'), ('Mf', 'LCf')):
            statuses = ','.join(
                status for index, status in enumerate(('OK', 'no-op', 'timeout', 'fail')) if mask & (1 << index)
            )
            path = '/runs/' + rid + '?' + urlencode({'statuses': statuses, 'pr': pr, 'file': file})
            remote = page.request.get(origin + '/api/v1' + path).json()
            embedded = local.evaluate('(path) => window.prsimLocal.read(path)', path)
            for key in (
                'selection',
                'score',
                'f1_files',
                'total_tasks',
                'selected_tasks',
                'scored_tasks',
                'status_counts',
                'cost',
                'tokens',
                'time',
            ):
                assert embedded[key] == remote[key], (mask, pr, file, key)
        path = '/runs/' + rid + '/config'
        remote = page.request.get(origin + '/api/v1' + path).json()
        embedded = local.evaluate('(path) => window.prsimLocal.read(path)', path)
        input_model = embedded['config']['model']
        for key in ('supported', 'config', 'setup_summary'):
            assert embedded[key] == remote[key]
        for file in ('Mf', 'LCf'):
            path = '/runs/' + rid + '/tasks?file=' + file
            remote = page.request.get(origin + '/api/v1' + path).json()['items']
            embedded = local.evaluate('(path) => window.prsimLocal.read(path)', path)['items']
            assert [row['task_id'] for row in embedded] == [row['task_id'] for row in remote]
            for left, right in zip(embedded, remote, strict=True):
                assert {key: left[key] for key in right} == right
        for task in imported.report.tasks:
            path = '/runs/' + rid + '/tasks/' + task.id + '?view=changes'
            assert (
                local.evaluate('(path) => window.prsimLocal.read(path)', path)
                == page.request.get(origin + '/api/v1' + path).json()
            )
            details = page.request.get(origin + '/api/v1/runs/' + rid + '/tasks/' + task.id).json()
            for file in details['files']:
                for query in ('', '?view=full'):
                    path = '/runs/' + rid + '/tasks/' + task.id + '/files/' + file['file_id'] + query
                    assert (
                        local.evaluate('(path) => window.prsimLocal.read(path)', path)
                        == page.request.get(origin + '/api/v1' + path).json()
                    )
        local.get_by_role('button', name='opencode.json', exact=True).click()
        expect(local.locator('#config-content pre')).to_contain_text(input_model)
        local.get_by_role('button', name='Close config', exact=True).click()
        local.locator('.task .title').first.click()
        expect(local.locator('.task-change-summary[data-side=human]')).to_contain_text('Human PR:')
        local.locator('button.file').first.click()
        expect(local.locator('.diff-lines')).to_have_count(2)
        local.get_by_role('button', name='Full file', exact=True).click()
        expect(local.locator('.full-file-panel')).to_have_count(2)
        expect(local.locator('.file-change-map')).to_have_count(2)
        assert not network and not local_errors and not errors
    finally:
        local_context.close()
