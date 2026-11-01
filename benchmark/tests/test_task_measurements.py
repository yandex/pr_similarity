"""Display evidence follows final selections without modifying saved artifacts."""

import json
import sqlite3
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from benchmark.persistence.run_store import RunStore
from benchmark.presentation.task_measurements import build_task_measurements
from benchmark.presentation.visualize import visualize
from benchmark.pricing.operations import reprice
from benchmark.publication.archive import create_archive
from benchmark.runtime import attempt as executor
from test_archive import complete_run
from test_cost_summary import tariffs
from test_operation_workflows import stopped_run


def test_display_uses_final_attempt_and_exact_selected_price(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry's display excludes older spend and preserves persisted report bytes."""
    directory, _, _ = stopped_run(tmp_path, monkeypatch, attempts=2)
    assert reprice(directory, tariffs(tmp_path)) == 0
    store = RunStore.open(directory)
    report = store.snapshot()
    before = (directory / 'report.json').read_bytes()
    projected = build_task_measurements(store, report)
    assert len(projected) == 1
    task = next(iter(projected.values()))
    assert task.tokens['total'].subtotal == 64
    assert task.tokens['total'].known_requests == 4
    assert task.cost.subtotal == Decimal('0.000202400000')
    assert task.cost.complete
    assert task.cost_source == 'reprice'
    assert (directory / 'report.json').read_bytes() == before
    assert report.costs.all_attempts.api_equivalent.amount == '0.000404800000'


def test_partial_usage_and_selected_price_match_public_viewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both public readers independently reconstruct the same sealed partial measurements."""
    original = executor._Attempt.telemetry

    def unfinished(self: executor._Attempt) -> None:
        """Leave one registered native request unfinished before the producer seals it."""
        with sqlite3.connect(self.private / 'home/data/opencode/opencode.db') as database:
            raw = database.execute('select data from part where id=?', ('part-detached',)).fetchone()[0]
            request = json.loads(raw)
            request.update(reason=None, cost=None)
            database.execute('update part set data=? where id=?', (json.dumps(request), 'part-detached'))
        original(self)

    monkeypatch.setattr(executor._Attempt, 'telemetry', unfinished)
    directory = complete_run(tmp_path, monkeypatch)
    assert reprice(directory, tariffs(tmp_path)) == 1
    archive = tmp_path / 'partial.tar.gz'
    create_archive(directory, archive)
    protected = [path for path in directory.rglob('*.json') if '_meta/invocations' not in path.as_posix()]
    before = {path: path.read_bytes() for path in protected}
    payload = archive.read_bytes()
    html = visualize(directory).read_text()
    embedded = json.loads(html.split('<script type="application/json" id="view-data">', 1)[1].split('</script>', 1)[0])
    expected = embedded['measurements']['selections']['7']
    assert expected['tokens']['total']['amount'] is None
    assert expected['tokens']['total']['known_subtotal'] == 48
    assert expected['tokens']['total']['known_requests'] == 3
    assert expected['tokens']['total']['total_requests'] == 4
    assert expected['cost']['amount'] is None
    assert expected['cost']['known_subtotal'] == 0.0001518
    actual = _public_viewer_projection(archive, tmp_path)
    assert actual['run'] == actual['task'] == expected
    assert before == {path: path.read_bytes() for path in protected}
    assert archive.read_bytes() == payload


def _public_viewer_projection(archive: Path, tmp_path: Path) -> dict:
    """Use the consumer's own interpreter and HTTP API, with no producer imports."""
    viewer = Path(__file__).resolve().parents[2] / 'viewer'
    script = '''
import json
import sys
from pathlib import Path
from starlette.testclient import TestClient
from viewer.config import Config, Limits
from viewer.persistence.storage import MemoryStore
from viewer.validation.archive import validate_archive
from viewer.web.api import create_app
root = Path(sys.argv[2])
archive = validate_archive(Path(sys.argv[1]), root / 'imported', Limits())
token = root / 'admin-token'
token.write_text('local-fixture-admin-token-123456789')
token.chmod(0o400)
app = create_app(Config(runtime=root / 'viewer-runtime', storage='mock', admin_token_file=token), MemoryStore())
service = app.state.service
service.db.create_user('fixture', 'local-fixture-password')
service.publish(archive, 'fixture', 'partial')
with TestClient(app) as client:
    base = '/api/v1/runs/' + archive.report.run.id
    run = client.get(base)
    tasks = client.get(base + '/tasks')
    assert run.status_code == tasks.status_code == 200
    result = {}
    for kind, value in [('run', run.json()), ('task', tasks.json()['items'][0])]:
        cost = {key: amount for key, amount in value['cost'].items()
                if key not in ('currency', 'sources', 'native_zero_tasks')}
        result[kind] = {'cost': cost, 'tokens': value['tokens']}
    print(json.dumps(result))
'''
    result = subprocess.run(
        [str(viewer / '.venv/bin/python'), '-c', script, str(archive), str(tmp_path)],
        cwd=viewer,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)
