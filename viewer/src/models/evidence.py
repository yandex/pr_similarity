"""Typed immutable native and host evidence consumed by the viewer.

These objects retain optional producer fields and exact JSON numbers; semantic
ownership and digest validation remains at the archive boundary.
"""

from typing import Literal, Protocol

from pydantic import Field, ValidationError, field_validator, model_validator

from viewer.errors import ViewerError
from viewer.util import JsonObject, safe_path

from .base import ValidatedJsonObject, WireModel
from .legacy import LegacyApiEquivalent, LegacyMetric
from .report import (
    ReportAgentInput,
    ReportArtifactsEffectiveConfig,
    ReportAttempt,
    ReportError,
    ReportFileExists,
    ReportInference,
    ReportInfra,
    ReportMetric,
    ReportPhase,
    ReportSourceDescriptor,
    ReportTaskChanges,
    ReportTaskExecution,
    ReportTaskScoring,
    ReportTelemetryApiEquivalent,
    ReportTelemetryUsageLedger,
)


class ArtifactReceipt(WireModel):
    """Identify exact immutable bytes within an archive."""

    path: str
    size_bytes: int
    sha256: str


class NativeOwner(WireModel):
    """Bind a native attempt to its frozen inputs."""

    schema_version: int
    run_id: str
    basket_sha256: str
    config_identity_sha256: str


class StartAttempt(WireModel):
    """Read reservation fields while preserving unused completion metadata."""

    id: str | None = None
    invocation_id: str | None = None
    sequence: int | float | bool | None = None
    retry_index: int | float | bool | None = None
    started_at: str | None = None


class Start(NativeOwner):
    """Describe an allocated native attempt."""

    schema_family: Literal['pr_similarity_os.start']
    task_id: str
    attempt: StartAttempt


class NativeTiming(WireModel):
    """Retain execution timestamps and measured duration."""

    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: int | float | None = None


class StreamReceipt(WireModel):
    """Record whether each native output stream was captured completely."""

    schema_version: int
    status: str
    events_complete: bool
    runtime_complete: bool
    transcript_complete: bool
    events_count: int
    runtime_lines: int
    warnings: list[str]


class EvidenceStatus(WireModel):
    """Describe one independently captured evidence component."""

    status: str
    reason: str | None = None
    code: str | None = None
    warnings: list[str] = Field(default_factory=list)
    archive_complete: bool | None = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    receipt: StreamReceipt | None = None


class SealEvidence(WireModel):
    """Keep native capture completeness separately for each evidence source."""

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


class NativeTelemetry(WireModel):
    """Read selected native cost references while retaining other telemetry facts."""

    usage_ledger: ReportTelemetryUsageLedger | None = None
    api_equivalent: ReportTelemetryApiEquivalent | LegacyApiEquivalent | None


class NativeTask(WireModel):
    """Read native execution facts, retaining unused public metadata verbatim."""

    id: str
    final_attempt: ReportAttempt | None
    setup: ReportPhase
    execution: ReportTaskExecution
    changes: ReportTaskChanges
    telemetry: NativeTelemetry
    infrastructure_error: ReportInfra | None


class OutcomeTask(NativeTask):
    """Read the initial host score added to the immutable native task."""

    scoring: ReportTaskScoring


class Seal(NativeOwner):
    """Read native task ownership, postponing branch-specific receipt inspection."""

    schema_family: Literal['pr_similarity_os.seal']
    python_runtime: ArtifactReceipt | None = None
    task: NativeTask
    metric: ReportMetric | LegacyMetric | None = None

    def matches_agent_input(self, expected: WireModel) -> bool:
        """Compare the frozen input snapshot at its original semantic validation gate."""
        return self.to_wire().get('agent_input') == expected.to_wire()

    def cost_receipt(self, path: str) -> ArtifactReceipt | None:
        """Parse only the signed usage receipt actually consulted by cost projection."""
        rows = self.to_wire()['artifact_receipts']
        if not isinstance(rows, list):
            raise TypeError('Native artifact receipts must be a list.')
        selected = None
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError('Native artifact receipt must be an object.')
            candidate = row['path']
            if isinstance(candidate, (list, dict)):
                raise TypeError(f'unhashable type: {type(candidate).__name__!r}')
            if candidate == path:
                selected = row
        return None if selected is None else parse_evidence(ArtifactReceipt, selected)

    def cost_started(self, fallback: bool) -> bool:
        """Use native timing only when the original evidence contains a timing object."""
        timing = self.to_wire().get('native_timing')
        if isinstance(timing, dict):
            return timing['started_at'] is not None
        return fallback


class Diagnostic(ReportError):
    """Describe a scoring diagnostic, optionally attached to a source path."""

    path: str | None = None


class ScoringEvidence(WireModel):
    """Bind an initial host score to its immutable native seal."""

    schema_family: Literal['pr_similarity_os.scoring']
    schema_version: int
    run_id: str
    task_id: str
    attempt_id: str
    seal: ArtifactReceipt
    metric: ReportMetric | LegacyMetric
    scoring: ReportTaskScoring

    def matches_error(self, outcome: WireModel) -> bool:
        """Compare opaque host diagnostics without imposing an unused error schema."""
        return self.to_wire().get('error') == outcome.to_wire().get('scoring_error')


class Outcome(WireModel):
    """Combine the sealed native task with its first scoring result."""

    schema_family: Literal['pr_similarity_os.outcome']
    python_runtime: ArtifactReceipt | None = None
    schema_version: int
    run_id: str
    task: OutcomeTask
    seal: ArtifactReceipt
    scoring_result: ArtifactReceipt
    metric: ReportMetric | LegacyMetric

    def has_scoring_receipt(self, receipt: WireModel) -> bool:
        """Check only membership in the original artifact receipt list."""
        receipts = self.to_wire().get('artifact_receipts', [])
        if not isinstance(receipts, list):
            raise TypeError('Outcome artifact receipts must be a list.')
        return receipt.to_wire() in receipts


class RuntimeReceipt(WireModel):
    """Identify the downloaded and probed native runtime."""

    actual_version: str
    asset_name: str
    platform: str
    archive_sha256: str
    binary_sha256: str


class TripletSource(WireModel):
    """Bind a triplet collection to before and after revisions."""

    before: str
    after: str


class TripletModes(WireModel):
    """Keep executable bits independently for all three roles."""

    base: str | None
    human: str | None
    ai: str | None


class TripletFiles(WireModel):
    """Keep explicit absence distinct from a receipt for an empty file."""

    base: ArtifactReceipt | None
    human: ArtifactReceipt | None
    ai: ArtifactReceipt | None


class TripletEntry(WireModel):
    """Describe one source path and its three captured file variants."""

    path: str
    number: int
    exists: ReportFileExists
    modes: TripletModes
    files: TripletFiles


class TripletIndex(WireModel):
    """Own the ordered source variants captured for a native attempt."""

    version: int
    run_id: str
    task_id: str
    attempt_id: str
    source: TripletSource
    entries: list[TripletEntry]


class IdentitySetup(WireModel):
    """Bind setup bytes and its configured timeout."""

    sha256: str


class IdentityInstructions(WireModel):
    """Bind instructions independently from native configuration."""

    sha256: str


class ResourceFile(WireModel):
    """Identify a file relative to a configured local resource."""

    path: str
    sha256: str


class IdentityResource(WireModel):
    """Bind a native resource reference to its content inventory."""

    field: str | int | float | bool | None
    index: str | int | float | bool | None


class IdentityRetries(WireModel):
    """Retain retry controls without inventing missing settings."""

    max_retries: int | float | None = None
    categories: list[str] | None = None


class ConfigIdentity(WireModel):
    """Read semantic input fields; unrelated harness metadata remains untouched."""

    native_config: ValidatedJsonObject
    instructions: IdentityInstructions | None = None
    resources: list[IdentityResource] | None = None
    setup: IdentitySetup | None = None


class OperationOwner(NativeOwner):
    """Identify a host operation and its native input population."""

    id: str
    kind: Literal['rescore', 'reprice']


class ScoreItem(WireModel):
    """Store one host rescore result."""

    task_id: str
    attempt_id: str
    scoring: ReportTaskScoring


class PriceRequest(WireModel):
    """Retain an exact decimal price or an explicit coverage reason."""

    id: str
    amount: str | None
    reason: str | None


class PriceResult(WireModel):
    """Retain unrounded decimal strings from the pricing calculator."""

    status: str
    known_subtotal: str | int | float | bool
    priced_requests: int | float | bool
    total_requests: int | float | bool | None


class PriceInputs(WireModel):
    """Bind pricing to the original native usage and sanitized database."""

    usage: ArtifactReceipt | None


class PriceItem(WireModel):
    """Store one host reprice result with its exact input receipts."""

    task_id: str
    attempt_id: str
    api_equivalent: ReportTelemetryApiEquivalent | LegacyApiEquivalent
    result: PriceResult | None = None
    inputs: PriceInputs | None = None


class OperationResult(OperationOwner):
    """Contain either task scores or attempt prices for a host operation."""

    schema_family: Literal['pr_similarity_os.operation_result']
    metric: ReportMetric | LegacyMetric | None = None
    tasks: list[ScoreItem] | None = None
    attempts: list[PriceItem] | None = None


class PriceCoverage(WireModel):
    """Summarize exact decimal totals and pricing coverage."""

    status: str
    amount: str | None
    known_subtotal: str
    total_attempts: int
    complete_attempts: int
    missing_tasks: int
    priced_requests: int
    total_requests: int | None


class OperationCoverage(WireModel):
    """Separate final attempts from the complete execution history."""

    final: PriceCoverage
    all_attempts: PriceCoverage


class OperationDetails(WireModel):
    """Capture pricing provenance or a failed operation's error."""

    error: ReportError | None = None
    request: ArtifactReceipt | None = None
    inputs_identity_sha256: str | None = None
    frozen_artifacts: list[ArtifactReceipt] = Field(default_factory=list)
    coverage: OperationCoverage | None = None


class OperationReceipt(OperationOwner):
    """Close a host operation against its exact frozen snapshots."""

    schema_family: Literal['pr_similarity_os.operation_receipt']
    status: str
    invocation_id: str | None = None
    finished_at: str | None = None
    result: ArtifactReceipt
    input_state: ArtifactReceipt
    input_report: ArtifactReceipt


class PublishFingerprint(WireModel):
    """Bind an idempotent request to server, bytes and overwrite intent."""

    server: str
    run_id: str
    archive_sha256: str
    report_sha256: str
    overwrite: bool


class BenchmarkSettings(WireModel):
    """Project the portable harness settings included in publications."""

    runtime: str | None
    timeout_seconds: int | float | None
    setup_timeout_seconds: int | float | None
    retries: IdentityRetries | None
    diff_excludes: list[str] | None


class PublishMetadata(WireModel):
    """Retain publication display metadata and native experiment facts."""

    display_name: str
    tags: list[str]
    basket_name: str
    inference: ReportInference
    agent_input: ReportAgentInput
    benchmark: BenchmarkSettings
    task_ids: list[str]


class PublishPayload(WireModel):
    """Read the payload path even when its deliberately omitted receipt is partial."""

    path: str
    size_bytes: int | float | bool | None = None
    sha256: str | None = None


class PublishRequest(WireModel):
    """Describe the exact publication payload, including deliberately omitted bytes."""

    schema_family: Literal['pr_similarity_os.publish_request']
    schema_version: int | float | bool
    run_id: str
    basket_sha256: str
    config_identity_sha256: str
    id: str
    payload: PublishPayload


class ServerReceipt(WireModel):
    """Retain the server acknowledgement for an uploaded archive."""

    run_id: str
    archive_sha256: str
    report_sha256: str
    idempotency_key: str
    author_login: str


class PublicMetadata(WireModel):
    """Retain server display labels after publication."""

    display_name: str
    tags: list[str]


class PublishReceipt(WireModel):
    """Close a publication operation against its immutable request."""

    schema_family: Literal['pr_similarity_os.publish_receipt']
    schema_version: int | float | bool
    id: str
    run_id: str
    request: ArtifactReceipt


def parse_evidence[T: WireModel](
    model: type[T],
    value: object,
    code: str = 'invalid_state',
    *,
    message: str = 'Uploaded documents or artifacts are inconsistent.',
) -> T:
    """Translate typed parsing failures into the existing safe archive error."""
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise ViewerError(code, message) from error


class Receipt(Protocol):
    """Read a receipt from any schema-owned artifact reference."""

    @property
    def path(self) -> str:
        """Return the archive-relative member path."""
        ...

    @property
    def size_bytes(self) -> int | float:
        """Return the original numeric byte count."""
        ...

    @property
    def sha256(self) -> str:
        """Return the digest of the immutable member bytes."""
        ...

    def to_wire(self) -> JsonObject:
        """Serialize the original receipt or selection without adding fields."""
        ...


class OperationSelection(Protocol):
    """Read a host-operation selection from current or historical state."""

    @property
    def id(self) -> str:
        """Return the immutable operation identifier."""
        ...

    @property
    def kind(self) -> str:
        """Return the selected host operation kind."""
        ...

    @property
    def accepted_revision(self) -> int | float:
        """Return the state revision that accepted this selection."""
        ...

    @property
    def result(self) -> Receipt:
        """Return the receipt for the selected operation result."""
        ...

    @property
    def receipt(self) -> Receipt:
        """Return the receipt for the completed operation."""
        ...

    def to_wire(self) -> JsonObject:
        """Serialize the original receipt or selection without adding fields."""
        ...


class OperationInputAttempt(WireModel):
    """Read only the attempt identity consulted in a frozen operation input."""

    id: str


class OperationInputHistory(WireModel):
    """Read an operation input's nullable attempt selection."""

    attempt: OperationInputAttempt | None


class OperationInputTask(OperationInputHistory):
    """Read task ownership without imposing the complete live-state schema."""

    id: str
    history: list[OperationInputHistory] = Field(default_factory=list)


class OperationInputState(WireModel):
    """Represent exactly the frozen input fields validated by host operations."""

    schema_family: Literal['pr_similarity_os.state']
    schema_version: int
    run_id: str
    registry_sha256: str
    revision: int
    tasks: list[OperationInputTask]


class InferenceAttempt(WireModel):
    """Read the native identity needed for effective configuration provenance."""

    id: str
    sequence: int | float | None = None


class InferenceHistory(WireModel):
    """Read the native start and seal selected for one inference claim."""

    attempt: InferenceAttempt | None = None
    start: ArtifactReceipt | None = None
    seal: ArtifactReceipt | None = None


class InferenceTask(InferenceHistory):
    """Read inference ownership independently of full registry-state validation."""

    id: str
    history: list[InferenceHistory]


class InferenceState(WireModel):
    """Read task evidence for independently callable inference validation."""

    tasks: list[InferenceTask]


class InferenceStart(NativeOwner):
    """Read native start ownership without validating ignored attempt metadata."""

    schema_family: Literal['pr_similarity_os.start']
    task_id: str
    attempt: InferenceAttempt


class InferenceEffectiveConfig(WireModel):
    """Read capture status and path; display messages are unrelated evidence."""

    path: str | None = None
    status: str | None = None


class InferenceArtifacts(WireModel):
    """Read only the native effective configuration reference."""

    effective_config: InferenceEffectiveConfig


class InferenceCaptureTask(WireModel):
    """Determine whether a registered seal declares captured effective input."""

    artifacts: InferenceArtifacts


class InferenceCaptureSeal(WireModel):
    """Read capture availability before selecting detailed native ownership."""

    task: InferenceCaptureTask


class InferenceTaskFacts(InferenceCaptureTask):
    """Read exactly the source identity fields bound by effective configuration."""

    id: str
    final_attempt: InferenceAttempt
    repository: str
    number: int | float | bool
    kind: str
    source: ReportSourceDescriptor
    diff_policy: str
    before: str
    after: str
    before_tree: str
    after_tree: str
    workspace: str
    agent_task: str


class InferenceReceipt(ArtifactReceipt):
    """Preserve the native validator's exact-key and unsafe-path error order."""

    @model_validator(mode='before')
    @classmethod
    def check_keys(cls, value: object) -> object:
        """Require an exact receipt object before inspecting its path."""
        if not isinstance(value, dict) or set(value) != {
            'path',
            'size_bytes',
            'sha256',
        }:
            raise ViewerError('invalid_inference', 'Uploaded documents or artifacts are inconsistent.')
        return value

    @field_validator('path', mode='before')
    @classmethod
    def check_path(cls, value: object) -> str:
        """Retain the legacy unsafe_path category for malformed receipt paths."""
        if not isinstance(value, str):
            raise ViewerError('unsafe_path', 'Unsafe artifact path.')
        return safe_path(value)


class InferenceSeal(NativeOwner):
    """Bind an effective config to native ownership and its source task identity."""

    schema_family: Literal['pr_similarity_os.seal']
    python_runtime: ArtifactReceipt | None = None
    task: InferenceTaskFacts
    artifact_receipts: list[InferenceReceipt]

    def matches_agent_input(self, expected: WireModel) -> bool:
        """Compare frozen input only after source ownership checks have passed."""
        return self.to_wire().get('agent_input') == expected.to_wire()
