"""Typed report-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import WireModel
from .legacy import LegacyApiEquivalent, LegacyMetric


class ReportError(WireModel):
    """Represent the ReportError object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/error'
    code: str
    message: str


class ReportRun(WireModel):
    """Represent the ReportRun object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/run'
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
    error: ReportError | None


class ReportBasket(WireModel):
    """Represent the ReportBasket object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/basket'
    sha256: str
    path: str
    partial: bool
    total_tasks: int | float
    omitted_failed_count: int | float


class ReportAgentResolvedRuntime(WireModel):
    """Represent the ReportAgentResolvedRuntime object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/agent/properties/resolved_runtime/anyOf/0'
    version: str
    asset_name: str
    platform: str
    archive_sha256: str
    binary_sha256: str
    receipt: str


class ReportAgent(WireModel):
    """Represent the ReportAgent object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/agent'
    name: str
    runtime: str
    resolved_runtime: ReportAgentResolvedRuntime | None
    # Historical v1 omits this field; the v2 ingress schema requires it.
    python_runtime: 'ReportAgentInputConfig | None' = None
    auth_mode: Literal['api_key', 'chatgpt_oauth']


class ReportConfig(WireModel):
    """Represent the ReportConfig object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/config'
    identity_version: int | float
    identity_sha256: str
    identity_path: str


class ReportMetric(WireModel):
    """Represent the ReportMetric object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/metric'
    name: Literal['mlp']
    version: str
    pythonhashseed: int | float


class ReportSummary(WireModel):
    """Represent the ReportSummary object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/summary'
    total_tasks: int | float
    completed_tasks: int | float
    pending_tasks: int | float
    active_tasks: int | float
    retry_pending_tasks: int | float
    interrupted_tasks: int | float
    infrastructure_error_tasks: int | float
    timeout_tasks: int | float
    error_tasks: int | float
    cancelled_tasks: int | float
    nonempty_diff_tasks: int | float
    empty_diff_tasks: int | float
    unknown_diff_tasks: int | float
    scored_tasks: int | float


class ReportAttempt(WireModel):
    """Represent the ReportAttempt object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/attempt'
    id: str
    invocation_id: str
    sequence: int | float
    retry_index: int | float
    started_at: str
    finished_at: str | None


class ReportPhase(WireModel):
    """Represent the ReportPhase object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/phase'
    status: Literal[
        'not_started',
        'running',
        'completed',
        'timeout',
        'error',
        'cancelled',
        'skipped',
        'unknown',
    ]
    exit_code: int | float | None
    duration_seconds: int | float | None


class ReportTaskExecution(WireModel):
    """Represent the ReportTaskExecution object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/task/properties/execution'
    status: Literal[
        'not_started',
        'running',
        'completed',
        'timeout',
        'error',
        'cancelled',
        'unknown',
    ]
    exit_code: int | float | None
    duration_seconds: int | float | None


class ReportInfra(WireModel):
    """Represent the ReportInfra object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/infra'
    stage: Literal['prepare', 'setup', 'agent', 'capture', 'score']
    code: str
    message: str
    paths: list[str]


class ReportTaskChanges(WireModel):
    """Represent the ReportTaskChanges object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/task/properties/changes'
    status: Literal['unknown', 'empty', 'nonempty']
    files_count: int | float | None
    diff: str | None


class ReportCounts(WireModel):
    """Represent the ReportCounts object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/counts'
    human_files: int | float
    agent_files: int | float
    matched_files: int | float
    missed_files: int | float
    extra_files: int | float
    human_loc: int | float
    agent_loc: int | float
    loc_weight: int | float


class ReportFileExists(WireModel):
    """Represent the ReportFileExists object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/file/properties/exists'
    base: bool
    human: bool
    ai: bool


class ReportFile(WireModel):
    """Represent the ReportFile object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/file'
    path: str
    changed_human: bool
    changed_agent: bool
    human_loc: int | float
    agent_loc: int | float
    loc_weight: int | float
    mlp: int | float
    branch: Literal['code', 'struct', 'default']
    base: str | None
    human: str | None
    ai: str | None
    exists: ReportFileExists


class ReportTaskScoring(WireModel):
    """Represent the ReportTaskScoring object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/task/properties/scoring'
    status: Literal['pending', 'available', 'unavailable']
    error: ReportError | None
    Mf_mlp: int | float | None
    LCf_mlp: int | float | None
    counts: ReportCounts | None
    files: list[ReportFile]


class ReportTelemetryTokens(WireModel):
    """Represent the ReportTelemetryTokens object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/telemetry/properties/tokens'
    input: int | float | None
    output: int | float | None
    reasoning: int | float | None
    cache_read: int | float | None
    cache_write: int | float | None
    total: int | float | None


class ReportTelemetryCost(WireModel):
    """Represent the ReportTelemetryCost object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/telemetry/properties/cost'
    amount: int | float | None
    currency: Literal['USD']
    source: Literal['opencode']


class ReportTelemetrySessionDb(WireModel):
    """Represent the ReportTelemetrySessionDb object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/telemetry/properties/session_db/anyOf/0'
    path: str
    sha256: str


ReportTelemetryUsageLedger = ReportTelemetrySessionDb


class ReportTelemetrySessionCounts(WireModel):
    """Represent the ReportTelemetrySessionCounts object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/telemetry/properties/session_counts/anyOf/0'
    root: int | float
    subagent: int | float
    detached: int | float


ReportTelemetryDiagnosticItem = ReportError


class ReportTelemetryApiEquivalent(WireModel):
    """Represent the ReportTelemetryApiEquivalent object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/telemetry/properties/api_equivalent/anyOf/0'
    status: Literal['complete', 'partial', 'unavailable']
    amount: int | float | None
    currency: Literal['USD']
    basis: Literal['api_equivalent']
    calculation_id: str
    pricing_sha256: str
    calculator_version: str
    usage_sha256: str | None
    priced_requests: int | float
    total_requests: int | float | None


class ReportTelemetry(WireModel):
    """Represent the ReportTelemetry object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/telemetry'
    source: Literal['opencode']
    status: Literal['complete', 'partial', 'unavailable']
    tokens: ReportTelemetryTokens
    total_source: Literal['reported', 'derived_disjoint', 'unavailable']
    cost: ReportTelemetryCost
    transport: Literal['db', 'stream', 'none']
    session_db: ReportTelemetrySessionDb | None
    usage_ledger: ReportTelemetryUsageLedger | None
    session_counts: ReportTelemetrySessionCounts | None
    diagnostics: list[ReportTelemetryDiagnosticItem]
    api_equivalent: ReportTelemetryApiEquivalent | LegacyApiEquivalent | None


class ReportArtifactsEffectiveConfig(WireModel):
    """Represent the ReportArtifactsEffectiveConfig object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/artifacts/properties/effective_config'
    status: Literal['pending', 'saved', 'warning', 'not_started']
    path: str | None
    message: str | None


class ReportArtifacts(WireModel):
    """Represent the ReportArtifacts object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/artifacts'
    outcome: str | None
    triplets_manifest: str | None
    setup_log: str | None
    agent_events: str | None
    agent_log: str | None
    effective_config: ReportArtifactsEffectiveConfig


class ReportSourceDescriptor(WireModel):
    """Represent the ReportSourceDescriptor object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/source_descriptor'
    vcs: Literal['git']
    fetch_url: str
    fetch_refs: list[str]


class ReportTaskProgress(WireModel):
    """Represent the ReportTaskProgress object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/task/properties/progress'
    phase: str
    updated_at: str | None


class ReportTask(WireModel):
    """Represent the ReportTask object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/task'
    id: str
    repository: str
    number: int | float
    url: str
    title: str
    before: str
    after: str
    workspace: Literal['.'] | str
    agent_task: str
    processing_status: Literal[
        'pending',
        'preparing',
        'running',
        'capturing',
        'scoring',
        'retry_pending',
        'completed',
        'interrupted',
    ]
    final_attempt: ReportAttempt | None
    setup: ReportPhase
    execution: ReportTaskExecution
    infrastructure_error: ReportInfra | None
    changes: ReportTaskChanges
    scoring: ReportTaskScoring
    telemetry: ReportTelemetry
    artifacts: ReportArtifacts
    before_tree: str
    after_tree: str
    source: ReportSourceDescriptor
    kind: Literal['pull_request', 'merge_request']
    diff_policy: Literal['git_source_changes_v1']
    ordinal: int | float
    directory: str
    progress: ReportTaskProgress
    locations: dict[str, str]


class ReportLastRescore(WireModel):
    """Represent the ReportLastRescore object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/last_rescore/anyOf/0'
    at: str
    invocation_id: str
    previous_report_sha256: str
    backup_path: str
    receipt_path: str


class ReportLastReprice(WireModel):
    """Represent the ReportLastReprice object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/last_reprice/anyOf/0'
    at: str
    invocation_id: str
    calculation_id: str
    previous_report_sha256: str
    backup_path: str
    receipt_path: str


class ReportInferenceValueSource(WireModel):
    """Represent the ReportInferenceValueSource object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/inference_value/properties/source'
    ref: str
    field: str


class ReportInferenceValue(WireModel):
    """Represent the ReportInferenceValue object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/inference_value'
    status: Literal['known', 'unknown', 'unsupported', 'mixed']
    value: str | None
    reason: str | None
    source: ReportInferenceValueSource | None


class ReportInferenceConfigured(WireModel):
    """Represent the ReportInferenceConfigured object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/inference/properties/configured'
    provider: ReportInferenceValue
    model: ReportInferenceValue
    effort: ReportInferenceValue


class ReportInferenceEffectiveAttemptItem(WireModel):
    """Represent the ReportInferenceEffectiveAttemptItem object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/inference/properties/effective/properties/attempts/items'
    attempt_id: str
    source_ref: str
    provider: ReportInferenceValue
    model: ReportInferenceValue
    effort: ReportInferenceValue


class ReportInferenceEffective(WireModel):
    """Represent the ReportInferenceEffective object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/inference/properties/effective'
    provider: ReportInferenceValue
    model: ReportInferenceValue
    effort: ReportInferenceValue
    attempts: list[ReportInferenceEffectiveAttemptItem]


class ReportInference(WireModel):
    """Represent the ReportInference object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/inference'
    schema_version: int | float
    configured: ReportInferenceConfigured
    effective: ReportInferenceEffective
    discrepancy: bool


class ReportState(WireModel):
    """Represent the ReportState object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/state'
    path: Literal['_meta/state.json']
    sha256: str


class ReportCostsFinalAttemptsNativeReportedCoverage(WireModel):
    """Represent the ReportCostsFinalAttemptsNativeReportedCoverage object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = (
        '#/properties/costs/properties/final_attempts/properties/native_reported/properties/coverage'
    )
    total_attempts: int | float
    complete_attempts: int | float
    known_attempts: int | float
    known_requests: int | float
    missing_tasks: int | float
    total_requests: int | float | None


class ReportCostsFinalAttemptsNativeReported(WireModel):
    """Represent the ReportCostsFinalAttemptsNativeReported object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs/properties/final_attempts/properties/native_reported'
    status: Literal['complete', 'partial', 'unavailable', 'not_started', 'not_requested']
    currency: Literal['USD']
    amount: str | None
    known_subtotal: str
    coverage: ReportCostsFinalAttemptsNativeReportedCoverage


ReportCostsFinalAttemptsApiEquivalentCoverage = ReportCostsFinalAttemptsNativeReportedCoverage


class ReportCostsFinalAttemptsApiEquivalent(WireModel):
    """Represent the ReportCostsFinalAttemptsApiEquivalent object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs/properties/final_attempts/properties/api_equivalent'
    status: Literal['complete', 'partial', 'unavailable', 'not_started', 'not_requested']
    currency: Literal['USD']
    amount: str | None
    known_subtotal: str
    coverage: ReportCostsFinalAttemptsApiEquivalentCoverage


class ReportCostsFinalAttempts(WireModel):
    """Represent the ReportCostsFinalAttempts object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs/properties/final_attempts'
    native_reported: ReportCostsFinalAttemptsNativeReported
    api_equivalent: ReportCostsFinalAttemptsApiEquivalent


ReportCostsAllAttemptsNativeReportedCoverage = ReportCostsFinalAttemptsNativeReportedCoverage


class ReportCostsAllAttemptsNativeReported(WireModel):
    """Represent the ReportCostsAllAttemptsNativeReported object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs/properties/all_attempts/properties/native_reported'
    status: Literal['complete', 'partial', 'unavailable', 'not_started', 'not_requested']
    currency: Literal['USD']
    amount: str | None
    known_subtotal: str
    coverage: ReportCostsAllAttemptsNativeReportedCoverage


ReportCostsAllAttemptsApiEquivalentCoverage = ReportCostsFinalAttemptsNativeReportedCoverage


class ReportCostsAllAttemptsApiEquivalent(WireModel):
    """Represent the ReportCostsAllAttemptsApiEquivalent object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs/properties/all_attempts/properties/api_equivalent'
    status: Literal['complete', 'partial', 'unavailable', 'not_started', 'not_requested']
    currency: Literal['USD']
    amount: str | None
    known_subtotal: str
    coverage: ReportCostsAllAttemptsApiEquivalentCoverage


class ReportCostsAllAttempts(WireModel):
    """Represent the ReportCostsAllAttempts object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs/properties/all_attempts'
    native_reported: ReportCostsAllAttemptsNativeReported
    api_equivalent: ReportCostsAllAttemptsApiEquivalent


class ReportCosts(WireModel):
    """Represent the ReportCosts object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/costs'
    final_attempts: ReportCostsFinalAttempts
    all_attempts: ReportCostsAllAttempts


class ReportAgentInputConfig(WireModel):
    """Represent the ReportAgentInputConfig object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/agent_input/properties/config'
    path: str
    size_bytes: int | float
    sha256: str


ReportAgentInputResourceItem = ReportAgentInputConfig


class ReportAgentInput(WireModel):
    """Represent the ReportAgentInput object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/agent_input'
    config: ReportAgentInputConfig
    resources: list[ReportAgentInputResourceItem]


class Report(WireModel):
    """Represent the Report object in the report-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_family: Literal['pr_similarity_os.report']
    schema_version: int | float
    run: ReportRun
    basket: ReportBasket
    agent: ReportAgent
    config: ReportConfig
    metric: ReportMetric | LegacyMetric
    summary: ReportSummary
    tasks: list[ReportTask]
    last_rescore: ReportLastRescore | None
    last_reprice: ReportLastReprice | None
    inference: ReportInference
    state: ReportState
    costs: ReportCosts
    agent_input: ReportAgentInput


Report.model_rebuild()
