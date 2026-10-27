"""Typed source-role inventories shared by capture and host scoring."""

from dataclasses import dataclass

from benchmark.vcs.workspace import FileState

from .base import ArtifactReceipt, WireModel
from .report import ReportFileExists


class TripletSource(WireModel):
    """Bind immutable source roles to their before and after revisions."""

    before: str
    after: str


class TripletModes(WireModel):
    """Retain executable mode or absence separately for every source role."""

    base: str | None
    human: str | None
    ai: str | None


class TripletFiles(WireModel):
    """Retain each role's exact byte receipt or explicit absence."""

    base: ArtifactReceipt | None
    human: ArtifactReceipt | None
    ai: ArtifactReceipt | None


class TripletReferences(WireModel):
    """Expose manifest-owned relative paths for metric result references."""

    base: str | None
    human: str | None
    ai: str | None


class TripletEntry(WireModel):
    """Describe one changed source path and all three variants."""

    path: str
    number: int
    modes: TripletModes
    exists: ReportFileExists
    files: TripletFiles


class TripletIndex(WireModel):
    """Own an ordered immutable collection of source variants."""

    version: int
    run_id: str | None
    task_id: str
    attempt_id: str
    source: TripletSource
    entries: list[TripletEntry]


@dataclass(frozen=True)
class TripletStates:
    """Keep existing/absent decoded file states for the three named roles."""

    base: FileState
    human: FileState
    ai: FileState
