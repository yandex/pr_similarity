"""Canonical report construction, independent counters and arithmetic validation."""

from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.models import Report
from benchmark.models.evidence import Diagnostic, Outcome, ScoringEvidence
from benchmark.models.report import ReportMetric
from benchmark.models.state import StateTaskItem
from benchmark.persistence.artifact_refs import read_reference
from benchmark.persistence.operations import validate_operation
from benchmark.persistence.run_store import RunStore
from benchmark.presentation.result import pending_task as pending_task
from benchmark.presentation.result import summary as summary
from benchmark.presentation.result import unavailable_scoring as unavailable_scoring
from benchmark.presentation.result import validate_report as validate_report

__all__ = ['fallback_statistics', 'pending_task', 'read_report', 'summary', 'unavailable_scoring', 'validate_report']


@dataclass(frozen=True)
class FallbackStatistics:
    """Distinguish counted fallback diagnostics from unavailable evidence."""

    files: int | None
    tasks: int | None
    unknown_tasks: int


def read_report(run_dir: Path) -> Report:
    """Read the authoritative projection even when its report cache is absent or stale."""
    report = RunStore.open(run_dir).snapshot()
    validate_report(report)
    return report


def _diagnostics(store: RunStore, selected: StateTaskItem, metric: ReportMetric) -> list[Diagnostic] | None:
    """Follow the selected score operation or exact initial scoring receipt."""
    if selected.scoring is not None:
        operation = validate_operation(store.directory, store.registry, selected.scoring, 'rescore')
        if operation.result.metric != metric:
            return None
        assert selected.attempt is not None
        matches = [
            item
            for item in operation.result.tasks or []
            if item.task_id == selected.id and item.attempt_id == selected.attempt.id
        ]
        return matches[0].diagnostics if len(matches) == 1 else None
    if selected.outcome is None:
        return None
    outcome = read_reference(store.directory, selected.outcome, Outcome)
    receipt = outcome.scoring_result
    if receipt is None:
        return None
    result = read_reference(store.directory, receipt, ScoringEvidence)
    return result.diagnostics if result.metric == metric else None


def fallback_statistics(run_dir: Path, report: Report) -> FallbackStatistics:
    """Count task-owned fallback files from explicitly selected immutable score records."""
    store = RunStore.open(run_dir)
    files = tasks = unknown = 0
    for row, selected in zip(report.tasks, store.state.tasks, strict=True):
        if row.id != selected.id:
            raise BenchmarkError('report_owner_mismatch', 'Diagnostics report differs from the frozen task queue.')
        if row.scoring.status != 'available':
            continue
        diagnostics = _diagnostics(store, selected, report.metric)
        if diagnostics is None:
            unknown += 1
            continue
        paths = {item.path for item in diagnostics if item.code == 'codebleu_fallback'}
        files += len(paths)
        tasks += bool(paths)
    return FallbackStatistics(files=None if unknown else files, tasks=None if unknown else tasks, unknown_tasks=unknown)
