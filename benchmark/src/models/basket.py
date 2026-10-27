"""Typed frozen basket variants after authoritative version-specific schema validation."""

from typing import ClassVar, Literal

from .base import ValidatedJsonValue, WireModel


class Counts(WireModel):
    """Represent the Counts object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Counts'
    failed: int
    input: int
    pending: int
    rejected: int
    succeeded: int


class Discovery(WireModel):
    """Represent the Discovery object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Discovery'
    capped: bool
    matched: int
    selected: int


class Coverage(WireModel):
    """Represent the Coverage object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Coverage'
    collect: Counts
    discovery: Discovery
    filter: Counts
    generate: Counts
    selected: int


class StageError(WireModel):
    """Represent the StageError object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/StageError'
    code: str
    message: str
    retryable: bool


class InputReceipt(WireModel):
    """Represent the InputReceipt object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/InputReceipt'
    sha256: str
    stage: Literal['collect', 'filter', 'generate', 'export']


class Producer(WireModel):
    """Represent the Producer object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Producer'
    git_version: str
    python_version: str
    version: str


class LineageReceipt(WireModel):
    """Represent the LineageReceipt object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/LineageReceipt'
    producer: Producer
    settings: dict[str, ValidatedJsonValue]
    settings_hash: str
    sha256: str
    stage: Literal['collect', 'filter', 'generate', 'export']


class Omission(WireModel):
    """Represent the Omission object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Omission'
    code: str
    id: str
    kind: Literal['failed', 'rejected']
    result_sha256: str
    stage: Literal['collect', 'filter', 'generate']


class Repository(WireModel):
    """Represent the Repository object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Repository'
    default_branch: str
    path: str
    project_id: str
    provider: Literal['github', 'gitlab']
    server_url: str
    web_url: str


class SelectionReceipt(WireModel):
    """Represent the SelectionReceipt object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/SelectionReceipt'
    fingerprint: str
    generation_keys: dict[str, str]
    input_sha256: str
    schema_version: int
    selected_ids: list[str]
    sha256: str


class BasketMeta(WireModel):
    """Represent the BasketMeta object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketMeta'
    completed_at: str | None
    counts: Counts
    coverage: Coverage
    description: str
    error: StageError | None
    input: InputReceipt | None
    lineage: list[LineageReceipt]
    maintainers: list[str]
    name: str
    omissions: list[Omission]
    partial: bool
    producer: Producer
    repository: Repository
    schema_family: Literal['pr_similarity_os.basket'] | None = None
    schema_version: int
    selection: SelectionReceipt
    settings: dict[str, ValidatedJsonValue]
    settings_hash: str
    stage: Literal['export'] | None = None
    started_at: str
    status: Literal['running', 'interrupted', 'failed', 'completed']
    updated_at: str


class LegacyProducer(WireModel):
    """Retain historical producer fingerprints without binding them to installed code."""

    schema_pointer: ClassVar[str] = '#/$defs/Producer'
    version: str
    implementation_sha256: str
    dependencies_sha256: str
    python_version: str
    git_version: str


class LegacyLineageReceipt(WireModel):
    """Preserve the exact provenance carried by a published v1 or v2 basket."""

    schema_pointer: ClassVar[str] = '#/$defs/LineageReceipt'
    stage: Literal['collect', 'filter', 'generate', 'export']
    sha256: str
    settings_hash: str
    settings: dict[str, ValidatedJsonValue]
    producer: LegacyProducer


class BasketMetaV1(WireModel):
    """Describe a frozen v1 population without inventing coverage or selection records."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketMeta'
    schema_family: Literal['pr_similarity_os.basket'] | None = None
    schema_version: int | None = None
    stage: Literal['export'] | None = None
    status: Literal['running', 'interrupted', 'failed', 'completed']
    input: InputReceipt | None
    settings: dict[str, ValidatedJsonValue]
    settings_hash: str
    producer: LegacyProducer
    lineage: list[LegacyLineageReceipt]
    repository: Repository
    started_at: str
    updated_at: str
    completed_at: str | None
    counts: Counts
    error: StageError | None
    name: str
    description: str
    maintainers: list[str]
    partial: bool
    omitted_failed_ids: list[str]


class BasketMetaV2(WireModel):
    """Describe frozen v2 coverage with its original producer provenance."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketMeta'
    schema_family: Literal['pr_similarity_os.basket'] | None = None
    schema_version: int
    stage: Literal['export'] | None = None
    status: Literal['running', 'interrupted', 'failed', 'completed']
    input: InputReceipt | None
    settings: dict[str, ValidatedJsonValue]
    settings_hash: str
    producer: LegacyProducer
    lineage: list[LegacyLineageReceipt]
    repository: Repository
    started_at: str
    updated_at: str
    completed_at: str | None
    counts: Counts
    error: StageError | None
    name: str
    description: str
    maintainers: list[str]
    partial: bool
    omissions: list[Omission]
    coverage: Coverage
    selection: SelectionReceipt


class BasketChangeRequest(WireModel):
    """Represent the BasketChangeRequest object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketChangeRequest'
    author: str | None
    kind: Literal['pull_request', 'merge_request']
    merged_at: str
    number: int
    repository: str
    title: str
    url: str


class ChangedFile(WireModel):
    """Represent the ChangedFile object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/ChangedFile'
    additions: int
    after_blob: str | None
    after_mode: Literal['100644', '100755'] | None
    before_blob: str | None
    before_mode: Literal['100644', '100755'] | None
    deletions: int
    path: str
    status: Literal['added', 'modified', 'deleted']


class ChangeStats(WireModel):
    """Represent the ChangeStats object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/ChangeStats'
    additions: int
    changed_lines: int
    deletions: int
    files_count: int
    patch_chars: int


class BasketChanges(WireModel):
    """Represent the BasketChanges object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketChanges'
    files: list[ChangedFile]
    stats: ChangeStats


class Usage(WireModel):
    """Represent the Usage object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Usage'
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class GenerationV1(WireModel):
    """Retain v1 generation provenance without adding a later selection identity."""

    schema_pointer: ClassVar[str] = '#/$defs/Generation'
    issue_ids: list[str]
    attempts: int
    response_id: str | None
    response_model: str | None
    usage: Usage | None
    request_sha256: str | None


class Generation(WireModel):
    """Represent the Generation object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Generation'
    attempts: int
    input_sha256: str
    issue_ids: list[str]
    request_sha256: str | None
    response_id: str | None
    response_model: str | None
    reused_from: str | None = None
    usage: Usage | None


class IssueRef(WireModel):
    """Represent the IssueRef object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/IssueRef'
    id: str
    url: str


class RetrievalReceipt(WireModel):
    """Represent the RetrievalReceipt object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/RetrievalReceipt'
    after: str
    after_tree: str
    before: str
    before_tree: str
    checked_at: str
    source_sha256: str


class SourceRevisions(WireModel):
    """Represent the SourceRevisions object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/SourceRevisions'
    base_sha: str
    commits_count: int
    diff_version_id: str | None
    expected_base_sha: str | None
    head_repository: str | None
    head_sha: str
    merge_commit_sha: str | None
    squash_commit_sha: str | None
    target_branch: str


class Provenance(WireModel):
    """Represent the Provenance object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Provenance'
    generation: Generation | GenerationV1
    issue_refs: list[IssueRef]
    retrieval: RetrievalReceipt
    source_revisions: SourceRevisions


class BasketRevisions(WireModel):
    """Represent the BasketRevisions object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketRevisions'
    after: str
    after_tree: str
    before: str
    before_tree: str
    policy: Literal['change_merge_base_v1']


class SourceDescriptor(WireModel):
    """Represent the SourceDescriptor object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/SourceDescriptor'
    fetch_refs: list[str]
    fetch_url: str
    vcs: Literal['git']


class TaskContent(WireModel):
    """Represent the TaskContent object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/TaskContent'
    subtasks: list[str]
    task: str


class BasketRecord(WireModel):
    """Represent the BasketRecord object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketRecord'
    agent_task: str
    change_request: BasketChangeRequest
    changes: BasketChanges
    diff_policy: Literal['git_source_changes_v1']
    golden_diff: str | None = None
    id: str
    provenance: Provenance
    revisions: BasketRevisions
    source: SourceDescriptor
    task: TaskContent
    workspace: str


class BasketV3(WireModel):
    """Represent the BasketV3 object in the basket-v3 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    meta: BasketMeta
    tasks: list[BasketRecord]


class BasketV1(WireModel):
    """Read a published v1 basket without rewriting its frozen representation."""

    schema_pointer: ClassVar[str] = '#'
    meta: BasketMetaV1
    tasks: list[BasketRecord]


class BasketV2(WireModel):
    """Read a published v2 basket with its original selection and provenance."""

    schema_pointer: ClassVar[str] = '#'
    meta: BasketMetaV2
    tasks: list[BasketRecord]


BasketV1.model_rebuild()
BasketV2.model_rebuild()
BasketV3.model_rebuild()


type BasketDocument = BasketV1 | BasketV2 | BasketV3
