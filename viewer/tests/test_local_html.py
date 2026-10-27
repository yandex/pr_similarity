"""Keep local HTML aggregation selection independent of task diagnostics and search."""

import subprocess
from pathlib import Path

import pytest
from helpers import documents
from playwright.sync_api import expect, sync_playwright
from test_metrics import task

from viewer.models.report import Report, ReportInfra
from viewer.util import json_bytes


def _local_report_page(tmp_path: Path) -> Path:
    """Render the actual benchmark page with healthy and failed task evidence."""
    benchmark = Path(__file__).parents[2] / 'benchmark'
    interpreter = benchmark / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('requires the sibling benchmark module venv')
    report = Report.model_validate(documents()[0])
    healthy = task('OK', score=0.8, cost=1)
    healthy.id, healthy.title = 'healthy', 'Healthy task'
    failed = task('fail', score=0, cost=100)
    failed.id, failed.title = 'failed', 'Failed task'
    failed.infrastructure_error = ReportInfra(
        stage='setup', code='fixture_failure', message='Dependency installation failed', paths=[]
    )
    report.tasks = [healthy, failed]
    source = tmp_path / 'report.json'
    source.write_bytes(json_bytes(report.to_wire()))
    output = tmp_path / 'report.html'
    script = '''
import sys
from pathlib import Path
from benchmark.models.report import Report
from benchmark.persistence.storage import read_json
from benchmark.presentation.visualize import _page, _task
report = Report.model_validate(read_json(Path(sys.argv[1])))
data = {'config': {'supported': True, 'config': {'model': 'fixture/model'},
                   'source': {'path': 'agent/opencode.json', 'sha256': 'a' * 64}},
        'diffs': {'0': {'0': {'human_diff': '--- base/file.py\\n+++ human/file.py\\n@@ -1 +1 @@\\n-old\\n+human\\n',
                             'agent_diff': '--- base/file.py\\n+++ ai/file.py\\n@@ -1 +1 @@\\n-old\\n'
                                           '+</script><script>window.__diffUnsafe=true</script>\\n'}}}}
rows = [_task(row, index, has_diffs=index == 0) for index, row in enumerate(report.tasks)]
Path(sys.argv[2]).write_bytes(_page('Run', '<button id="show-config">See Run Config</button>', rows, report, data))
'''
    result = subprocess.run(
        [str(interpreter), '-c', script, str(source), str(output)],
        cwd=benchmark,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def test_status_selection_keeps_all_local_tasks_and_diagnostics(tmp_path: Path) -> None:
    """Status toggles change aggregates; only text search may hide task rows."""
    path = _local_report_page(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            errors: list[str] = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            rows = page.locator('.task:visible')
            selected = page.locator('.stat').filter(has_text='Selected tasks').locator('strong')
            score = page.locator('.stat').filter(has_text='Score').locator('strong')
            expect(selected).to_have_text('1/2')
            expect(score).to_have_text('0.800')
            expect(rows).to_have_count(2)
            failed = page.locator('.task').filter(has_text='Failed task')
            failed.locator('summary').click()
            diagnostic = failed.locator('.error')
            expect(diagnostic).to_have_text('Dependency installation failed')
            expect(diagnostic).to_be_visible()
            page.locator('#selection input[value="fail"]').check()
            expect(selected).to_have_text('2/2')
            expect(score).to_have_text('0.400')
            expect(rows).to_have_count(2)
            for checkbox in page.locator('#selection input').all():
                checkbox.uncheck()
            expect(selected).to_have_text('0/2')
            expect(score).to_have_text('—')
            expect(rows).to_have_count(2)
            expect(diagnostic).to_be_visible()
            page.locator('#search').fill('Healthy task')
            expect(rows).to_have_count(1)
            expect(rows).to_contain_text('Healthy task')
            page.locator('#search').fill('Dependency installation failed')
            expect(rows).to_have_count(1)
            expect(diagnostic).to_be_visible()
            page.locator('#selection input[value="fail"]').check()
            expect(selected).to_have_text('1/2')
            expect(rows).to_have_count(1)
            page.locator('#search').fill('')
            expect(rows).to_have_count(2)
            expect(diagnostic).to_be_visible()
            page.locator('#file').select_option('Mf')
            page.locator('#task-sort').select_option('score')
            page.locator('#task-direction').select_option('desc')
            expect(rows.first).to_contain_text('Healthy task')
            page.locator('#task-direction').select_option('asc')
            expect(rows.first).to_contain_text('Failed task')
            expect(diagnostic).to_be_visible()
            page.get_by_role('button', name='Original task order', exact=True).click()
            expect(rows.first).to_contain_text('Healthy task')
            expect(page.locator('#task-direction')).to_be_disabled()
            page.locator('#task-sort').select_option('total_tokens')
            expect(page.locator('#task-direction')).to_be_enabled()
            assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(5, 7, 12)'
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert errors == []
        finally:
            browser.close()


def test_local_diffs_and_config_are_embedded_and_keyboard_accessible(tmp_path: Path) -> None:
    """A moved HTML file opens both dialogs without API access or source execution."""
    path = _local_report_page(tmp_path)
    moved = tmp_path / 'moved.html'
    path.replace(moved)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            requests: list[str] = []
            errors: list[str] = []
            page.on('request', lambda request: requests.append(request.url))
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(moved.as_uri())
            first = page.locator('.task').first
            first.locator('summary .title').click()
            expect(first).to_have_attribute('open', '')
            expect(first.get_by_role('link', name='Original PR')).to_be_visible()
            file = first.locator('button.file').first
            file.click()
            dialog = page.locator('#diff-dialog')
            expect(dialog).to_be_visible()
            expect(dialog.locator('.diff-panel')).to_have_count(2)
            expect(dialog.get_by_label('Human diff', exact=True)).to_contain_text('+human')
            expect(dialog.get_by_label('Agent diff', exact=True)).to_contain_text('</script>')
            assert page.evaluate('window.__diffUnsafe') is None
            page.keyboard.press('Escape')
            expect(dialog).not_to_be_visible()
            expect(file).to_be_focused()
            page.get_by_role('button', name='See Run Config').click()
            expect(page.locator('#config-content pre')).to_contain_text('fixture/model')
            page.get_by_role('button', name='Close config', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            file.click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            expect(dialog.get_by_label('Agent diff', exact=True)).to_be_visible()
            assert not [url for url in requests if url.startswith(('http:', 'https:'))]
            assert not errors
        finally:
            browser.close()
