"""Rescore chooses exact immutable operations and keeps native evidence unchanged."""

from pathlib import Path

import pytest
from test_operation_workflows import stopped_run

from benchmark.errors import BenchmarkError
from benchmark.metrics import rescore as module
from benchmark.models.evidence import ArtifactReceipt, OperationResult
from benchmark.models.report import ReportTaskScoring
from benchmark.models.scoring import Diagnostic
from benchmark.models.triplets import TripletReferences
from benchmark.persistence import run_store
from benchmark.persistence.artifact_refs import read_reference
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json
from benchmark.persistence.triplets import TripletStates
from benchmark.runtime import attempt as executor


def test_rescore_selects_complete_task_results_and_preserves_native_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host scores can replace a failed original score without editing its native outcome."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    before = RunStore.open(directory).snapshot()
    selected = RunStore.open(directory).state.tasks[0]
    assert selected.outcome is not None
    outcome_path = directory / selected.outcome.path
    native = outcome_path.read_bytes()
    assert module.rescore(directory) == 0
    store = RunStore.open(directory)
    current = store.snapshot()
    operation = store.last_operation('rescore')
    assert operation is not None
    result = read_reference(directory, operation.result, OperationResult)
    row = current.tasks[0]
    assert result.tasks is not None
    assert result.tasks[0].scoring == row.scoring
    assert result.metric == current.metric
    assert row.scoring.status == 'available'
    assert row.execution == before.tasks[0].execution and row.telemetry == before.tasks[0].telemetry
    assert row.final_attempt == before.tasks[0].final_attempt
    assert outcome_path.read_bytes() == native
    assert not (directory / 'rescoring').exists() and not list(directory.glob('report_backup_*.json'))


def test_score_failure_records_failed_operation_and_preserves_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed host result cannot become accepted state or cause a native retry."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    before = (directory / '_meta/state.json').read_bytes()
    report = (directory / 'report.json').read_bytes()

    def fail(*args: object, **kwargs: object) -> dict:
        """Inject a CPU scoring defect after all native owners were validated."""
        raise BenchmarkError('scoring_failed', 'Fixture parser failure.')

    monkeypatch.setattr(module, 'score_pr', fail)
    with pytest.raises(BenchmarkError):
        module.rescore(directory)
    assert (directory / '_meta/state.json').read_bytes() == before
    assert (directory / 'report.json').read_bytes() == report
    receipts = [read_json(path) for path in (directory / 'operations/rescore').glob('*/receipt.json')]
    assert len(receipts) == 1 and receipts[0]['status'] == 'failed'
    assert RunStore.open(directory).last_operation('rescore') is None


def test_rescore_fallback_diagnostics_keep_task_and_role_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same source path labels cannot erase task-bound scoring diagnostics."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    original = module.score_pr

    def diagnosed(
        triplets: dict[str, TripletStates],
        *,
        artifact_prefix: str = 'triplets',
        diagnostics: list[Diagnostic] | None = None,
        artifact_refs: dict[str, TripletReferences] | None = None,
    ) -> ReportTaskScoring:
        """Retain real scoring and add a controlled per-file fallback observation."""
        result = original(
            triplets, artifact_prefix=artifact_prefix, diagnostics=diagnostics, artifact_refs=artifact_refs
        )
        assert diagnostics is not None
        diagnostics.append(Diagnostic(code='codebleu_fallback', message='Fixture fallback.', path='new/source.py'))
        return result

    monkeypatch.setattr(module, 'score_pr', diagnosed)
    assert module.rescore(directory) == 0
    store = RunStore.open(directory)
    selected = store.last_operation('rescore')
    assert selected is not None
    result = read_reference(directory, selected.result, OperationResult)
    assert result.tasks is not None
    row = result.tasks[0]
    assert row.task_id == store.registry.tasks[0].id
    assert row.diagnostics[0].code == 'codebleu_fallback'
    assert row.scoring.files[0].ai is not None
    assert row.scoring.files[0].ai.endswith('/triplets/001/new/source_agent.py')


def test_selected_state_survives_report_failure_and_rebuilds_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once state commits, recovery rebuilds its selected operation and ignores newer directories."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    original = run_store.write_json

    def fail_report(path: Path, value: dict, **kwargs: object) -> None:
        """Fail the derived projection after accepting the rescore operation in state."""
        if path == directory / 'report.json' and RunStore.open(directory).last_operation('rescore') is not None:
            raise BenchmarkError('artifact_write_failed', 'Fixture report interruption.')
        original(path, value)

    monkeypatch.setattr(run_store, 'write_json', fail_report)
    with pytest.raises(BenchmarkError):
        module.rescore(directory)
    monkeypatch.setattr(run_store, 'write_json', original)
    store = RunStore.open(directory)
    assert store.last_operation('rescore') is not None
    store.rebuild()
    assert read_json(directory / 'report.json') == store.snapshot().to_wire()


def test_missing_original_role_receipt_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest alone cannot authorize rescoring source not owned by the native seal."""
    original = executor._Attempt.inventory

    def omit_role(self: executor._Attempt) -> list[ArtifactReceipt]:
        """Simulate a broken producer that omitted the agent role from its sealed inventory."""
        return [item for item in original(self) if not item.path.endswith('/triplets/001/new/source_agent.py')]

    monkeypatch.setattr(executor._Attempt, 'inventory', omit_role)
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch)
    before = (directory / '_meta/state.json').read_bytes()
    with pytest.raises(BenchmarkError) as error:
        module.rescore(directory)
    assert error.value.code == 'triplet_receipt_missing'
    assert (directory / '_meta/state.json').read_bytes() == before
