"""Pure public aggregates over final task attempts; unknown is never zero."""

from collections import Counter
from collections.abc import Sequence
from statistics import median

from viewer.errors import ViewerError
from viewer.models.report import ReportInference, ReportTask, ReportTaskScoring
from viewer.presentation.inference import generated_groups

STATUSES = ('OK', 'no-op', 'timeout', 'fail')
DEFAULT_STATUSES = STATUSES[:-1]
TOKEN_CATEGORIES = ('cache_read', 'input', 'output', 'cache_write', 'reasoning')


def task_status(task: ReportTask) -> str:
    """Classify the final task outcome from execution and capture state."""
    if task.infrastructure_error or task.processing_status != 'completed':
        return 'fail'
    execution = task.execution.status
    if execution == 'timeout':
        return 'timeout'
    if execution == 'completed':
        return {'empty': 'no-op', 'nonempty': 'OK'}.get(task.changes.status, 'fail')
    return 'fail'


def parse_tags(tags: list[str]) -> dict[str, list[str]]:
    """Keep all editable labels in Setup, including prefix-shaped strings."""
    return {'model': [], 'setup': list(tags)}


def tag_groups(tags: list[str], inference: ReportInference) -> dict[str, list[str]]:
    """Use configured facts for generated groups and raw user labels for Setup."""
    return {**generated_groups(inference), 'setup': list(tags)}


def task_cost(task: ReportTask) -> tuple[float | None, str]:
    """Select the task cost and record whether it came from repricing."""
    telemetry = task.telemetry
    reprice = telemetry.api_equivalent
    if reprice is not None:
        return (reprice.amount if reprice.status == 'complete' else None), 'reprice'
    return telemetry.cost.amount, 'opencode'


def covered_sum(values: Sequence[float | None]) -> dict:
    """Report known totals separately from incomplete aggregate amounts."""
    known = [v for v in values if v is not None]
    return {
        'amount': sum(known) if values and len(known) == len(values) else None,
        'known_subtotal': sum(known),
        'known_tasks': len(known),
        'total_tasks': len(values),
    }


def displayed_amount(value: dict) -> float | None:
    """Sort by the same measured value the UI shows, never by an unknown zero."""
    if value['amount'] is not None:
        return value['amount']
    return value.get('known_subtotal') if value.get('known_tasks', 0) else None


def score_value(scoring: ReportTaskScoring, aggregation: str) -> float:
    """Read the selected available score after contract validation."""
    value = scoring.Mf_mlp if aggregation == 'Mf' else scoring.LCf_mlp
    assert value is not None
    return value


def score_weight(scoring: ReportTaskScoring, field: str | None) -> float:
    """Read the validated count used by one aggregate."""
    if field is None:
        return 1
    counts = scoring.counts
    assert counts is not None
    if field == 'human_files':
        return counts.human_files
    if field == 'agent_files':
        return counts.agent_files
    if field == 'matched_files':
        return counts.matched_files
    return counts.loc_weight


def aggregate(
    tasks: list[ReportTask],
    statuses: Sequence[str] | None = None,
    pr_aggregation: str = 'LCpr',
    file_aggregation: str = 'LCf',
) -> dict:
    """Compute selected task metrics without treating unknown values as zero."""
    statuses = list(DEFAULT_STATUSES if statuses is None else statuses)
    if (
        set(statuses) - set(STATUSES)
        or pr_aggregation not in ('Mpr', 'FCpr', 'LCpr')
        or file_aggregation not in ('Mf', 'LCf')
    ):
        raise ViewerError('invalid_selection', 'Invalid aggregation selection.')
    selected = [t for t in tasks if task_status(t) in statuses]
    scored = [t.scoring for t in selected if t.scoring.status == 'available']
    weight_field = {'Mpr': None, 'FCpr': 'human_files', 'LCpr': 'loc_weight'}[pr_aggregation]
    pairs = [(score_value(t, file_aggregation), score_weight(t, weight_field)) for t in scored]
    weight = sum(w for _, w in pairs)
    denominator = sum(score_weight(t, 'human_files') + score_weight(t, 'agent_files') for t in scored)
    f1 = (
        None
        if not scored
        else (2 * sum(score_weight(t, 'matched_files') for t in scored) / denominator if denominator else 1)
    )
    costs = [task_cost(t) for t in selected]
    durations = [t.execution.duration_seconds for t in selected if t.execution.duration_seconds is not None]
    return {
        'selection': {
            'statuses': [s for s in STATUSES if s in statuses],
            'pr_aggregation': pr_aggregation,
            'file_aggregation': file_aggregation,
        },
        'score': sum(v * w for v, w in pairs) / weight if weight else None,
        'f1_files': f1,
        'total_tasks': len(tasks),
        'selected_tasks': len(selected),
        'scored_tasks': len(scored),
        'status_counts': {s: sum(task_status(t) == s for t in tasks) for s in STATUSES},
        'cost': {
            **covered_sum([v for v, _ in costs]),
            'currency': 'USD',
            'sources': dict(Counter(s for _, s in costs)),
            'native_zero_tasks': sum(v == 0 and source == 'opencode' for v, source in costs),
        },
        'tokens': {
            category: covered_sum([getattr(t.telemetry.tokens, category) for t in selected])
            for category in (*TOKEN_CATEGORIES, 'total')
        },
        'time': {
            'median_seconds': median(durations) if durations else None,
            'total_seconds': sum(durations) if selected and len(durations) == len(selected) else None,
            'known_subtotal_seconds': sum(durations),
            'known_tasks': len(durations),
            'total_tasks': len(selected),
        },
    }


def aggregate_snapshot(tasks: list[ReportTask]) -> dict[int, dict]:
    """Retain bounded aggregate views, not every run's full report, in the catalogue."""
    result = {}
    for mask in range(1 << len(STATUSES)):
        statuses = [s for i, s in enumerate(STATUSES) if mask & (1 << i)]
        common = aggregate(tasks, statuses, 'Mpr', 'Mf')
        scored = [t.scoring for t in tasks if task_status(t) in statuses and t.scoring.status == 'available']
        scores = {}
        for pr, field in [('Mpr', None), ('FCpr', 'human_files'), ('LCpr', 'loc_weight')]:
            weights = [score_weight(s, field) for s in scored]
            denominator = sum(weights)
            for file in ('Mf', 'LCf'):
                scores[pr + '/' + file] = (
                    sum(score_value(s, file) * w for s, w in zip(scored, weights)) / denominator
                    if denominator
                    else None
                )
        common['scores'] = scores
        result[mask] = common
    return result
