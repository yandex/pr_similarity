"""Stored publications recover request subtotals without rewriting frozen evidence."""

import json
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
import test_ui
from helpers import archive_fixture
from playwright.sync_api import Page, expect, sync_playwright
from starlette.applications import Starlette
from test_service import service
from viewer.config import Limits
from viewer.errors import ViewerError
from viewer.models import WireModel
from viewer.presentation.queries import public_task
from viewer.util import digest, json_bytes
from viewer.validation.archive import validate_archive
from viewer.validation.operations import OperationStore

browser_page = test_ui.browser_page


def partial_usage(report: dict, basket: dict, files: dict[str, bytes]) -> None:
    """Supply 99 finished requests plus an unfinished unit with untrusted zero counters."""
    task = report['tasks'][0]
    usage_path = task['directory'] + '/attempts/001/telemetry/usage.json'
    requests = []
    for index in range(100):
        requests.append(
            {
                'id': str(index),
                'session_id': 'root',
                'provider': 'provider',
                'model': 'model',
                'complete': index != 99,
                'usage': dict(
                    input=10,
                    output=2,
                    reasoning=1,
                    cache_read=3,
                    cache_write=0,
                    total=16,
                ),
                'billing_tokens': dict(input=10, output=3, cache_read=3, cache_write=0),
                'input_context_tokens': 13,
                'reported_cost_usd': 0.1,
            }
        )
    ledger = {
        'schema_family': 'pr_similarity_os.usage',
        'schema_version': 1,
        'run_id': report['run']['id'],
        'task_id': task['id'],
        'attempt_id': task['final_attempt']['id'],
        'runtime': report['agent']['runtime'],
        'adapter_version': 'opencode-db-v1',
        'source_db': task['telemetry']['session_db'],
        'complete': False,
        'sessions': [{'id': 'root', 'parent_id': None, 'agent_name': None, 'kind': 'root'}],
        'requests': requests,
    }
    files[usage_path] = json_bytes(ledger)
    task['telemetry']['transport'] = 'db'
    task['telemetry']['usage_ledger'] = {
        'path': usage_path,
        'sha256': digest(files[usage_path]),
    }
    task['telemetry']['tokens'] = {key: None for key in task['telemetry']['tokens']}
    task['telemetry']['cost']['amount'] = None
    task['telemetry']['status'] = 'partial'
    task['telemetry']['total_source'] = 'unavailable'


def partial_zero_usage(report: dict, basket: dict, files: dict[str, bytes]) -> None:
    """Retain measured zero requests and an unknown full amount."""
    partial_usage(report, basket, files)
    reference = report['tasks'][0]['telemetry']['usage_ledger']
    ledger = json.loads(files[reference['path']])
    for request in ledger['requests']:
        request['reported_cost_usd'] = 0
    files[reference['path']] = json_bytes(ledger)
    reference['sha256'] = digest(files[reference['path']])


def test_refresh_recovers_partial_usage_and_get_reuses_compact_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catalogue and task projection use the same measured requests and preserve archive bytes."""
    svc = service(tmp_path)
    path = tmp_path / 'partial.tar.gz'
    report, _ = archive_fixture(path, configure=partial_usage)
    original = path.read_bytes()
    imported = validate_archive(path, tmp_path / 'imported', Limits())
    svc.publish(imported, 'alice', 'partial')
    svc.refresh()
    rid = report['run']['id']
    task = public_task(imported.report.tasks[0], 'LCf', svc.runs[rid]['measurements'])

    def no_storage_read(*args: object, **kwargs: object) -> NoReturn:
        """Reject any storage access after the compact projection was cached."""
        raise AssertionError('GET must use normalized cached measurements')

    monkeypatch.setattr(svc.store, 'get', no_storage_read)
    row = svc.run(rid)
    assert row['tokens'] == task['tokens']
    total = row['tokens']['total']
    assert total['amount'] is None
    assert total['known_subtotal'] == 1584
    assert total['known_requests'] == 99
    assert total['total_requests'] == 100
    assert total['partial_tasks'] == 1
    assert total['status'] == 'partial'
    assert row['cost']['known_subtotal'] == 9.9
    assert row['cost']['amount'] is None
    assert row['cost']['known_tasks'] == 0
    assert row['cost']['partial_tasks'] == 1
    assert row['tokens']['cache_write']['known_subtotal'] == 0
    assert row['tokens']['cache_write']['status'] == 'partial'
    assert svc.run(rid, statuses=[])['cost']['status'] == 'unavailable'
    assert path.read_bytes() == original
    svc.db.close()


def test_failed_evidence_refresh_preserves_previous_cache(tmp_path: Path) -> None:
    """A changed declared ledger is an integrity error, not ordinary missing telemetry."""
    svc = service(tmp_path)
    path = tmp_path / 'partial.tar.gz'
    report, _ = archive_fixture(path, configure=partial_usage)
    svc.publish(validate_archive(path, tmp_path / 'imported', Limits()), 'alice', 'partial')
    rid = report['run']['id']
    before = svc.run(rid)
    ledger_path = report['tasks'][0]['telemetry']['usage_ledger']['path']
    svc.store.put(svc.runs[rid]['prefix'] + 'data/' + ledger_path, b'changed')
    with pytest.raises(ViewerError):
        svc.refresh()
    assert svc.run(rid) == before
    svc.db.close()


def test_partial_usage_is_visible_in_browser(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """The run card and task details expose the same partial zero and coverage."""
    page, origin, app, errors = browser_page
    svc = app.state.service
    path = tmp_path / 'partial-browser.tar.gz'
    report, _ = archive_fixture(path, configure=partial_usage)
    svc.publish(validate_archive(path, tmp_path / 'partial-browser', Limits()), 'alice', 'partial-browser')
    page.goto(origin + '/run?run_id=' + report['run']['id'])
    cost = page.locator('.stat').filter(has_text='Cost, USD')
    expect(cost).to_contain_text('9.900000 (partial)')
    title = cost.get_attribute('title')
    assert title is not None
    assert '99/100 requests measured' in title
    summary = page.locator('.task-metrics')
    expect(summary).to_contain_text('1,584 (partial)')
    page.locator('.task summary .title').click()
    expect(page.locator('.task-body')).to_contain_text('9.900000 (partial)')
    expect(page.locator('.task-body')).to_contain_text('0 (partial)')
    page.locator('#statuses input[value="OK"]').uncheck()
    expect(cost).to_contain_text('—')
    page.locator('#statuses input[value="OK"]').check()
    expect(cost).to_contain_text('9.900000 (partial)')


@pytest.mark.parametrize('zero_cost', [False, True])
def test_partial_local_html_matches_viewer_projection(tmp_path: Path, zero_cost: bool) -> None:
    """The autonomous page renders the same synthetic requests without a network."""
    svc = service(tmp_path)
    path = tmp_path / 'partial-local.tar.gz'
    report, _ = archive_fixture(path, configure=partial_zero_usage if zero_cost else partial_usage)
    imported = validate_archive(path, tmp_path / 'partial-local', Limits())
    svc.publish(imported, 'alice', 'partial-local')
    rid = report['run']['id']
    measurements = svc.runs[rid]['measurements']
    # Transfer compact measurements only; the offline page must not contain the ledger.
    source = tmp_path / 'measurements.json'
    source.write_text(
        json.dumps(
            {
                key: {
                    'tokens': {name: vars(value) for name, value in task.tokens.items()},
                    'cost': {**vars(task.cost), 'subtotal': str(task.cost.subtotal)},
                    'cost_source': task.cost_source,
                }
                for key, task in measurements.items()
            }
        )
    )
    benchmark = Path(__file__).parents[2] / 'benchmark'
    output = tmp_path / 'report.html'
    script = '''
import json
import sys
from decimal import Decimal
import json
import subprocess
from pathlib import Path
from typing import NoReturn
from benchmark.models.report import Report
from benchmark.presentation.measurements import Measurement
from benchmark.presentation.task_measurements import TaskMeasurements
from benchmark.presentation.visualize import _page, _task
from benchmark.presentation.visualize_data import display_metrics
report = Report.model_validate(json.loads(Path(sys.argv[1]).read_text()))
values = json.loads(Path(sys.argv[2]).read_text())
measurements = {}
for key, value in values.items():
    value['cost']['subtotal'] = Decimal(value['cost']['subtotal'])
    measurements[key] = TaskMeasurements(
        tokens={name: Measurement(**data) for name, data in value['tokens'].items()},
        cost=Measurement(**value['cost']), cost_source=value['cost_source'])
rows = [_task(task, index) for index, task in enumerate(report.tasks)]
data = {'measurements': display_metrics(report, measurements)}
Path(sys.argv[3]).write_bytes(_page('Partial', '', rows, report, data))
'''
    subprocess.run(
        [
            str(benchmark / '.venv/bin/python'),
            '-c',
            script,
            str(imported.directory / 'report.json'),
            str(source),
            str(output),
        ],
        cwd=benchmark,
        check=True,
        capture_output=True,
        timeout=30,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(output.as_uri())
            cost = page.locator('.stat').filter(has_text='Cost, USD')
            expect(cost).to_contain_text(('0.000000' if zero_cost else '9.900000') + ' (partial)')
            title = cost.get_attribute('title')
            assert title is not None
            assert '99/100 requests measured' in title
            if zero_cost:
                assert 'not verified free usage or measured subscription API cost' in title
            expect(page.locator('.task-metrics')).to_contain_text('1584 (partial)')
            page.locator('.task summary').click()
            expect(page.locator('.task-tokens')).to_contain_text('cache_write: 0 (partial)')
            expect(page.locator('.task-cost')).to_contain_text(
                ('0.00000000' if zero_cost else '9.90000000') + ' (partial)'
            )
            page.locator('#selection input[value="OK"]').uncheck()
            expect(cost).to_contain_text('—')
            page.locator('#selection input[value="OK"]').check()
            expect(cost).to_contain_text(('0.000000' if zero_cost else '9.900000') + ' (partial)')
        finally:
            browser.close()
    svc.db.close()


def test_shared_selected_price_is_read_once_per_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated provenance checks reuse the same operation result only within one refresh."""
    svc = service(tmp_path)
    path = Path(__file__).parent / 'fixtures/legacy/producer-v1-basket2.tar.gz'
    imported = validate_archive(path, tmp_path / 'historical', Limits())
    svc.publish(imported, 'alice', 'historical')
    reads = []
    original = OperationStore.document

    def record[T: WireModel](self: OperationStore, path: str, model: type[T]) -> T:
        """Count reads of the shared selected price document."""
        if path.startswith('operations/reprice/') and path.endswith('/result.json'):
            reads.append(path)
        return original(self, path, model)

    monkeypatch.setattr(OperationStore, 'document', record)
    svc.refresh()
    assert len(reads) == 1
    svc.refresh()
    assert len(reads) == 2
    svc.db.close()
