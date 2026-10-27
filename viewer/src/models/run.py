"""Typed run-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import ValidatedJsonValue, WireModel
from .legacy import LegacyMetric


class RunError(WireModel):
    """Represent the RunError object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/error'
    code: str
    message: str


class RunBaselineRun(WireModel):
    """Represent the RunBaselineRun object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/run'
    id: str
    revision: int | float
    status: Literal[
        'running',
        'stopping',
        'interrupted',
        'completed',
        'completed_with_errors',
        'failed',
    ]
    created_at: str
    updated_at: str
    finished_at: str | None
    current_invocation_id: str
    tags: list[str]
    error: RunError | None


class RunBaselineBasket(WireModel):
    """Represent the RunBaselineBasket object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/basket'
    sha256: str
    path: str
    partial: bool
    total_tasks: int | float
    omitted_failed_count: int | float


class RunBaselineAgentResolvedRuntime(WireModel):
    """Represent the RunBaselineAgentResolvedRuntime object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/agent/properties/resolved_runtime/anyOf/0'
    version: str
    asset_name: str
    platform: str
    archive_sha256: str
    binary_sha256: str
    receipt: str


class RunBaselineAgent(WireModel):
    """Represent the RunBaselineAgent object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/agent'
    name: str
    runtime: str
    resolved_runtime: RunBaselineAgentResolvedRuntime | None
    # Historical v1 omits this field; the v2 ingress schema requires it.
    python_runtime: 'RunAgentInputConfig | None' = None
    auth_mode: Literal['api_key', 'chatgpt_oauth']


class RunBaselineConfig(WireModel):
    """Represent the RunBaselineConfig object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/config'
    identity_version: int | float
    identity_sha256: str
    identity_path: str


class RunBaselineMetric(WireModel):
    """Represent the RunBaselineMetric object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/metric'
    name: Literal['mlp']
    version: str
    pythonhashseed: int | float


class RunBaselineLastRescore(WireModel):
    """Represent the RunBaselineLastRescore object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/last_rescore/anyOf/0'
    at: str
    invocation_id: str
    previous_report_sha256: str
    backup_path: str
    receipt_path: str


class RunBaselineLastReprice(WireModel):
    """Represent the RunBaselineLastReprice object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/last_reprice/anyOf/0'
    at: str
    invocation_id: str
    calculation_id: str
    previous_report_sha256: str
    backup_path: str
    receipt_path: str


class RunInferenceValueSource(WireModel):
    """Represent the RunInferenceValueSource object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/inference_value/properties/source'
    ref: str
    field: str


class RunInferenceValue(WireModel):
    """Represent the RunInferenceValue object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/inference_value'
    status: Literal['known', 'unknown', 'unsupported', 'mixed']
    value: str | None
    reason: str | None
    source: RunInferenceValueSource | None


class RunBaselineInferenceConfigured(WireModel):
    """Represent the RunBaselineInferenceConfigured object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/inference/properties/configured'
    provider: RunInferenceValue
    model: RunInferenceValue
    effort: RunInferenceValue


class RunBaselineInferenceEffectiveAttemptItem(WireModel):
    """Represent the RunBaselineInferenceEffectiveAttemptItem object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = (
        '#/properties/baseline/properties/inference/properties/effective/properties/attempts/items'
    )
    attempt_id: str
    source_ref: str
    provider: RunInferenceValue
    model: RunInferenceValue
    effort: RunInferenceValue


class RunBaselineInferenceEffective(WireModel):
    """Represent the RunBaselineInferenceEffective object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/inference/properties/effective'
    provider: RunInferenceValue
    model: RunInferenceValue
    effort: RunInferenceValue
    attempts: list[RunBaselineInferenceEffectiveAttemptItem]


class RunBaselineInference(WireModel):
    """Represent the RunBaselineInference object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/inference'
    schema_version: int | float
    configured: RunBaselineInferenceConfigured
    effective: RunBaselineInferenceEffective
    discrepancy: bool


class RunBaselineState(WireModel):
    """Represent the RunBaselineState object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline/properties/state'
    path: Literal['_meta/state.json']
    sha256: str


class RunBaseline(WireModel):
    """Represent the RunBaseline object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline'
    schema_family: Literal['pr_similarity_os.report']
    schema_version: int | float
    run: RunBaselineRun
    basket: RunBaselineBasket
    agent: RunBaselineAgent
    config: RunBaselineConfig
    metric: RunBaselineMetric | LegacyMetric
    last_rescore: RunBaselineLastRescore | None
    last_reprice: RunBaselineLastReprice | None
    inference: RunBaselineInference
    state: RunBaselineState | None = None


class RunTaskItem(WireModel):
    """Represent the RunTaskItem object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items'
    id: str
    ordinal: int | float
    key: str
    path: str
    basket_row: dict[str, ValidatedJsonValue]


class RunAgentInputConfig(WireModel):
    """Represent the RunAgentInputConfig object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/agent_input/properties/config'
    path: str
    size_bytes: int | float
    sha256: str


RunAgentInputResourceItem = RunAgentInputConfig


class RunAgentInput(WireModel):
    """Represent the RunAgentInput object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/agent_input'
    config: RunAgentInputConfig
    resources: list[RunAgentInputResourceItem]


class Run(WireModel):
    """Represent the Run object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_family: Literal['pr_similarity_os.run']
    schema_version: int | float
    id: str
    baseline: RunBaseline
    tasks: list[RunTaskItem]
    agent_input: RunAgentInput


Run.model_rebuild()
