"""Verify Python ownership without requiring any installed agent runtime."""

from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.models.base import ArtifactReceipt
from benchmark.models.evidence import Outcome, Seal
from benchmark.models.runtime import PythonAttemptReceipt, PythonReceipt
from benchmark.persistence.artifact_refs import read_reference


def validate_python_fact(directory: Path, fact: Seal | Outcome, runtime: ArtifactReceipt | None, prefix: str) -> None:
    """Bind the shared runtime and available attempt probes to sealed execution."""
    if fact.python_runtime != runtime:
        raise BenchmarkError('python_receipt_mismatch', 'Attempt Python differs from the run runtime.')
    executed = fact.task.execution.status != 'not_started'
    paths = {item.path for item in fact.artifact_receipts}
    prepared = executed or fact.task.setup.status not in {'not_started', 'skipped'} or prefix + 'launch.json' in paths
    checked_for_agent = executed or prefix + 'effective-config.receipt.json' in paths
    if runtime is None:
        if prepared or checked_for_agent:
            raise BenchmarkError('python_receipt_missing', 'Native execution requires Python provenance.')
        return
    if runtime not in fact.artifact_receipts:
        raise BenchmarkError('python_receipt_missing', 'Attempt inventory omits its Python runtime.')
    base = read_reference(directory, runtime, PythonReceipt)
    probes = [item for item in fact.artifact_receipts if item.path == prefix + 'python.json']
    if not probes:
        if prepared or checked_for_agent:
            raise BenchmarkError('python_probe_missing', 'Native execution requires successful Python probes.')
        return
    if len(probes) != 1:
        raise BenchmarkError('python_probe_mismatch', 'Attempt duplicates its Python probe.')
    probe = read_reference(directory, probes[0], PythonAttemptReceipt)
    if probe.runtime != runtime or probe.before_setup.version != base.version:
        raise BenchmarkError('python_probe_mismatch', 'Attempt probe belongs to a different Python runtime.')
    if checked_for_agent and probe.before_agent is None:
        raise BenchmarkError('python_probe_missing', 'Native execution lacks its post-setup Python probe.')
    if probe.before_agent is not None and probe.before_agent.version != base.version:
        raise BenchmarkError('python_probe_mismatch', 'Setup changed the Python runtime version.')
