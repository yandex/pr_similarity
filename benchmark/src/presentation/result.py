"""Typed report drafts, independent counters and arithmetic validation."""

import math
from collections.abc import Sequence
from typing import Literal

from benchmark.errors import BenchmarkError
from benchmark.execution.telemetry import unavailable_telemetry
from benchmark.json_types import JsonObject
from benchmark.models.basket import BasketRecord
from benchmark.models.report import (
    Report,
    ReportArtifacts,
    ReportArtifactsEffectiveConfig,
    ReportCounts,
    ReportError,
    ReportInfra,
    ReportPhase,
    ReportSourceDescriptor,
    ReportSummary,
    ReportTask,
    ReportTaskChanges,
    ReportTaskExecution,
    ReportTaskProgress,
    ReportTaskScoring,
    TaskDraft,
)
from benchmark.models.sources import SourceDescriptor
from benchmark.validation.contracts import validate_document


def unavailable_scoring(
    status: Literal['pending', 'unavailable'] = 'pending', error: ReportError | ReportInfra | None = None
) -> ReportTaskScoring:
    """Describe an uncomputed score without inventing numeric results."""
    if status == 'unavailable' and error is None:
        error = ReportError(code='scoring_not_available', message='This attempt has no complete scored triplets.')
    selected = None if error is None else ReportError(code=error.code, message=error.message)
    return ReportTaskScoring(status=status, error=selected, Mf_mlp=None, LCf_mlp=None, counts=None, files=[])


def pending_task(row: BasketRecord) -> TaskDraft:
    """Keep every frozen task, before registration and native work are reserved."""
    return TaskDraft(
        id=row.id,
        repository=row.change_request.repository,
        number=row.change_request.number,
        url=row.change_request.url,
        title=row.change_request.title,
        kind=row.change_request.kind,
        source=ReportSourceDescriptor(
            vcs=row.source.vcs, fetch_url=row.source.fetch_url, fetch_refs=row.source.fetch_refs.copy()
        ),
        diff_policy=row.diff_policy,
        before=row.revisions.before,
        after=row.revisions.after,
        before_tree=row.revisions.before_tree,
        after_tree=row.revisions.after_tree,
        workspace=row.workspace,
        agent_task=row.agent_task,
        processing_status='pending',
        final_attempt=None,
        setup=ReportPhase(status='not_started', exit_code=None, duration_seconds=None),
        execution=ReportTaskExecution(status='not_started', exit_code=None, duration_seconds=None),
        infrastructure_error=None,
        changes=ReportTaskChanges(status='unknown', files_count=None, diff=None),
        scoring=unavailable_scoring(),
        telemetry=unavailable_telemetry(),
        artifacts=ReportArtifacts(
            outcome=None,
            triplets_manifest=None,
            setup_log=None,
            agent_events=None,
            agent_log=None,
            effective_config=ReportArtifactsEffectiveConfig(status='not_started', path=None, message=None),
        ),
    )


def project_task(row: TaskDraft, ordinal: int, directory: str, progress: ReportTaskProgress) -> ReportTask:
    """Promote explicit draft fields into the complete public task contract."""
    return ReportTask(
        id=row.id,
        repository=row.repository,
        number=row.number,
        url=row.url,
        title=row.title,
        before=row.before,
        after=row.after,
        workspace=row.workspace,
        agent_task=row.agent_task,
        processing_status=row.processing_status,
        final_attempt=row.final_attempt,
        setup=row.setup,
        execution=row.execution,
        infrastructure_error=row.infrastructure_error,
        changes=row.changes,
        scoring=row.scoring,
        telemetry=row.telemetry,
        artifacts=row.artifacts,
        before_tree=row.before_tree,
        after_tree=row.after_tree,
        source=row.source,
        kind=row.kind,
        diff_policy=row.diff_policy,
        ordinal=ordinal,
        directory=directory,
        progress=progress,
        locations=row.locations,
    )


def summary(tasks: Sequence[TaskDraft]) -> ReportSummary:
    """Count execution, changes and scoring as independent result axes."""
    return ReportSummary(
        total_tasks=len(tasks),
        completed_tasks=sum(row.processing_status == 'completed' for row in tasks),
        pending_tasks=sum(row.processing_status == 'pending' for row in tasks),
        active_tasks=sum(row.processing_status == 'running' for row in tasks),
        retry_pending_tasks=sum(row.processing_status == 'retry_pending' for row in tasks),
        interrupted_tasks=sum(row.processing_status == 'interrupted' for row in tasks),
        infrastructure_error_tasks=sum(row.infrastructure_error is not None for row in tasks),
        timeout_tasks=sum(row.execution.status == 'timeout' for row in tasks),
        error_tasks=sum(row.execution.status == 'error' for row in tasks),
        cancelled_tasks=sum(row.execution.status == 'cancelled' for row in tasks),
        nonempty_diff_tasks=sum(row.changes.status == 'nonempty' for row in tasks),
        empty_diff_tasks=sum(row.changes.status == 'empty' for row in tasks),
        unknown_diff_tasks=sum(row.changes.status == 'unknown' for row in tasks),
        scored_tasks=sum(row.scoring.status == 'available' for row in tasks),
    )


def validate_report(report: Report | JsonObject) -> Report:
    """Validate the complete schema before parsing and checking score arithmetic."""
    validate_document('report', report)
    report = report if isinstance(report, Report) else Report.model_validate(report)
    if report.summary != summary(report.tasks) or len(report.tasks) != report.basket.total_tasks:
        raise BenchmarkError('invalid_report', 'Report summary does not match its complete PR queue.', exit_code=1)
    ids = [row.id for row in report.tasks]
    if len(ids) != len(set(ids)):
        raise BenchmarkError('invalid_report', 'Report contains duplicate PR identities.', exit_code=1)
    for row in report.tasks:
        SourceDescriptor.from_model(row.source)
        score = row.scoring
        if score.status != 'available':
            continue
        files = score.files
        paths = [item.path for item in files]
        if paths != sorted(set(paths)):
            raise BenchmarkError('invalid_report', 'Scoring file paths must be sorted and unique.', exit_code=1)
        counts = ReportCounts(
            human_files=sum(item.changed_human for item in files),
            agent_files=sum(item.changed_agent for item in files),
            matched_files=sum(item.changed_human and item.changed_agent for item in files),
            missed_files=sum(item.changed_human and not item.changed_agent for item in files),
            extra_files=sum(item.changed_agent and not item.changed_human for item in files),
            human_loc=sum(item.human_loc for item in files),
            agent_loc=sum(item.agent_loc for item in files),
            loc_weight=sum(item.loc_weight for item in files),
        )
        if counts != score.counts:
            raise BenchmarkError('invalid_report', 'Scoring counts do not match per-file data.', exit_code=1)
        for item in files:
            expected = max(1, item.human_loc) if item.changed_human else 0
            if item.loc_weight != expected:
                raise BenchmarkError('invalid_report', 'Scoring LOC weights are inconsistent.', exit_code=1)
        mean = sum(item.mlp for item in files) / len(files) if files else 1.0
        weighted = sum(item.mlp * item.loc_weight for item in files) / counts.loc_weight if counts.loc_weight else mean
        assert score.Mf_mlp is not None and score.LCf_mlp is not None
        if not math.isclose(mean, score.Mf_mlp, abs_tol=1e-12) or not math.isclose(
            weighted, score.LCf_mlp, abs_tol=1e-12
        ):
            raise BenchmarkError('invalid_report', 'Scoring aggregates do not match per-file results.', exit_code=1)
    return report
