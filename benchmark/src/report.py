"""Canonical report construction, independent counters and arithmetic validation."""

from pathlib import Path

from benchmark.artifact_refs import read_reference
from benchmark.errors import BenchmarkError
from benchmark.operations import validate_operation
from benchmark.result import pending_task as pending_task
from benchmark.result import summary as summary
from benchmark.result import unavailable_scoring as unavailable_scoring
from benchmark.result import validate_report as validate_report
from benchmark.run_store import RunStore

__all__ = ['fallback_statistics', 'pending_task', 'read_report', 'summary', 'unavailable_scoring', 'validate_report']


def read_report(run_dir: Path) -> dict:
    """Read the authoritative projection even when its report cache is absent or stale."""
    report = RunStore.open(run_dir).snapshot()
    validate_report(report)
    return report


def _diagnostics(store: RunStore, selected: dict, metric: dict) -> list[dict] | None:
    """Follow the selected score operation or exact initial scoring receipt."""
    if selected['scoring'] is not None:
        operation = validate_operation(store.directory, store.registry, selected['scoring'], 'rescore')
        if operation.result['metric'] != metric:
            return None
        matches = [
            item
            for item in operation.result['tasks']
            if item['task_id'] == selected['id'] and item['attempt_id'] == selected['attempt']['id']
        ]
        return matches[0].get('diagnostics') if len(matches) == 1 else None
    if selected['outcome'] is None:
        return None
    outcome = read_reference(store.directory, selected['outcome'])
    receipt = outcome.get('scoring_result')
    if receipt is None:
        return None
    result = read_reference(store.directory, receipt)
    return result.get('diagnostics') if result.get('metric') == metric else None


def fallback_statistics(run_dir: Path, report: dict) -> dict:
    """Count task-owned fallback files from explicitly selected immutable score records."""
    store = RunStore.open(run_dir)
    files = tasks = unknown = 0
    for row, selected in zip(report['tasks'], store.state['tasks'], strict=True):
        if row['id'] != selected['id']:
            raise BenchmarkError('report_owner_mismatch', 'Diagnostics report differs from the frozen task queue.')
        if row['scoring']['status'] != 'available':
            continue
        diagnostics = _diagnostics(store, selected, report['metric'])
        if not isinstance(diagnostics, list):
            unknown += 1
            continue
        paths = {item['path'] for item in diagnostics if item.get('code') == 'codebleu_fallback'}
        files += len(paths)
        tasks += bool(paths)
    return {'files': None if unknown else files, 'tasks': None if unknown else tasks, 'unknown_tasks': unknown}
