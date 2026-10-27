"""Cost projections follow registered native facts and each selected pricing result."""

import json
import sqlite3
import subprocess
from decimal import localcontext
from pathlib import Path

import pytest
import yaml
from test_operation_workflows import empty_run, stopped_run
from test_pricing import policy

from benchmark.errors import BenchmarkError
from benchmark.models import Report, State, Usage, WireModel
from benchmark.models.evidence import (
    ArtifactReceipt,
    OperationResult,
    OperationSelection,
    PriceInputs,
    PriceItem,
    PriceResult,
    Seal,
)
from benchmark.models.report import (
    ReportAgentResolvedRuntime,
    ReportAttempt,
    ReportTelemetryApiEquivalent,
)
from benchmark.persistence import run_store
from benchmark.persistence.artifact_refs import read_reference
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json, write_json
from benchmark.presentation.result import validate_report
from benchmark.pricing import operations as repricing
from benchmark.pricing import summary as cost_summary
from benchmark.pricing.policy import load_policy
from benchmark.pricing.summary import build_cost_summary
from benchmark.runtime import attempt as executor
from benchmark.runtime.commands import Command


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
    assert costs.final_attempts.native_reported.amount == '2.0'
    assert costs.all_attempts.native_reported.amount == '4.0'
    assert costs.final_attempts.api_equivalent.amount == '0.000202400000'
    assert costs.all_attempts.api_equivalent.amount == '0.000404800000'
    assert costs.all_attempts.api_equivalent.coverage.total_attempts == 2
    assert costs.all_attempts.api_equivalent.coverage.known_requests == 8


def test_no_pricing_remains_not_requested_with_known_native_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of an accepted tariff operation is not an estimated free model call."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    costs = build_cost_summary(RunStore.open(directory))
    assert costs.final_attempts.native_reported.amount == '2.0'
    assert costs.final_attempts.api_equivalent.status == 'not_requested'
    assert costs.final_attempts.api_equivalent.amount is None
    assert costs.final_attempts.api_equivalent.coverage.known_requests == 0
    assert costs.final_attempts.api_equivalent.coverage.total_requests == 4


def test_no_attempts_have_unknown_amounts_and_explicit_coverage(tmp_path: Path) -> None:
    """A stopped preflight never becomes zero-priced measured inference."""
    costs = build_cost_summary(RunStore.open(empty_run(tmp_path)))
    for scope in (costs.final_attempts, costs.all_attempts):
        assert scope.native_reported.status == 'not_started'
        assert scope.native_reported.amount is None
        assert scope.api_equivalent.status == 'not_requested'
        assert scope.api_equivalent.amount is None
    assert costs.final_attempts.native_reported.coverage.missing_tasks == 1


def test_new_attempt_after_pricing_is_a_current_coverage_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Older selected price receipt coverage cannot claim that a newer native attempt was priced."""
    path = tariffs(tmp_path)
    original = RunStore.reserve

    def reserve(self: RunStore, task_id: str, attempt: ReportAttempt) -> Report:
        """Freeze and accept first-attempt prices before reserving the subsequent retry."""
        if attempt.sequence == 2:
            report = self.snapshot()
            report.run.status = 'interrupted'
            self.commit(report)
            command = Command(self.directory, 'reprice', level='ERROR')
            try:
                request = repricing.freeze_pricing(self.directory, load_policy(path), command, store=self)
                assert repricing.apply_pricing(self.directory, request, command, store=self) == 0
                command.finish('completed')
            finally:
                command.close()
            report = self.snapshot()
            report.run.status = 'running'
            self.commit(report)
        return original(self, task_id, attempt)

    monkeypatch.setattr(RunStore, 'reserve', reserve)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, attempts=2)
    costs = build_cost_summary(RunStore.open(directory))
    assert costs.final_attempts.api_equivalent.status == 'unavailable'
    assert costs.final_attempts.api_equivalent.amount is None
    all_prices = costs.all_attempts.api_equivalent
    assert all_prices.status == 'partial' and all_prices.amount is None
    assert all_prices.known_subtotal == '0.000202400000'
    assert all_prices.coverage.known_requests == 4 and all_prices.coverage.total_requests == 8


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
    costs = build_cost_summary(RunStore.open(directory)).final_attempts
    assert costs.native_reported.amount is None
    assert costs.native_reported.known_subtotal == '1.5'
    assert costs.native_reported.coverage.known_requests == 3
    assert costs.api_equivalent.amount is None
    assert costs.api_equivalent.known_subtotal == '0.000151800000'
    assert costs.api_equivalent.coverage.known_requests == 3
    assert costs.api_equivalent.coverage.total_requests == 4


def test_incomplete_native_inventory_projects_unknown_without_loading_unowned_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete seal can preserve native outcome while unsigned ledger bytes remain unused."""
    original = executor._Attempt.inventory

    def omit_usage(self: executor._Attempt) -> list[ArtifactReceipt]:
        """Leave an explicit usage capture gap in the native producer inventory."""
        return [item for item in original(self) if not item.path.endswith('/telemetry/usage.json')]

    monkeypatch.setattr(executor._Attempt, 'inventory', omit_usage)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    costs = build_cost_summary(RunStore.open(directory)).final_attempts
    native = costs.native_reported
    assert native.status == 'unavailable' and native.amount is None
    assert native.coverage.known_attempts == 0 and native.coverage.total_requests is None
    assert costs.api_equivalent.status == 'not_requested'


class MemoryCostEvidence:
    """Detach typed native facts from filesystem access for cost projection checks."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, priced: bool = True) -> None:
        """Seed complete current-format objects, then isolate exact synthetic monetary facts."""
        directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
        store = RunStore.open(directory)
        self.registry, self.state = store.registry, store.state
        task = self.state.tasks[0]
        assert task.seal is not None and task.attempt is not None
        self.native = read_reference(directory, task.seal, Seal)
        ledger_ref = self.native.task.telemetry.usage_ledger
        assert ledger_ref is not None
        usage_ref = next(item for item in self.native.artifact_receipts if item.path == ledger_ref.path)
        self.usage = read_reference(directory, usage_ref, Usage)
        self.usage.requests = self.usage.requests[:2]
        self.usage.requests[0].reported_cost_usd = 0.25
        self.usage.requests[1].reported_cost_usd = 0.5
        self.usage.complete = True
        self.documents: dict[str, WireModel] = {task.seal.path: self.native, usage_ref.path: self.usage}
        operation_id = '44444444-4444-4444-8444-444444444444'
        selected = OperationSelection(
            id=operation_id,
            kind='reprice',
            accepted_revision=1,
            result=ArtifactReceipt(path='result.json', size_bytes=0, sha256='a' * 64),
            receipt=ArtifactReceipt(path='receipt.json', size_bytes=0, sha256='b' * 64),
        )
        task.pricing = selected if priced else None
        self.result = OperationResult(
            schema_family='pr_similarity_os.operation_result',
            schema_version=2,
            id=operation_id,
            kind='reprice',
            run_id=self.registry.id,
            basket_sha256=self.registry.baseline.basket.sha256,
            config_identity_sha256=self.registry.baseline.config.identity_sha256,
            metric=None,
            attempts=[
                PriceItem(
                    task_id=task.id,
                    attempt_id=task.attempt.id,
                    sequence=task.attempt.sequence,
                    inputs=PriceInputs(usage=usage_ref, reason=None, receipt=None, db=None),
                    result=PriceResult(
                        status='complete',
                        amount='0.0000000000001234567890123456789',
                        known_subtotal='0.0000000000001234567890123456789',
                        priced_requests=2,
                        total_requests=2,
                        requests=[],
                    ),
                    api_equivalent=ReportTelemetryApiEquivalent(
                        status='complete',
                        amount=0.0000000000001234567890123456789,
                        currency='USD',
                        basis='api_equivalent',
                        calculation_id=operation_id,
                        pricing_sha256='c' * 64,
                        calculator_version='api-equivalent-v1',
                        usage_sha256=usage_ref.sha256,
                        priced_requests=2,
                        total_requests=2,
                    ),
                )
            ],
        )
        self.reads: list[str] = []
        self.usage_validated = False

    def read_artifact[T: WireModel](self, receipt: ArtifactReceipt, model: type[T]) -> T:
        """Resolve only an exact selected document through the typed adapter."""
        document = self.documents[receipt.path]
        assert isinstance(document, model)
        self.reads.append('seal' if isinstance(document, Seal) else 'usage')
        return document

    def price_result(self, selection: OperationSelection) -> OperationResult:
        """Return only the explicitly selected price result."""
        assert selection == self.state.tasks[0].pricing
        return self.result

    def validate_usage(self, ledger: Usage) -> None:
        """Record required validation before native billing arithmetic."""
        assert ledger is self.usage
        self.usage_validated = True

    def last_operation(self, kind: str) -> OperationSelection | None:
        """Expose whether the exact root pricing selection exists."""
        assert kind == 'reprice'
        return self.state.tasks[0].pricing


def test_cost_algorithm_uses_only_portable_evidence_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Producer-free evidence reproduces exact native and high-precision estimated billing."""
    evidence = MemoryCostEvidence(tmp_path, monkeypatch)
    with localcontext() as context:
        context.prec = 6
        costs = build_cost_summary(evidence)
    assert costs.final_attempts.native_reported.amount == '0.75'
    assert costs.all_attempts.api_equivalent.amount == '0.0000000000001234567890123456789'
    assert costs.all_attempts.api_equivalent.coverage.known_requests == 2
    assert evidence.reads == ['seal', 'usage'] and evidence.usage_validated


def test_portable_cost_algorithm_never_reads_unsigned_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsealed ledger remains unknown even when arbitrary matching bytes are available."""
    evidence = MemoryCostEvidence(tmp_path, monkeypatch, priced=False)
    evidence.native.artifact_receipts = []
    costs = build_cost_summary(evidence)
    assert evidence.reads == ['seal'] and not evidence.usage_validated
    assert costs.all_attempts.native_reported.amount is None
    assert costs.all_attempts.api_equivalent.status == 'not_requested'


def test_portable_cost_algorithm_reports_safe_ownership_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A selected price cannot substitute a result for another native attempt."""
    evidence = MemoryCostEvidence(tmp_path, monkeypatch)
    assert evidence.result.attempts is not None
    evidence.result.attempts[0].attempt_id = 'foreign'
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


def test_viewer_and_producer_cost_outputs_conform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent runtimes compute identical costs from the same saved native facts."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, attempts=2)
    assert repricing.reprice(directory, tariffs(tmp_path)) == 0
    expected = build_cost_summary(RunStore.open(directory)).to_wire()
    viewer = Path(__file__).resolve().parents[2] / 'viewer'
    script = '''import json,sys
from pathlib import Path
from viewer.models import Run,State
from viewer.models.evidence import ArtifactReceipt
from viewer.validation.operations import OperationStore
from viewer.validation.contracts import validate_report
from viewer.presentation.cost_summary import build_cost_summary
from viewer.util import digest
root=Path(sys.argv[1])
inventory={}
for path in root.rglob('*'):
 if path.is_file():
  data=path.read_bytes(); name=path.relative_to(root).as_posix()
  inventory[name]=ArtifactReceipt(path=name,size_bytes=len(data),sha256=digest(data))
store=OperationStore(
 Run.model_validate_json((root/'run.json').read_bytes()),
 State.model_validate_json((root/'_meta/state.json').read_bytes()),
 inventory,lambda name:(root/name).read_bytes(),validate_report)
print(json.dumps(build_cost_summary(store).to_wire(),sort_keys=True))
'''
    result = subprocess.run(
        [str(viewer / '.venv/bin/python'), '-c', script, str(directory)],
        cwd=viewer,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == expected


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
    for scope in (report.costs.final_attempts, report.costs.all_attempts):
        native = scope.native_reported
        assert native.amount == '0.0000004' and native.known_subtotal == '0.0000004'


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
    report.agent.resolved_runtime = ReportAgentResolvedRuntime.model_validate(
        {
            'version': receipt['actual_version'],
            **{key: receipt[key] for key in ('asset_name', 'platform', 'archive_sha256', 'binary_sha256')},
            'receipt': '_meta/runtime.json',
        }
    )
    store.commit(report)
    return receipt


@pytest.mark.parametrize('field', ['version', 'binary_sha256', 'archive_sha256', 'asset_name', 'platform', 'receipt'])
def test_runtime_projection_cannot_forge_state_and_report_together(tmp_path: Path, field: str) -> None:
    """Two agreeing mutable runtime projections cannot substitute for the unchanged raw receipt."""
    directory = empty_run(tmp_path)
    store = RunStore.open(directory)
    _verified_runtime(store)
    original = (directory / '_meta/runtime.json').read_bytes()
    state = State.model_validate(read_json(directory / '_meta/state.json'))
    report = Report.model_validate(read_json(directory / 'report.json'))
    forged = 'c' * 64 if field.endswith('sha256') else ('runtime.json' if field == 'receipt' else 'forged')
    assert state.resolved_runtime is not None and report.agent.resolved_runtime is not None
    setattr(state.resolved_runtime, field, forged)
    setattr(report.agent.resolved_runtime, field, forged)
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
    state = State.model_validate(read_json(directory / '_meta/state.json'))
    assert state.resolved_runtime is not None
    state.resolved_runtime.version = '1.18.34'
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'runtime_receipt_mismatch'


def test_metric_projection_without_selected_rescore_remains_frozen(tmp_path: Path) -> None:
    """A source metric cannot be changed by editing authoritative state without a score operation."""
    directory = empty_run(tmp_path)
    state = State.model_validate(read_json(directory / '_meta/state.json'))
    state.metric.version = 'f' * 64
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'metric_owner_mismatch'


def test_metric_projection_matches_selected_rescore_result(tmp_path: Path) -> None:
    """A real selected score operation changes the metric while later state-only forgery is rejected."""
    directory = empty_run(tmp_path)
    store = RunStore.open(directory)
    metric = store.snapshot().metric.model_copy()
    metric.version = 'fixture-metric-v2'
    selected = store.prepare_operation(
        'rescore', [], invocation_id=store.state.run.current_invocation_id, metric=metric
    )
    store.accept_operation(selected)
    assert RunStore.open(directory).snapshot().metric == metric
    state = State.model_validate(read_json(directory / '_meta/state.json'))
    state.metric.version = 'f' * 64
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory)
    assert caught.value.code == 'metric_owner_mismatch'
