"""Repricing selects exact operations and preserves Decimal coverage and native evidence."""

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from benchmark import attempt as executor
from benchmark import repricing as module
from benchmark import run_store
from benchmark.artifact_refs import read_reference
from benchmark.commands import Command
from benchmark.errors import BenchmarkError
from benchmark.pricing import load_policy
from benchmark.run_store import RunStore
from benchmark.storage import read_json, run_lock
from test_operation_workflows import empty_run, stopped_run
from test_pricing import policy


def make_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a current-format stopped run and a compatible frozen tariff policy."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    data = policy()
    data['models'][0].update(provider='provider', model='model')
    path = tmp_path / 'pricing.yaml'
    path.write_text(yaml.safe_dump(data))
    return directory, path


def test_two_reprices_keep_immutable_operations_and_native_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed tariffs replace only accepted estimates and retain the previous input projection."""
    directory, path = make_run(tmp_path, monkeypatch)
    before = read_json(directory / 'report.json')
    assert module.reprice(directory, path) == 0
    first = read_json(directory / 'report.json')
    assert first['tasks'][0]['telemetry']['api_equivalent']['amount'] == pytest.approx(0.0002024)
    data = yaml.safe_load(path.read_text())
    data['models'][0]['rates']['input'] = '3'
    path.write_text(yaml.safe_dump(data))
    assert module.reprice(directory, path) == 0
    second = read_json(directory / 'report.json')
    assert second['tasks'][0]['telemetry']['api_equivalent']['amount'] == pytest.approx(0.0002424)
    assert second['tasks'][0]['telemetry']['cost'] == before['tasks'][0]['telemetry']['cost']
    assert second['tasks'][0]['scoring'] == before['tasks'][0]['scoring']
    store = RunStore.open(directory)
    selected = store.last_operation('reprice')
    assert selected is not None
    receipt = read_reference(directory, selected['receipt'])
    assert read_reference(directory, receipt['input_report']) == first
    result = read_reference(directory, selected['result'])
    assert result['attempts'][0]['result']['known_subtotal'] == '0.000242400000'
    assert not (directory / 'repricing').exists() and not list(directory.glob('report_backup_*.json'))


def test_same_semantic_inputs_record_noop_without_state_or_report_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new command can be audited while the already accepted price selection remains intact."""
    directory, path = make_run(tmp_path, monkeypatch)
    assert module.reprice(directory, path) == 0
    report = (directory / 'report.json').read_bytes()
    state = (directory / '_meta/state.json').read_bytes()
    selected = RunStore.open(directory).last_operation('reprice')
    path.write_text('# equivalent formatting\n' + path.read_text())
    assert module.reprice(directory, path) == 0
    assert (directory / 'report.json').read_bytes() == report
    assert (directory / '_meta/state.json').read_bytes() == state
    assert RunStore.open(directory).last_operation('reprice') == selected
    receipts = [read_json(item) for item in (directory / 'operations/reprice').glob('*/receipt.json')]
    assert sorted(receipt['status'] for receipt in receipts) == ['completed', 'no_op']


def test_usage_corruption_is_rejected_before_command_or_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unverified native usage never becomes an operation input or triggers a repair inference."""
    directory, path = make_run(tmp_path, monkeypatch)
    content = (directory / '_meta/state.json').read_bytes()
    invocations = set((directory / '_meta/invocations').iterdir())
    row = RunStore.open(directory).snapshot()['tasks'][0]
    ledger = directory / row['telemetry']['usage_ledger']['path']
    ledger.write_bytes(ledger.read_bytes() + b'\\n')
    with pytest.raises(BenchmarkError):
        module.reprice(directory, path)
    assert (directory / '_meta/state.json').read_bytes() == content
    assert set((directory / '_meta/invocations').iterdir()) == invocations


def test_unknown_model_has_null_total_and_exact_known_subtotal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpriced usage preserves native billing and reports an explicit coverage gap."""
    directory, path = make_run(tmp_path, monkeypatch)
    data = yaml.safe_load(path.read_text())
    data['models'][0]['model'] = 'unpriced'
    path.write_text(yaml.safe_dump(data))
    assert module.reprice(directory, path) == 1
    store = RunStore.open(directory)
    selected = store.last_operation('reprice')
    assert selected is not None
    result = read_reference(directory, selected['result'])
    row = store.snapshot()['tasks'][0]
    assert row['telemetry']['api_equivalent']['amount'] is None
    assert row['telemetry']['cost']['amount'] == 2
    assert result['attempts'][0]['result']['known_subtotal'] == '0E-12'


def test_preallocated_pricing_reuses_outer_lease_and_freezes_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Policy freezing can precede native work, while input state is captured only at completion."""
    directory, path = make_run(tmp_path, monkeypatch)
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        command = Command(directory, 'reprice')
        try:
            request = module.freeze_pricing(directory, load_policy(path), command, store=store)
            operation = directory / 'operations/reprice' / request['calculation_id']
            assert (operation / 'request.json').is_file() and not (operation / 'input_state.json').exists()
            assert module.apply_pricing(directory, request, command, store=store) == 0
            assert (operation / 'input_state.json').is_file()
        finally:
            command.close()


def test_reprice_prices_exact_current_and_history_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempt history has independent selected prices and contributes to exact all-attempt cost."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, attempts=2)
    data = policy()
    data['models'][0].update(provider='provider', model='model')
    path = tmp_path / 'pricing.yaml'
    path.write_text(yaml.safe_dump(data))
    assert module.reprice(directory, path) == 0
    store = RunStore.open(directory)
    selected = store.last_operation('reprice')
    assert selected is not None
    result = read_reference(directory, selected['result'])
    receipt = read_reference(directory, selected['receipt'])
    assert [item['sequence'] for item in result['attempts']] == [1, 2]
    assert receipt['details']['coverage']['all_attempts']['known_subtotal'] == '0.000404800000'
    assert receipt['details']['coverage']['final']['known_subtotal'] == '0.000202400000'
    assert store.state['tasks'][0]['history'][0]['pricing'] == selected
    assert store.state['tasks'][0]['pricing'] == selected


def test_partial_pricing_keeps_positive_known_subtotal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single unpriced request makes the amount unknown without discarding priced units."""
    original = executor._Attempt.telemetry

    def mixed(self: executor._Attempt) -> None:
        """Change one private native request model before its immutable capture."""
        with sqlite3.connect(self.private / 'home/data/opencode/opencode.db') as database:
            value = json.loads(database.execute('select data from message where id=?', ('msg-detached',)).fetchone()[0])
            value['modelID'] = 'unpriced'
            database.execute('update message set data=? where id=?', (json.dumps(value), 'msg-detached'))
        original(self)

    monkeypatch.setattr(executor._Attempt, 'telemetry', mixed)
    directory, path = make_run(tmp_path, monkeypatch)
    assert module.reprice(directory, path) == 1
    store = RunStore.open(directory)
    selected = store.last_operation('reprice')
    assert selected is not None
    result = read_reference(directory, selected['result'])['attempts'][0]['result']
    assert result['status'] == 'partial' and result['amount'] is None
    assert result['known_subtotal'] == '0.000151800000'
    assert result['priced_requests'] == 3 and result['total_requests'] == 4
    assert store.snapshot()['tasks'][0]['telemetry']['cost']['amount'] == 2


def test_abandoned_complete_operation_is_not_adopted_on_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed but unselected result cannot override state on a later pricing command."""
    directory, path = make_run(tmp_path, monkeypatch)
    report = (directory / 'report.json').read_bytes()
    state = (directory / '_meta/state.json').read_bytes()
    original = RunStore.accept_operation

    def reject(self: RunStore, selection: dict) -> dict:
        """Stop immediately before authoritative selection to leave a complete orphan."""
        raise BenchmarkError('fixture_commit_failed', 'Fixture stopped before operation commit.')

    monkeypatch.setattr(RunStore, 'accept_operation', reject)
    with pytest.raises(BenchmarkError):
        module.reprice(directory, path)
    assert (directory / 'report.json').read_bytes() == report
    assert (directory / '_meta/state.json').read_bytes() == state
    receipts = list((directory / 'operations/reprice').glob('*/receipt.json'))
    assert len(receipts) == 1 and read_json(receipts[0])['status'] == 'completed'
    frozen_receipt = receipts[0].read_bytes()
    RunStore.open(directory).rebuild()
    assert (directory / 'report.json').read_bytes() == report
    assert RunStore.open(directory).last_operation('reprice') is None
    monkeypatch.setattr(RunStore, 'accept_operation', original)
    assert module.reprice(directory, path) == 0
    accepted = RunStore.open(directory).last_operation('reprice')
    assert accepted is not None
    assert accepted['id'] != receipts[0].parent.name
    assert receipts[0].read_bytes() == frozen_receipt


def test_pricing_state_commit_survives_derived_report_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The accepted price remains authoritative even when its report cache cannot be saved."""
    directory, path = make_run(tmp_path, monkeypatch)
    original = run_store.write_json

    def fail_report(target: Path, value: dict, **kwargs: object) -> None:
        """Reject only the report write that follows authoritative operation selection."""
        if target == directory / 'report.json' and RunStore.open(directory).last_operation('reprice') is not None:
            raise BenchmarkError('artifact_write_failed', 'Fixture projection failed after state commit.')
        original(target, value)

    monkeypatch.setattr(run_store, 'write_json', fail_report)
    with pytest.raises(BenchmarkError):
        module.reprice(directory, path)
    monkeypatch.setattr(run_store, 'write_json', original)
    store = RunStore.open(directory)
    selected = store.last_operation('reprice')
    assert selected is not None
    store.rebuild()
    assert store.snapshot()['tasks'][0]['telemetry']['api_equivalent']['amount'] == pytest.approx(0.0002024)
    assert read_json(directory / 'report.json') == store.snapshot()


def test_damaged_calculator_closes_failed_operation_without_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frozen calculator corruption cannot silently use current code or replace accepted prices."""
    directory, path = make_run(tmp_path, monkeypatch)
    before = (directory / '_meta/state.json').read_bytes()
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        command = Command(directory, 'reprice')
        try:
            request = module.freeze_pricing(directory, load_policy(path), command, store=store)
            (directory / request['calculator_path'] / 'pricing.py').write_text('damaged')
            with pytest.raises(BenchmarkError):
                module.apply_pricing(directory, request, command, store=store)
            receipt = directory / 'operations/reprice' / request['calculation_id'] / 'receipt.json'
            assert read_json(receipt)['status'] == 'failed'
        finally:
            command.close()
    assert (directory / '_meta/state.json').read_bytes() == before


def test_missing_sealed_usage_is_not_recovered_from_file_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unowned ledger bytes are not pricing inputs even if they lie beside a valid receipt."""
    original = executor._Attempt.inventory

    def omit_usage(self: executor._Attempt) -> list[dict]:
        """Simulate a producer that left the ledger out of its explicit seal inventory."""
        return [item for item in original(self) if not item['path'].endswith('/telemetry/usage.json')]

    monkeypatch.setattr(executor._Attempt, 'inventory', omit_usage)
    directory, path = make_run(tmp_path, monkeypatch)
    before = (directory / '_meta/state.json').read_bytes()
    with pytest.raises(BenchmarkError) as error:
        module.reprice(directory, path)
    assert error.value.code == 'usage_receipt_missing'
    assert (directory / '_meta/state.json').read_bytes() == before


def test_zero_attempt_reprice_has_exact_root_selection_and_noop(tmp_path: Path) -> None:
    """Even an unrun basket retains a verifiable selected operation for repeated pricing."""
    directory = empty_run(tmp_path)
    path = tmp_path / 'pricing.yaml'
    path.write_text(yaml.safe_dump(policy()))
    assert module.reprice(directory, path) == 1
    selected = RunStore.open(directory).last_operation('reprice')
    assert selected is not None
    assert read_reference(directory, selected['result'])['attempts'] == []
    before = (directory / '_meta/state.json').read_bytes()
    report = (directory / 'report.json').read_bytes()
    assert module.reprice(directory, path) == 1
    assert (directory / '_meta/state.json').read_bytes() == before
    assert (directory / 'report.json').read_bytes() == report
    assert RunStore.open(directory).last_operation('reprice') == selected
