"""Cost projections follow registered native facts and each selected pricing result."""

import json
import sqlite3
from decimal import localcontext
from pathlib import Path

import pytest
import yaml
from benchmark import attempt as executor
from benchmark import cost_summary, repricing, run_store
from benchmark.commands import Command
from benchmark.cost_summary import build_cost_summary
from benchmark.errors import BenchmarkError
from benchmark.pricing import load_policy
from benchmark.result import validate_report
from benchmark.run_store import RunStore
from benchmark.storage import read_json, write_json
from test_operation_workflows import empty_run, stopped_run
from test_pricing import policy


def tariffs(tmp_path: Path) -> Path:
    """Write compatible synthetic rates without any model or network access."""
    data = policy()
    data['models'][0].update(provider='provider', model='model')
    path = tmp_path / 'pricing.yaml'
    path.write_text(yaml.safe_dump(data))
    return path


def test_retry_spend_separates_final_and_all_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed retry does not erase the original native and estimated spend."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, attempts=2)
    assert repricing.reprice(directory, tariffs(tmp_path)) == 0
    costs = build_cost_summary(RunStore.open(directory))
    assert costs['final_attempts']['native_reported']['amount'] == '2.0'
    assert costs['all_attempts']['native_reported']['amount'] == '4.0'
    assert costs['final_attempts']['api_equivalent']['amount'] == '0.000202400000'
    assert costs['all_attempts']['api_equivalent']['amount'] == '0.000404800000'
    assert costs['all_attempts']['api_equivalent']['coverage']['total_attempts'] == 2
    assert costs['all_attempts']['api_equivalent']['coverage']['known_requests'] == 8


def test_no_pricing_remains_not_requested_with_known_native_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of an accepted tariff operation is not an estimated free model call."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    costs = build_cost_summary(RunStore.open(directory))
    assert costs['final_attempts']['native_reported']['amount'] == '2.0'
    assert costs['final_attempts']['api_equivalent']['status'] == 'not_requested'
    assert costs['final_attempts']['api_equivalent']['amount'] is None
    assert costs['final_attempts']['api_equivalent']['coverage']['known_requests'] == 0
    assert costs['final_attempts']['api_equivalent']['coverage']['total_requests'] == 4


def test_no_attempts_have_unknown_amounts_and_explicit_coverage(tmp_path: Path) -> None:
    """A stopped preflight never becomes zero-priced measured inference."""
    costs = build_cost_summary(RunStore.open(empty_run(tmp_path)))
    for scope in costs.values():
        assert scope['native_reported']['status'] == 'not_started'
        assert scope['native_reported']['amount'] is None
        assert scope['api_equivalent']['status'] == 'not_requested'
        assert scope['api_equivalent']['amount'] is None
    assert costs['final_attempts']['native_reported']['coverage']['missing_tasks'] == 1


def test_new_attempt_after_pricing_is_a_current_coverage_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Older selected price receipt coverage cannot claim that a newer native attempt was priced."""
    path = tariffs(tmp_path)
    original = RunStore.reserve

    def reserve(self: RunStore, task_id: str, attempt: dict) -> dict:
        """Freeze and accept first-attempt prices before reserving the subsequent retry."""
        if attempt['sequence'] == 2:
            report = self.snapshot()
            report['run']['status'] = 'interrupted'
            self.commit(report)
            command = Command(self.directory, 'reprice', level='ERROR')
            try:
                request = repricing.freeze_pricing(self.directory, load_policy(path), command, store=self)
                assert repricing.apply_pricing(self.directory, request, command, store=self) == 0
                command.finish('completed')
            finally:
                command.close()
            report = self.snapshot()
            report['run']['status'] = 'running'
            self.commit(report)
        return original(self, task_id, attempt)

    monkeypatch.setattr(RunStore, 'reserve', reserve)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, attempts=2)
    costs = build_cost_summary(RunStore.open(directory))
    assert costs['final_attempts']['api_equivalent']['status'] == 'unavailable'
    assert costs['final_attempts']['api_equivalent']['amount'] is None
    all_prices = costs['all_attempts']['api_equivalent']
    assert all_prices['status'] == 'partial' and all_prices['amount'] is None
    assert all_prices['known_subtotal'] == '0.000202400000'
    assert all_prices['coverage']['known_requests'] == 4 and all_prices['coverage']['total_requests'] == 8


def test_partial_native_and_api_cost_keep_independent_known_subtotals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unknown request prevents full totals while preserving both kinds of known spend."""
    original = executor._Attempt.telemetry

    def mixed(self: executor._Attempt) -> None:
        """Remove one reported cost and use an unpriced model before native sealing."""
        with sqlite3.connect(self.private / 'home/data/opencode/opencode.db') as database:
            message = json.loads(
                database.execute('select data from message where id=?', ('msg-detached',)).fetchone()[0]
            )
            message['modelID'] = 'unpriced'
            database.execute('update message set data=? where id=?', (json.dumps(message), 'msg-detached'))
            part = json.loads(database.execute('select data from part where id=?', ('part-detached',)).fetchone()[0])
            part['cost'] = None
            database.execute('update part set data=? where id=?', (json.dumps(part), 'part-detached'))
        original(self)

    monkeypatch.setattr(executor._Attempt, 'telemetry', mixed)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    assert repricing.reprice(directory, tariffs(tmp_path)) == 1
    costs = build_cost_summary(RunStore.open(directory))['final_attempts']
    assert costs['native_reported']['amount'] is None
    assert costs['native_reported']['known_subtotal'] == '1.5'
    assert costs['native_reported']['coverage']['known_requests'] == 3
    assert costs['api_equivalent']['amount'] is None
    assert costs['api_equivalent']['known_subtotal'] == '0.000151800000'
    assert costs['api_equivalent']['coverage']['known_requests'] == 3
    assert costs['api_equivalent']['coverage']['total_requests'] == 4


def test_incomplete_native_inventory_projects_unknown_without_loading_unowned_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete seal can preserve native outcome while unsigned ledger bytes remain unused."""
    original = executor._Attempt.inventory

    def omit_usage(self: executor._Attempt) -> list[dict]:
        """Leave an explicit usage capture gap in the native producer inventory."""
        return [item for item in original(self) if not item['path'].endswith('/telemetry/usage.json')]

    monkeypatch.setattr(executor._Attempt, 'inventory', omit_usage)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    costs = build_cost_summary(RunStore.open(directory))['final_attempts']
    native = costs['native_reported']
    assert native['status'] == 'unavailable' and native['amount'] is None
    assert native['coverage']['known_attempts'] == 0 and native['coverage']['total_requests'] is None
    assert costs['api_equivalent']['status'] == 'not_requested'


class MemoryCostEvidence:
    """Implement the portable evidence boundary without a filesystem or producer dependency."""

    def __init__(self, *, priced: bool = True) -> None:
        """Bind a single selected seal/usage/result with lossless Decimal pricing strings."""
        self.registry = {'id': 'run'}
        usage = {'path': 'usage', 'size_bytes': 20, 'sha256': 'a' * 64}
        self.state = {
            'tasks': [
                {
                    'id': 'task',
                    'attempt': {'id': 'attempt'},
                    'history': [],
                    'seal': {'path': 'seal'},
                    'pricing': {'id': 'price'} if priced else None,
                }
            ]
        }
        self.documents = {
            'seal': {
                'native_timing': {'started_at': '2026-10-08T12:00:00Z'},
                'task': {'execution': {'status': 'completed'}, 'telemetry': {'usage_ledger': usage}},
                'artifact_receipts': [usage],
            },
            'usage': {
                'run_id': 'run',
                'task_id': 'task',
                'attempt_id': 'attempt',
                'complete': True,
                'requests': [
                    {'complete': True, 'reported_cost_usd': 0.25},
                    {'complete': True, 'reported_cost_usd': 0.5},
                ],
            },
        }
        self.result = {
            'attempts': [
                {
                    'task_id': 'task',
                    'attempt_id': 'attempt',
                    'inputs': {'usage': usage},
                    'result': {
                        'status': 'complete',
                        'known_subtotal': '0.0000000000001234567890123456789',
                        'priced_requests': 2,
                        'total_requests': 2,
                    },
                }
            ]
        }
        self.reads: list[str] = []
        self.usage_validated = False

    def read_artifact(self, receipt: dict) -> dict:
        """Resolve exact documents through the adapter rather than discovering directories."""
        self.reads.append(receipt['path'])
        return self.documents[receipt['path']]

    def price_result(self, selection: dict) -> dict:
        """Return only the explicitly selected and adapter-validated price result."""
        assert selection == {'id': 'price'}
        return self.result

    def validate_usage(self, ledger: dict) -> None:
        """Record the required validation of the signed ledger before any billing arithmetic."""
        assert ledger == self.documents['usage']
        self.usage_validated = True

    def last_operation(self, kind: str) -> dict | None:
        """Expose whether API-equivalent pricing was requested for the exact root selection."""
        assert kind == 'reprice'
        return self.state['tasks'][0]['pricing']


def test_cost_algorithm_uses_only_portable_evidence_methods() -> None:
    """Producer-free evidence reproduces exact native and high-precision estimated billing."""
    evidence = MemoryCostEvidence()
    with localcontext() as context:
        context.prec = 6
        costs = build_cost_summary(evidence)
    assert costs['final_attempts']['native_reported']['amount'] == '0.75'
    assert costs['all_attempts']['api_equivalent']['amount'] == '0.0000000000001234567890123456789'
    assert costs['all_attempts']['api_equivalent']['coverage']['known_requests'] == 2
    assert evidence.reads == ['seal', 'usage'] and evidence.usage_validated


def test_portable_cost_algorithm_never_reads_unsigned_usage() -> None:
    """An unsealed ledger remains unknown even when arbitrary matching bytes are available."""
    evidence = MemoryCostEvidence(priced=False)
    evidence.documents['seal']['artifact_receipts'] = []
    costs = build_cost_summary(evidence)
    assert evidence.reads == ['seal'] and not evidence.usage_validated
    assert costs['all_attempts']['native_reported']['amount'] is None
    assert costs['all_attempts']['api_equivalent']['status'] == 'not_requested'


def test_portable_cost_algorithm_reports_safe_ownership_error() -> None:
    """A selected price cannot substitute a result for another native attempt."""
    evidence = MemoryCostEvidence()
    evidence.result['attempts'][0]['attempt_id'] = 'foreign'
    with pytest.raises(cost_summary.CostEvidenceError) as caught:
        build_cost_summary(evidence)
    assert caught.value.code == 'operation_owner_mismatch'
    assert 'foreign' not in caught.value.message


def test_snapshot_maps_only_portable_cost_error_to_benchmark_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public producer retains safe existing errors while translating portable contract errors."""
    store = RunStore.open(empty_run(tmp_path))
    original = cost_summary.CostEvidenceError('usage_owner_mismatch', 'Cost evidence belongs to another attempt.')

    def fail(_store: object) -> dict:
        """Inject a contract error after ordinary immutable input validation."""
        raise original

    monkeypatch.setattr(run_store, 'build_cost_summary', fail)
    with pytest.raises(BenchmarkError) as caught:
        store.snapshot()
    assert caught.value.code == original.code and caught.value.message == original.message
    assert caught.value.__cause__ is original


def test_snapshot_preserves_original_read_reference_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Transport/digest errors already owned by the producer are not double-wrapped."""
    store = RunStore.open(empty_run(tmp_path))
    original = BenchmarkError('artifact_digest_mismatch', 'Exact artifact bytes changed.')

    def fail(_store: object) -> dict:
        """Expose an existing read-reference error through the portable billing adapter."""
        raise original

    monkeypatch.setattr(run_store, 'build_cost_summary', fail)
    with pytest.raises(BenchmarkError) as caught:
        store.snapshot()
    assert caught.value is original


def test_viewer_vendors_byte_identical_stdlib_cost_contract() -> None:
    """Producer and consumer share exact source bytes without importing each other's runtime."""
    root = Path(__file__).resolve().parents[2]
    producer = root / 'benchmark/src/cost_summary.py'
    consumer = root / 'viewer/src/cost_summary.py'
    assert consumer.read_bytes() == producer.read_bytes()
    assert 'from benchmark' not in producer.read_text() and 'from viewer' not in producer.read_text()


def test_tiny_native_cost_serializes_as_valid_lossless_report_decimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tiny measured native bills use human decimal strings accepted by the actual report contract."""
    original = executor._Attempt.telemetry

    def tiny(self: executor._Attempt) -> None:
        """Store a legitimate tiny provider charge in each real native billing unit before sealing."""
        with sqlite3.connect(self.private / 'home/data/opencode/opencode.db') as database:
            for table in ('message', 'part'):
                for identifier, encoded in database.execute(f'select id, data from {table}').fetchall():
                    value = json.loads(encoded)
                    value['cost'] = 1e-7
                    database.execute(f'update {table} set data=? where id=?', (json.dumps(value), identifier))
        original(self)

    monkeypatch.setattr(executor._Attempt, 'telemetry', tiny)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    report = RunStore.open(directory).snapshot()
    validate_report(report)
    for scope in ('final_attempts', 'all_attempts'):
        native = report['costs'][scope]['native_reported']
        assert native['amount'] == '0.0000004' and native['known_subtotal'] == '0.0000004'


def _verified_runtime(store: RunStore) -> dict:
    """Write a real-shaped root runtime receipt and commit its exact public projection."""
    receipt = {
        'actual_version': '1.18.33',
        'asset_name': 'fixture.tar.gz',
        'platform': 'linux-x64-offline-fixture',
        'archive_sha256': 'a' * 64,
        'binary_sha256': 'b' * 64,
    }
    write_json(store.directory / '_meta/runtime.json', receipt)
    report = store.snapshot()
    report['agent']['resolved_runtime'] = {
        'version': receipt['actual_version'],
        **{key: receipt[key] for key in ('asset_name', 'platform', 'archive_sha256', 'binary_sha256')},
        'receipt': '_meta/runtime.json',
    }
    store.commit(report)
    return receipt


@pytest.mark.parametrize('field', ['version', 'binary_sha256', 'archive_sha256', 'asset_name', 'platform', 'receipt'])
def test_runtime_projection_cannot_forge_state_and_report_together(tmp_path: Path, field: str) -> None:
    """Two agreeing mutable runtime projections cannot substitute for the unchanged raw receipt."""
    directory = empty_run(tmp_path)
    store = RunStore.open(directory)
    _verified_runtime(store)
    original = (directory / '_meta/runtime.json').read_bytes()
    state, report = read_json(directory / '_meta/state.json'), read_json(directory / 'report.json')
    forged = 'c' * 64 if field.endswith('sha256') else ('runtime.json' if field == 'receipt' else 'forged')
    state['resolved_runtime'][field] = report['agent']['resolved_runtime'][field] = forged
    write_json(directory / '_meta/state.json', state)
    write_json(directory / 'report.json', report)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'runtime_receipt_mismatch'
    assert (directory / '_meta/runtime.json').read_bytes() == original


def test_runtime_receipt_and_projection_must_match_frozen_runtime_pin(tmp_path: Path) -> None:
    """An agreeing forged raw version and state cannot override the frozen configuration pin."""
    directory = empty_run(tmp_path)
    store = RunStore.open(directory)
    raw = _verified_runtime(store)
    raw['actual_version'] = '1.18.34'
    write_json(directory / '_meta/runtime.json', raw)
    state = read_json(directory / '_meta/state.json')
    state['resolved_runtime']['version'] = '1.18.34'
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'runtime_receipt_mismatch'


def test_metric_projection_without_selected_rescore_remains_frozen(tmp_path: Path) -> None:
    """A source metric cannot be changed by editing authoritative state without a score operation."""
    directory = empty_run(tmp_path)
    state = read_json(directory / '_meta/state.json')
    state['metric']['implementation_sha256'] = 'f' * 64
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'metric_owner_mismatch'


def test_metric_projection_matches_selected_rescore_result(tmp_path: Path) -> None:
    """A real selected score operation changes the metric while later state-only forgery is rejected."""
    directory = empty_run(tmp_path)
    store = RunStore.open(directory)
    metric = store.snapshot()['metric'].copy()
    metric['implementation_sha256'] = 'e' * 64
    selected = store.prepare_operation(
        'rescore', [], invocation_id=store.state['run']['current_invocation_id'], metric=metric
    )
    store.accept_operation(selected)
    assert RunStore.open(directory).snapshot()['metric'] == metric
    state = read_json(directory / '_meta/state.json')
    state['metric']['implementation_sha256'] = 'f' * 64
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'metric_owner_mismatch'
