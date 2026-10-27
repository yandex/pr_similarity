"""Typed state-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import ArtifactReceipt as StateTaskItemOutcome
from .base import WireModel
from .evidence import OperationSelection
from .report import ReportAgentResolvedRuntime as StateResolvedRuntime
from .report import ReportAttempt as StateAttempt
from .report import ReportError as StateError
from .report import ReportMetric as StateMetric
from .report import ReportTaskProgress as StateTaskItemProgress


class StateRun(WireModel):
    """Represent the StateRun object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/run'
    status: Literal['running', 'stopping', 'interrupted', 'completed', 'completed_with_errors', 'failed']
    finished_at: str | None
    error: StateError | None
    current_invocation_id: str


StateTaskItemSeal = StateTaskItemOutcome


StateTaskItemScoringResult = StateTaskItemOutcome


StateTaskItemScoringReceipt = StateTaskItemOutcome


StateTaskItemScoring = OperationSelection


StateTaskItemPricingResult = StateTaskItemOutcome


StateTaskItemPricingReceipt = StateTaskItemOutcome


StateTaskItemPricing = OperationSelection


StateTaskItemHistoryItemStart = StateTaskItemOutcome


StateTaskItemHistoryItemSeal = StateTaskItemOutcome


StateTaskItemHistoryItemOutcome = StateTaskItemOutcome


StateTaskItemHistoryItemScoringResult = StateTaskItemOutcome


StateTaskItemHistoryItemScoringReceipt = StateTaskItemOutcome


StateTaskItemHistoryItemScoring = OperationSelection


StateTaskItemHistoryItemPricingResult = StateTaskItemOutcome


StateTaskItemHistoryItemPricingReceipt = StateTaskItemOutcome


StateTaskItemHistoryItemPricing = StateTaskItemHistoryItemScoring


class StateTaskItemHistoryItem(WireModel):
    """Represent the StateTaskItemHistoryItem object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/history/items'
    attempt: StateAttempt
    start: StateTaskItemHistoryItemStart | None
    seal: StateTaskItemHistoryItemSeal | None
    outcome: StateTaskItemHistoryItemOutcome | None
    scoring: StateTaskItemHistoryItemScoring | None
    pricing: StateTaskItemHistoryItemPricing | None


StateTaskItemStart = StateTaskItemOutcome


class StateTaskItem(WireModel):
    """Represent the StateTaskItem object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items'
    id: str
    ordinal: int
    processing_status: Literal['pending', 'running', 'completed', 'retry_pending', 'interrupted']
    attempt: StateAttempt | None
    next_sequence: int
    retry_index: int
    retry_epoch: int
    outcome: StateTaskItemOutcome | None
    seal: StateTaskItemSeal | None
    scoring: StateTaskItemScoring | None
    pricing: StateTaskItemPricing | None
    progress: StateTaskItemProgress
    history: list[StateTaskItemHistoryItem]
    start: StateTaskItemStart | None


StateSelectedOperationsRescoreResult = StateTaskItemOutcome


StateSelectedOperationsRescoreReceipt = StateTaskItemOutcome


StateSelectedOperationsRescore = StateTaskItemScoring


StateSelectedOperationsRepriceResult = StateTaskItemOutcome


StateSelectedOperationsRepriceReceipt = StateTaskItemOutcome


StateSelectedOperationsReprice = StateTaskItemPricing


class StateSelectedOperations(WireModel):
    """Represent the StateSelectedOperations object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/selected_operations'
    rescore: StateSelectedOperationsRescore | None
    reprice: StateSelectedOperationsReprice | None


StatePendingPublishRequest = StateTaskItemOutcome


class StatePendingPublish(WireModel):
    """Represent the StatePendingPublish object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/pending_publish'
    id: str
    request: StatePendingPublishRequest


StateLastPublishRequest = StateTaskItemOutcome


StateLastPublishReceipt = StateTaskItemOutcome


class StateLastPublish(WireModel):
    """Represent the StateLastPublish object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/last_publish'
    id: str
    request: StateLastPublishRequest
    receipt: StateLastPublishReceipt


class State(WireModel):
    """Represent the State object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_family: Literal['pr_similarity_os.state']
    schema_version: int
    run_id: str
    registry_sha256: str
    revision: int
    updated_at: str
    run: StateRun
    resolved_runtime: StateResolvedRuntime | None
    python_runtime: StateTaskItemOutcome | None
    metric: StateMetric
    tasks: list[StateTaskItem]
    selected_operations: StateSelectedOperations
    pending_publish: StatePendingPublish | None
    last_publish: StateLastPublish | None


State.model_rebuild()
