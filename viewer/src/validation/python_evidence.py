"""Consumer checks for Python identity and per-attempt preflight evidence."""

from viewer.errors import ViewerError
from viewer.models.base import WireModel
from viewer.models.evidence import ArtifactReceipt, Seal, parse_evidence
from viewer.models.python_runtime import PythonAttemptReceipt, PythonReceipt
from viewer.validation.operations import OperationStore


class PythonFact(WireModel):
    """Read the exact fields needed to verify native Python ownership."""

    python_runtime: ArtifactReceipt | None
    artifact_receipts: list[ArtifactReceipt]


def validate_python_attempt(seal: Seal, store: OperationStore, prefix: str) -> None:
    """Validate new-release evidence without requiring an executable on the viewer."""
    if store.registry.schema_version != 2:
        return
    fact = parse_evidence(PythonFact, seal.to_wire(), 'invalid_state')
    runtime = store.state.python_runtime
    if runtime is None or fact.python_runtime is None or runtime.to_wire() != fact.python_runtime.to_wire():
        raise ViewerError('invalid_state', 'Attempt Python differs from the run runtime.')
    if fact.python_runtime not in fact.artifact_receipts:
        raise ViewerError('invalid_state', 'Native inventory omits its Python runtime.')
    base = store.document(runtime.path, PythonReceipt)
    probes = [item for item in fact.artifact_receipts if item.path == prefix + 'python.json']
    executed = seal.task.execution.status != 'not_started'
    paths = {item.path for item in fact.artifact_receipts}
    prepared = executed or seal.task.setup.status not in {'not_started', 'skipped'} or prefix + 'launch.json' in paths
    checked_for_agent = executed or prefix + 'effective-config.receipt.json' in paths
    if not probes:
        if prepared or checked_for_agent:
            raise ViewerError('invalid_state', 'Native execution lacks Python probes.')
        return
    if (
        len(probes) != 1
        or probes[0].path not in store.inventory
        or probes[0].to_wire() != store.inventory[probes[0].path].to_wire()
    ):
        raise ViewerError('invalid_state', 'Python probe receipt differs from archive bytes.')
    probe = store.document(probes[0].path, PythonAttemptReceipt)
    if probe.runtime != fact.python_runtime or probe.before_setup.version != base.version:
        raise ViewerError('invalid_state', 'Python probe belongs to a different runtime.')
    if checked_for_agent and probe.before_agent is None:
        raise ViewerError('invalid_state', 'Native execution lacks its post-setup Python probe.')
    if probe.before_agent is not None and probe.before_agent.version != base.version:
        raise ViewerError('invalid_state', 'Setup changed the Python runtime version.')
