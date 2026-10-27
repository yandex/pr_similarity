"""Fixed MLP, existence-aware shortcuts and PR/file-set aggregation."""

import math
import os
from collections.abc import Sequence
from pathlib import PurePosixPath

from benchmark.errors import BenchmarkError
from benchmark.metrics.codebleu import (
    EXT_TO_LANG,
    compute_codebleu_components,
    require_dependencies,
)
from benchmark.metrics.features import triplet_features
from benchmark.metrics.mlp import (
    mlp_pair_similarity_from_features,
    mlp_similarity_from_pair_scores,
)
from benchmark.metrics.text import count_diff_lines, normalize
from benchmark.models.report import (
    ReportCounts,
    ReportFile,
    ReportFileExists,
    ReportMetric,
    ReportTaskScoring,
)
from benchmark.models.scoring import (
    Diagnostic,
    DiagnosticMetrics,
    ScoredTask,
    TripletScore,
)
from benchmark.models.triplets import TripletReferences, TripletStates
from benchmark.validation.triplet_paths import triplet_path
from benchmark.vcs.workspace import FileState

METRIC_VERSION = 'mlp-existence-v1'
_PAIRS = ('base_human', 'base_ai', 'human_ai')


def _state_value(state: FileState) -> tuple[bool, str]:
    """Return existence and normalized content for a scored file."""
    if not state.exists and (state.text or state.mode is not None):
        raise BenchmarkError('invalid_file_state', 'An absent scored file contains unexpected data.', exit_code=1)
    return state.exists, normalize(state.text)


def score_triplet(base: FileState, human: FileState, ai: FileState, path: str) -> TripletScore:
    """Evaluate one union path; callers exclude paths unchanged by both sides."""
    require_dependencies()
    ext = PurePosixPath(path).suffix
    branch = 'code' if ext in EXT_TO_LANG else ('struct' if ext in ('.yaml', '.yml', '.json') else 'default')
    b, h, a = _state_value(base), _state_value(human), _state_value(ai)
    result = TripletScore(score=0.0, branch=branch, pair_scores={}, diagnostics=[])
    if h == b or a == b or h[0] != a[0]:
        return result
    if h == a:
        result.score = 1.0
        return result
    if (h[1] == '') != (a[1] == ''):
        return result
    string_features, diff_features = triplet_features(base.text, human.text, ai.text)
    components = {}
    if branch == 'code':
        pairs = {
            'base_human': (base.text, human.text),
            'base_ai': (base.text, ai.text),
            'human_ai': (human.text, ai.text),
        }
        for name in _PAIRS:
            value = compute_codebleu_components(*pairs[name], EXT_TO_LANG[ext])
            if value is None:
                components = {}
                branch = 'default'
                result.diagnostics.append(
                    Diagnostic(
                        code='codebleu_fallback', message='CodeBLEU failed; default MLP was used.', path=path, pair=name
                    )
                )
                break
            components[name] = value
    pair_scores = {
        name: mlp_pair_similarity_from_features(branch, string_features[name], components.get(name)) for name in _PAIRS
    }
    score = mlp_similarity_from_pair_scores(branch, pair_scores, diff_features)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise BenchmarkError(
            'invalid_score', 'The fixed metric returned an invalid score; check the file log.', exit_code=1
        )
    result.score, result.branch, result.pair_scores = score, branch, pair_scores
    return result


def _references(
    path: str, states: TripletStates, prefix: str, receipts: dict[str, TripletReferences] | None, number: int
) -> TripletReferences:
    """Use manifest-owned references while keeping absent roles explicitly null."""
    supplied = None if receipts is None else receipts[path]
    result: dict[str, str | None] = {}
    for role, state, relative in (
        ('base', states.base, None if supplied is None else supplied.base),
        ('human', states.human, None if supplied is None else supplied.human),
        ('ai', states.ai, None if supplied is None else supplied.ai),
    ):
        if supplied is None:
            relative = triplet_path(number, path, role) if state.exists else None
        if state.exists:
            if not isinstance(relative, str):
                raise BenchmarkError('triplet_reference_missing', 'Existing scored state has no artifact reference.')
            result[role] = prefix + '/' + relative
        else:
            if relative is not None:
                raise BenchmarkError('triplet_reference_mismatch', 'Absent scored state references a file.')
            result[role] = None
    return TripletReferences(base=result['base'], human=result['human'], ai=result['ai'])


def score_pr(
    triplets: dict[str, TripletStates],
    *,
    artifact_prefix: str = 'triplets',
    diagnostics: list[Diagnostic] | None = None,
    artifact_refs: dict[str, TripletReferences] | None = None,
) -> ReportTaskScoring:
    """Return exactly the report's available-scoring object."""
    require_dependencies()
    counts = ReportCounts(
        human_files=0,
        agent_files=0,
        matched_files=0,
        missed_files=0,
        extra_files=0,
        human_loc=0,
        agent_loc=0,
        loc_weight=0,
    )
    rows = []
    for number, path in enumerate(sorted(triplets), 1):
        states = triplets[path]
        base, human, ai = states.base, states.human, states.ai
        changed_human = _state_value(base) != _state_value(human)
        changed_ai = _state_value(base) != _state_value(ai)
        if not changed_human and not changed_ai:
            continue
        human_loc = count_diff_lines(base.text, human.text) if changed_human else 0
        agent_loc = count_diff_lines(base.text, ai.text) if changed_ai else 0
        weight = max(1, human_loc) if changed_human else 0
        result = score_triplet(base, human, ai, path)
        if diagnostics is not None:
            diagnostics.extend(result.diagnostics)
        references = _references(path, states, artifact_prefix, artifact_refs, number)
        rows.append(
            ReportFile(
                path=path,
                exists=ReportFileExists(base=base.exists, human=human.exists, ai=ai.exists),
                changed_human=changed_human,
                changed_agent=changed_ai,
                human_loc=human_loc,
                agent_loc=agent_loc,
                loc_weight=weight,
                mlp=result.score,
                branch=result.branch,
                base=references.base,
                human=references.human,
                ai=references.ai,
            )
        )
        counts.human_files += int(changed_human)
        counts.agent_files += int(changed_ai)
        counts.matched_files += int(changed_human and changed_ai)
        counts.missed_files += int(changed_human and not changed_ai)
        counts.extra_files += int(changed_ai and not changed_human)
        counts.human_loc += human_loc
        counts.agent_loc += agent_loc
        counts.loc_weight += weight
    mean = sum(row.mlp for row in rows) / len(rows) if rows else 1.0
    weighted = sum(row.loc_weight * row.mlp for row in rows) / counts.loc_weight if counts.loc_weight else mean
    return ReportTaskScoring(status='available', error=None, Mf_mlp=mean, LCf_mlp=weighted, counts=counts, files=rows)


def diagnostic_metrics(tasks: Sequence[ScoredTask]) -> DiagnosticMetrics:
    """Calculate console diagnostics from completed final attempts only."""
    selected = [
        task.scoring for task in tasks if task.processing_status == 'completed' and task.scoring.status == 'available'
    ]
    result = DiagnosticMetrics(scored_tasks=len(selected), Mpr_LCf_mlp=None, LCpr_LCf_mlp=None, global_F1=None)
    if selected:
        for task in selected:
            assert task.LCf_mlp is not None and task.counts is not None
        scores = [
            (task.LCf_mlp, task.counts) for task in selected if task.LCf_mlp is not None and task.counts is not None
        ]
        result.Mpr_LCf_mlp = sum(score for score, _ in scores) / len(selected)
        weight = sum(counts.loc_weight for _, counts in scores)
        if weight:
            result.LCpr_LCf_mlp = sum(score * counts.loc_weight for score, counts in scores) / weight
        denominator = sum(counts.human_files + counts.agent_files for _, counts in scores)
        result.global_F1 = 2 * sum(counts.matched_files for _, counts in scores) / denominator if denominator else 1.0
    return result


def metric_receipt(*, check_environment: bool = True) -> ReportMetric:
    """Identify the explicit metric contract without binding deployment source bytes."""
    if check_environment:
        require_dependencies()
    if os.environ.get('PYTHONHASHSEED') != '42':
        raise BenchmarkError(
            'metric_seed_unfixed', 'Use Make or set PYTHONHASHSEED=42 before starting Python.', exit_code=1
        )
    return ReportMetric(name='mlp', version=METRIC_VERSION, pythonhashseed=42)
