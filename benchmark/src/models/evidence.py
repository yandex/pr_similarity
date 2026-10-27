"""Producer-owned immutable attempt, operation and publication artifacts."""

from pydantic import Field

from .base import ArtifactReceipt, ValidatedJsonObject, WireModel
from .report import (
    ReportAgentInput,
    ReportArtifactsEffectiveConfig,
    ReportAttempt,
    ReportError,
    ReportInference,
    ReportMetric,
    ReportTaskScoring,
    ReportTelemetryApiEquivalent,
    TaskDraft,
)
from .scoring import Diagnostic


class IdentitySetup(WireModel):
    """Freeze setup content and its independent execution timeout."""

    sha256: str
    timeout_seconds: int | float


class IdentityInstructions(WireModel):
    """Freeze prompt protocol and instruction content."""

    protocol: str
    sha256: str


class ResourceFile(WireModel):
    """Bind one resource-relative file to its exact bytes."""

    path: str
    sha256: str


class IdentityResource(WireModel):
    """Bind one configured local resource and its complete file inventory."""

    field: str
    index: int
    files: list[ResourceFile]


class IdentityRetries(WireModel):
    """Freeze the native retry budget and permitted categories."""

    max_retries: int
    categories: list[str]


class ConfigIdentity(WireModel):
    """Freeze all runtime, native configuration and harness identity inputs."""

    identity_version: int
    runtime: str
    auth_mode: str
    native_config: ValidatedJsonObject
    timeout_seconds: int | float
    setup: IdentitySetup | None
    instructions: IdentityInstructions
    resources: list[IdentityResource]
    retries: IdentityRetries
    diff_excludes: list[str]
    env: dict[str, str]


class EffectiveConfig(WireModel):
    """Associate one captured native config with its exact attempt source."""

    attempt_id: str
    source_ref: str
    config: ValidatedJsonObject


class NativeIdentity(WireModel):
    """Carry the frozen execution inputs supplied to a native worker."""

    run_id: str
    basket_sha256: str
    config_identity_sha256: str
    metric: ReportMetric
    runtime_binary_sha256: str
    python_runtime: ArtifactReceipt | None = None


class NativeOwner(WireModel):
    """Identify the frozen run inputs that own a persisted native artifact."""

    schema_family: str
    schema_version: int
    run_id: str
    basket_sha256: str
    config_identity_sha256: str


class Start(NativeOwner):
    """Reserve native ownership durably before executing any process."""

    task_id: str
    attempt: ReportAttempt


class NativeTiming(WireModel):
    """Retain native timing independently from setup and host scoring."""

    started_at: str | None
    finished_at: str | None
    duration_seconds: int | float | None


class StreamReceipt(WireModel):
    """Record independent completeness of each captured native stream."""

    schema_version: int
    status: str
    events_complete: bool
    runtime_complete: bool
    transcript_complete: bool
    events_count: int
    runtime_lines: int
    warnings: list[str]


class EvidenceStatus(WireModel):
    """Record the emitted fields for one native evidence component."""

    status: str
    reason: str | None = None
    code: str | None = None
    warnings: list[str] = Field(default_factory=list)
    archive_complete: bool | None = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    receipt: StreamReceipt | None = None


class SealEvidence(WireModel):
    """Keep independently captured evidence and its availability reasons."""

    setup: EvidenceStatus | None = None
    usage: EvidenceStatus | None = None
    native_error: EvidenceStatus | None = None
    source: EvidenceStatus | None = None
    declared_resources: EvidenceStatus | None = None
    inventory: EvidenceStatus | None = None
    streams: EvidenceStatus | None = None
    configured: EvidenceStatus | None = None
    effective: ReportArtifactsEffectiveConfig | None = None
    required: EvidenceStatus | None = None


class Seal(NativeOwner):
    """Freeze terminal native facts before host scoring begins."""

    metric: ReportMetric
    agent_input: ReportAgentInput
    task: TaskDraft
    runtime_binary_sha256: str
    python_runtime: ArtifactReceipt | None = None
    native_timing: NativeTiming
    archive_complete: bool
    evidence: SealEvidence
    artifact_receipts: list[ArtifactReceipt]


class ScoringEvidence(WireModel):
    """Bind a host scoring result to one immutable native seal."""

    schema_family: str
    schema_version: int
    run_id: str
    task_id: str
    attempt_id: str
    seal: ArtifactReceipt
    scoring: ReportTaskScoring
    metric: ReportMetric
    diagnostics: list[Diagnostic]
    error: ReportError | None


class Outcome(NativeOwner):
    """Close native work with its separately signed initial host score."""

    metric: ReportMetric
    agent_input: ReportAgentInput
    task: TaskDraft
    seal: ArtifactReceipt
    scoring_result: ArtifactReceipt
    scoring_error: ReportError | None
    runtime_binary_sha256: str
    python_runtime: ArtifactReceipt | None = None
    fatal: ReportError | None
    archive_complete: bool
    artifact_receipts: list[ArtifactReceipt]


class ScoreItem(WireModel):
    """Bind one offline scoring result to its original native owner."""

    task_id: str
    attempt_id: str
    scoring: ReportTaskScoring
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    error: ReportError | None = None


class PriceRequest(WireModel):
    """Keep exact request cost and an explicit reason for unknown prices."""

    id: str
    amount: str | None
    reason: str | None


class PriceResult(WireModel):
    """Persist exact decimal cost strings and independent coverage counts."""

    status: str
    amount: str | None
    known_subtotal: str
    priced_requests: int
    total_requests: int | None
    requests: list[PriceRequest]
    reason: str | None = None


class PriceInputs(WireModel):
    """Identify each signed native input used by one cost calculation."""

    reason: str | None
    usage: ArtifactReceipt | None
    receipt: ArtifactReceipt | None
    db: ArtifactReceipt | None


class PriceItem(WireModel):
    """Bind exact pricing and its public estimate to a native attempt."""

    task_id: str
    attempt_id: str
    sequence: int
    result: PriceResult
    api_equivalent: ReportTelemetryApiEquivalent
    inputs: PriceInputs


class PriceCoverage(WireModel):
    """Persist an exact subtotal with its full attempt and request population."""

    status: str
    amount: str | None
    known_subtotal: str
    total_attempts: int
    complete_attempts: int
    missing_tasks: int
    priced_requests: int
    total_requests: int | None


class OperationCoverage(WireModel):
    """Keep final-attempt and historical pricing populations independent."""

    final: PriceCoverage
    all_attempts: PriceCoverage


class OperationDetails(WireModel):
    """Keep optional operation-specific provenance without inventing absent fields."""

    error: ReportError | None = None
    source: str | None = None
    task_id: str | None = None
    request: ArtifactReceipt | None = None
    inputs_identity_sha256: str | None = None
    coverage: OperationCoverage | None = None
    frozen_artifacts: list[ArtifactReceipt] = Field(default_factory=list)


class OperationOwner(NativeOwner):
    """Identify one immutable host operation against the frozen experiment."""

    id: str
    kind: str


class OperationResult(OperationOwner):
    """Store only the score or pricing item collection emitted by the operation."""

    metric: ReportMetric | None
    tasks: list[ScoreItem] | None = None
    attempts: list[PriceItem] | None = None


class OperationReceipt(OperationOwner):
    """Close one host operation over exact input state and report bytes."""

    status: str
    invocation_id: str
    finished_at: str
    result: ArtifactReceipt
    input_state: ArtifactReceipt
    input_report: ArtifactReceipt
    details: OperationDetails


class OperationSelection(WireModel):
    """Select a verified immutable operation at its accepted state revision."""

    id: str
    kind: str
    accepted_revision: int
    result: ArtifactReceipt
    receipt: ArtifactReceipt


class PricingRequest(OperationOwner):
    """Freeze pricing policy and calculator before performing a calculation."""

    invocation_id: str
    calculation_id: str
    policy_path: str
    policy_sha256: str
    calculator_version: str
    frozen_artifacts: list[ArtifactReceipt]
    status: str
    request_reference: ArtifactReceipt | None = None


class PublishFingerprint(WireModel):
    """Bind an idempotent publication to server, bytes and overwrite intent."""

    server: str
    run_id: str
    archive_sha256: str
    report_sha256: str
    overwrite: bool


class BenchmarkSettings(WireModel):
    """Project portable harness settings for publication readback."""

    runtime: str | None
    timeout_seconds: int | float | None
    setup_timeout_seconds: int | float | None
    retries: IdentityRetries | None
    diff_excludes: list[str] | None


class PublishMetadata(WireModel):
    """Freeze requested labels, native input and the complete public task queue."""

    display_name: str
    tags: list[str]
    basket_name: str
    inference: ReportInference
    agent_input: ReportAgentInput
    benchmark: BenchmarkSettings
    task_ids: list[str]


class PublishRequest(NativeOwner):
    """Freeze one exact upload request before acquiring a remote grant."""

    id: str
    idempotency_key: str
    fingerprint: PublishFingerprint
    fingerprint_sha256: str
    payload: ArtifactReceipt
    created_at: str
    metadata: PublishMetadata


class ServerReceipt(WireModel):
    """Retain the server's exact accepted publication identity."""

    run_id: str
    archive_sha256: str
    report_sha256: str
    idempotency_key: str
    author_login: str


class PublicMetadata(WireModel):
    """Retain verified public display settings and experiment facts."""

    display_name: str
    tags: list[str]
    tag_groups: dict[str, list[str]]
    inference: ReportInference
    pinned: bool | None
    config_source: ArtifactReceipt
    config_supported: bool
    benchmark: BenchmarkSettings


class PublishReceipt(WireModel):
    """Close a publication with matching confirmation or explicit rejection."""

    schema_family: str
    schema_version: int
    id: str
    run_id: str
    status: str
    finished_at: str
    request: ArtifactReceipt
    server_receipt: ServerReceipt | None
    public_metadata: PublicMetadata | None


class ReservationNotice(WireModel):
    """Identify an intact start recovered without launching native work."""

    task_id: str
    attempt_id: str
    sequence: int


class RuntimeReceipt(WireModel):
    """Retain the executable identity consumed by the frozen run projection."""

    actual_version: str
    asset_name: str
    platform: str
    archive_sha256: str
    binary_sha256: str
