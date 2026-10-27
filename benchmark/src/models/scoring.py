"""Typed intermediate metric results and console-only aggregates."""

from typing import Literal, Protocol

from pydantic import Field

from .base import WireModel
from .report import ReportMetric, ReportTaskScoring


class Diagnostic(WireModel):
    """Keep a bounded metric warning with its optional source/pair identity."""

    code: str
    message: str
    path: str | None = None
    pair: str | None = None


class TripletScore(WireModel):
    """Carry one metric computation before aggregation into the report."""

    score: float
    branch: Literal['code', 'struct', 'default']
    pair_scores: dict[str, float] = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class ScoreResult(WireModel):
    """Carry the complete child-process scoring response."""

    scoring: ReportTaskScoring
    metric: ReportMetric
    diagnostics: list[Diagnostic]


class DiagnosticMetrics(WireModel):
    """Summarize selected final attempts for console and offline presentation."""

    scored_tasks: int
    Mpr_LCf_mlp: float | None
    LCpr_LCf_mlp: float | None
    global_F1: float | None


class ScoredTask(Protocol):
    """Expose only the selection fields needed by aggregate diagnostics."""

    @property
    def processing_status(self) -> str:
        """Return the scheduler state used to select diagnostic rows."""
        ...

    @property
    def scoring(self) -> ReportTaskScoring:
        """Return the typed score and its independent availability status."""
        ...


class DiagnosticTask(WireModel):
    """Represent a scored task selection independently of execution evidence."""

    processing_status: str
    scoring: ReportTaskScoring
