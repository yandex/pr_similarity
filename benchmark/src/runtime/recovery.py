"""Recover only registered native facts and score sealed evidence without agent replay."""

import copy
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from benchmark.config import RunConfig
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.metrics.process import score_saved
from benchmark.metrics.scoring import metric_receipt
from benchmark.models import Report
from benchmark.models.evidence import OperationDetails, Outcome, ScoreItem, Seal
from benchmark.models.report import ReportError, ReportTask
from benchmark.models.state import StateTaskItem
from benchmark.persistence.artifact_refs import read_reference, reference
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import utc_now
from benchmark.presentation.result import unavailable_scoring
from benchmark.runtime.commands import Command
from benchmark.runtime.lifecycle import Stop, native_policy, needs_host_score


@dataclass(frozen=True)
class RecoveryNotice:
    """Describe one adopted immutable fact without changing scheduling state."""

    task_id: str
    attempt_id: str
    kind: str
    fatal: ReportError | None = None


def _native_status(
    store: RunStore, config: RunConfig, task_id: str, outcome: Outcome | Seal
) -> Literal['retry_pending', 'interrupted', 'completed']:
    """Make retry decisions from native facts while deferring any failed host scoring."""
    selected = next(row for row in store.state.tasks if row.id == task_id)
    row = outcome.task
    error = row.infrastructure_error
    fatal = (outcome.fatal if isinstance(outcome, Outcome) else None) is not None or (
        error is not None and error.code in ('auth_required', 'credentials_missing')
    )
    return native_policy(
        row,
        config.data.agent.retries,
        selected.retry_index,
        fatal=fatal,
        scoring_error=(outcome.scoring_error if isinstance(outcome, Outcome) else None),
    )[0]


def recover_native_facts(store: RunStore, config: RunConfig) -> list[RecoveryNotice]:
    """Adopt exact owned facts missed by a commit, without selecting newest directories.

    Args:
        store: Validated writer-owned registry and authoritative state.
        config: Current settings with the same frozen semantic identity.

    Returns:
        Recovered reservation, seal and outcome notices, including native fatal errors.

    Raises:
        BenchmarkError: If an expected fact is corrupt or belongs to another owner.

    """
    notices = [
        RecoveryNotice(task_id=item.task_id, attempt_id=item.attempt_id, kind='reservation')
        for item in store.recover_reservations()
    ]
    for index in range(len(store.state.tasks)):
        selected = store.state.tasks[index]
        attempt = selected.attempt
        if attempt is None:
            continue
        directory = store.attempt_directory(selected.id, attempt.sequence)
        for kind in ('seal', 'outcome'):
            selected = store.state.tasks[index]
            path = artifact_path(store.directory, (directory / (kind + '.json')).as_posix())
            if (selected.seal if kind == 'seal' else selected.outcome) is not None or not path.is_file():
                continue
            receipt = reference(store.directory, path.relative_to(store.directory).as_posix())
            value = read_reference(store.directory, receipt, Outcome if kind == 'outcome' else Seal)
            status = _native_status(store, config, selected.id, value) if kind == 'outcome' else None
            store.accept_fact(selected.id, kind, receipt, processing_status=status)
            notices.append(
                RecoveryNotice(
                    task_id=selected.id,
                    attempt_id=attempt.id,
                    kind=kind,
                    fatal=value.fatal if isinstance(value, Outcome) else None,
                )
            )
        selected = store.state.tasks[index]
        if selected.seal is None and selected.processing_status == 'running':
            report = store.snapshot()
            report.tasks[index].processing_status = 'interrupted'
            store.commit(report)
    return notices


def _score_one(
    store: RunStore, config: RunConfig, command: Command, stop: Stop, selected: StateTaskItem, row: ReportTask
) -> tuple[ScoreItem, bytes]:
    """Run one bounded host score; failures produce a result without altering native facts."""
    manifest = row.artifacts.triplets_manifest
    assert manifest is not None and selected.attempt is not None
    path = artifact_path(store.directory, manifest, must_exist=True)
    assert selected.seal is not None
    native = read_reference(store.directory, selected.seal, Seal)
    owned = {receipt.path: receipt for receipt in native.artifact_receipts}
    if manifest not in owned:
        raise BenchmarkError('triplet_receipt_missing', 'Native seal does not own its triplet manifest.')
    command.task_start(row.id, ordinal=row.ordinal, attempt_sequence=selected.attempt.sequence)
    command.task_phase(row.id, 'recovering score', attempt_sequence=selected.attempt.sequence)
    store.observe(row.id, 'recovering score', utc_now())
    with TemporaryDirectory(prefix='benchmark-host-score-') as temporary:
        log = Path(temporary) / 'scoring.log'
        try:
            result = score_saved(
                path.parent,
                path.parent.relative_to(store.directory).as_posix(),
                log,
                stop,
                grace=config.data.shutdown.grace_seconds,
            )
            if result.metric != store.state.metric:
                raise BenchmarkError('metric_changed', 'Scoring result differs from the frozen metric.')
            scoring, diagnostics = result.scoring, result.diagnostics
        except OPERATION_ERRORS as error:
            command.diagnostic(error)
            message = (
                error.message
                if isinstance(error, BenchmarkError)
                else 'Host scoring failed; native evidence remains available.'
            )
            code = error.code if isinstance(error, BenchmarkError) else 'scoring_failed'
            scoring, diagnostics = unavailable_scoring('unavailable', ReportError(code=code, message=message)), []
            command.message(f'Task {row.ordinal:03d}: {message}', level='WARNING')
        content = log.read_bytes() if log.is_file() else b''
    return ScoreItem(task_id=row.id, attempt_id=selected.attempt.id, scoring=scoring, diagnostics=diagnostics), content


def recover_host_scores(store: RunStore, config: RunConfig, command: Command, stop: Stop) -> Report:
    """Recover sealed host work once per invocation, leaving all native outcomes immutable.

    Args:
        store: Stopped writer-owned run before issuing fresh native tasks.
        config: Retry and host cancellation settings matching frozen identity.
        command: Human progress and diagnostics for the current resume.
        stop: Signal intent; never converted into a native retry by host scoring.

    Returns:
        The derived report with explicitly selected host results.

    Raises:
        BenchmarkError: If sealed inputs are invalid or the metric changed.

    """
    report = store.snapshot()
    eligible = [
        index for index, selected in enumerate(store.state.tasks) if needs_host_score(selected, report.tasks[index])
    ]
    if not eligible:
        return report
    if metric_receipt() != store.state.metric:
        raise BenchmarkError('resume_metric_mismatch', 'Metric changed; use explicit rescore before resuming.')
    for index in eligible:
        if stop.is_set():
            break
        selected = copy.deepcopy(store.state.tasks[index])
        row = store.snapshot().tasks[index]
        assert selected.attempt is not None
        item, log = _score_one(store, config, command, stop, selected, row)
        operation = store.prepare_operation(
            'rescore',
            [item],
            invocation_id=command.id,
            metric=store.state.metric,
            extra=OperationDetails(source='resume_scoring', task_id=row.id),
            artifacts={'scoring.log': log},
        )
        store.accept_operation(operation)
        report = store.snapshot()
        current = report.tasks[index]
        if stop.is_set():
            status = 'completed' if selected.processing_status == 'completed' else 'interrupted'
        elif item.scoring.status != 'available':
            status = 'completed'
        else:
            assert selected.seal is not None
            native = read_reference(store.directory, selected.seal, Seal)
            status = _native_status(store, config, row.id, native)
        current.processing_status = status
        report = store.commit(report)
        command.task_result(row.id, mf=current.scoring.Mf_mlp, lcf=current.scoring.LCf_mlp)
        if status != 'completed' and not stop.is_set():
            command.task_phase(row.id, 'retry pending', attempt_sequence=selected.attempt.sequence)
            command.worker_done(row.id)
        else:
            command.task_finish(
                row.id,
                execution=current.execution.status,
                scoring='scored' if item.scoring.status == 'available' else 'not available',
                attempt_sequence=selected.attempt.sequence,
            )
    return store.snapshot()
