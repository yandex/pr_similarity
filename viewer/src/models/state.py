"""Typed state-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import WireModel
from .legacy import LegacyMetric


class StateError(WireModel):
    """Represent the StateError object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/error'
    code: str
    message: str


class StateRun(WireModel):
    """Represent the StateRun object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/run'
    status: Literal[
        'running',
        'stopping',
        'interrupted',
        'completed',
        'completed_with_errors',
        'failed',
    ]
    finished_at: str | None
    error: StateError | None
    current_invocation_id: str


class StateResolvedRuntime(WireModel):
    """Represent the StateResolvedRuntime object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/resolved_runtime/anyOf/0'
    version: str
    asset_name: str
    platform: str
    archive_sha256: str
    binary_sha256: str
    receipt: str


class StateMetric(WireModel):
    """Represent the StateMetric object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/metric'
    name: Literal['mlp']
    version: str
    pythonhashseed: int | float


class StateAttempt(WireModel):
    """Represent the StateAttempt object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/attempt'
    id: str
    invocation_id: str
    sequence: int | float
    retry_index: int | float
    started_at: str
    finished_at: str | None


class StateTaskItemOutcome(WireModel):
    """Represent the StateTaskItemOutcome object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/outcome'
    path: str
    size_bytes: int | float
    sha256: str


StateTaskItemSeal = StateTaskItemOutcome


StateTaskItemScoringResult = StateTaskItemOutcome


StateTaskItemScoringReceipt = StateTaskItemOutcome


class StateTaskItemScoring(WireModel):
    """Represent the StateTaskItemScoring object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/scoring'
    id: str
    kind: Literal['rescore']
    accepted_revision: int | float
    result: StateTaskItemScoringResult
    receipt: StateTaskItemScoringReceipt


StateTaskItemPricingResult = StateTaskItemOutcome


StateTaskItemPricingReceipt = StateTaskItemOutcome


class StateTaskItemPricing(WireModel):
    """Represent the StateTaskItemPricing object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/pricing'
    id: str
    kind: Literal['reprice']
    accepted_revision: int | float
    result: StateTaskItemPricingResult
    receipt: StateTaskItemPricingReceipt


class StateTaskItemProgress(WireModel):
    """Represent the StateTaskItemProgress object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/progress'
    phase: str
    updated_at: str | None


StateTaskItemHistoryItemStart = StateTaskItemOutcome


StateTaskItemHistoryItemSeal = StateTaskItemOutcome


StateTaskItemHistoryItemOutcome = StateTaskItemOutcome


StateTaskItemHistoryItemScoringResult = StateTaskItemOutcome


StateTaskItemHistoryItemScoringReceipt = StateTaskItemOutcome


class StateTaskItemHistoryItemScoring(WireModel):
    """Represent the StateTaskItemHistoryItemScoring object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/history/items/properties/scoring'
    id: str
    kind: Literal['rescore', 'reprice']
    accepted_revision: int | float
    result: StateTaskItemHistoryItemScoringResult
    receipt: StateTaskItemHistoryItemScoringReceipt


StateTaskItemHistoryItemPricingResult = StateTaskItemOutcome


StateTaskItemHistoryItemPricingReceipt = StateTaskItemOutcome


class StateTaskItemHistoryItemPricing(WireModel):
    """Represent the StateTaskItemHistoryItemPricing object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items/properties/history/items/properties/pricing'
    id: str
    kind: Literal['rescore', 'reprice']
    accepted_revision: int | float
    result: StateTaskItemHistoryItemPricingResult
    receipt: StateTaskItemHistoryItemPricingReceipt


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
    ordinal: int | float
    processing_status: Literal['pending', 'running', 'completed', 'retry_pending', 'interrupted']
    attempt: StateAttempt | None
    next_sequence: int | float
    retry_index: int | float
    retry_epoch: int | float
    outcome: StateTaskItemOutcome | None
    seal: StateTaskItemSeal | None
    scoring: StateTaskItemScoring | None
    pricing: StateTaskItemPricing | None
    progress: StateTaskItemProgress
    history: list[StateTaskItemHistoryItem]
    start: StateTaskItemStart | None


StateSelectedOperationsRescoreResult = StateTaskItemOutcome


StateSelectedOperationsRescoreReceipt = StateTaskItemOutcome


class StateSelectedOperationsRescore(WireModel):
    """Represent the StateSelectedOperationsRescore object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/selected_operations/properties/rescore'
    id: str
    kind: Literal['rescore']
    accepted_revision: int | float
    result: StateSelectedOperationsRescoreResult
    receipt: StateSelectedOperationsRescoreReceipt


StateSelectedOperationsRepriceResult = StateTaskItemOutcome


StateSelectedOperationsRepriceReceipt = StateTaskItemOutcome


class StateSelectedOperationsReprice(WireModel):
    """Represent the StateSelectedOperationsReprice object in the state-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/selected_operations/properties/reprice'
    id: str
    kind: Literal['reprice']
    accepted_revision: int | float
    result: StateSelectedOperationsRepriceResult
    receipt: StateSelectedOperationsRepriceReceipt


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
    schema_version: int | float
    run_id: str
    registry_sha256: str
    revision: int | float
    updated_at: str
    run: StateRun
    resolved_runtime: StateResolvedRuntime | None
    # Historical v1 omits this field; the v2 ingress schema requires it.
    python_runtime: StateTaskItemOutcome | None = None
    metric: StateMetric | LegacyMetric
    tasks: list[StateTaskItem]
    selected_operations: StateSelectedOperations
    pending_publish: StatePendingPublish | None
    last_publish: StateLastPublish | None


State.model_rebuild()
