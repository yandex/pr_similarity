"""Typed basket-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import ValidatedJsonValue, WireModel


class InputReceipt(WireModel):
    """Represent the InputReceipt object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/InputReceipt'
    stage: Literal['collect', 'filter', 'generate', 'export']
    sha256: str


class Producer(WireModel):
    """Represent the Producer object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Producer'
    version: str
    implementation_sha256: str
    dependencies_sha256: str
    python_version: str
    git_version: str


class LineageReceipt(WireModel):
    """Represent the LineageReceipt object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/LineageReceipt'
    stage: Literal['collect', 'filter', 'generate', 'export']
    sha256: str
    settings_hash: str
    settings: dict[str, ValidatedJsonValue]
    producer: Producer


class Repository(WireModel):
    """Represent the Repository object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Repository'
    provider: Literal['github', 'gitlab']
    server_url: str
    project_id: str
    path: str
    web_url: str
    default_branch: str


class Counts(WireModel):
    """Represent the Counts object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Counts'
    input: int
    succeeded: int
    rejected: int
    failed: int
    pending: int


class StageError(WireModel):
    """Represent the StageError object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/StageError'
    code: str
    message: str
    retryable: bool


class Omission(WireModel):
    """Represent the Omission object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Omission'
    id: str
    stage: Literal['collect', 'filter', 'generate']
    kind: Literal['failed', 'rejected']
    code: str
    result_sha256: str


class Discovery(WireModel):
    """Represent the Discovery object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Discovery'
    matched: int
    selected: int
    capped: bool


class Coverage(WireModel):
    """Represent the Coverage object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Coverage'
    discovery: Discovery
    collect: Counts
    filter: Counts
    selected: int
    generate: Counts


class SelectionReceipt(WireModel):
    """Represent the SelectionReceipt object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/SelectionReceipt'
    schema_version: int
    sha256: str
    input_sha256: str
    fingerprint: str
    selected_ids: list[str]
    generation_keys: dict[str, str]


class BasketMeta(WireModel):
    """Represent the BasketMeta object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketMeta'
    schema_family: Literal['pr_similarity_os.basket'] | None = None
    schema_version: int
    stage: Literal['export'] | None = None
    status: Literal['running', 'interrupted', 'failed', 'completed']
    input: InputReceipt | None
    settings: dict[str, ValidatedJsonValue]
    settings_hash: str
    producer: Producer
    lineage: list[LineageReceipt]
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


class BasketRevisions(WireModel):
    """Represent the BasketRevisions object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketRevisions'
    policy: Literal['change_merge_base_v1']
    before: str
    after: str
    before_tree: str
    after_tree: str


class ChangedFile(WireModel):
    """Represent the ChangedFile object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/ChangedFile'
    path: str
    status: Literal['added', 'modified', 'deleted']
    additions: int
    deletions: int
    before_blob: str | None
    after_blob: str | None
    before_mode: Literal['100644', '100755'] | None
    after_mode: Literal['100644', '100755'] | None


class ChangeStats(WireModel):
    """Represent the ChangeStats object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/ChangeStats'
    files_count: int
    additions: int
    deletions: int
    changed_lines: int
    patch_chars: int


class BasketChanges(WireModel):
    """Represent the BasketChanges object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketChanges'
    files: list[ChangedFile]
    stats: ChangeStats


class TaskContent(WireModel):
    """Represent the TaskContent object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/TaskContent'
    task: str
    subtasks: list[str]


class IssueRef(WireModel):
    """Represent the IssueRef object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/IssueRef'
    id: str
    url: str


class Usage(WireModel):
    """Represent the Usage object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Usage'
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class Generation(WireModel):
    """Represent the Generation object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Generation'
    issue_ids: list[str]
    attempts: int
    response_id: str | None
    response_model: str | None
    usage: Usage | None
    request_sha256: str | None
    input_sha256: str
    reused_from: str | None = None


class RetrievalReceipt(WireModel):
    """Represent the RetrievalReceipt object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/RetrievalReceipt'
    checked_at: str
    source_sha256: str
    before: str
    after: str
    before_tree: str
    after_tree: str


class SourceRevisions(WireModel):
    """Represent the SourceRevisions object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/SourceRevisions'
    base_sha: str
    head_sha: str
    merge_commit_sha: str | None
    head_repository: str | None
    expected_base_sha: str | None
    squash_commit_sha: str | None
    commits_count: int
    target_branch: str
    diff_version_id: str | None


class Provenance(WireModel):
    """Represent the Provenance object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/Provenance'
    issue_refs: list[IssueRef]
    generation: Generation
    retrieval: RetrievalReceipt
    source_revisions: SourceRevisions


class BasketChangeRequest(WireModel):
    """Represent the BasketChangeRequest object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketChangeRequest'
    repository: str
    number: int
    url: str
    title: str
    author: str | None
    merged_at: str
    kind: Literal['pull_request', 'merge_request']


class SourceDescriptor(WireModel):
    """Represent the SourceDescriptor object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/SourceDescriptor'
    vcs: Literal['git']
    fetch_url: str
    fetch_refs: list[str]


class BasketRecord(WireModel):
    """Represent the BasketRecord object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/$defs/BasketRecord'
    id: str
    revisions: BasketRevisions
    changes: BasketChanges
    task: TaskContent
    agent_task: str
    workspace: str
    golden_diff: str | None = None
    provenance: Provenance
    change_request: BasketChangeRequest
    source: SourceDescriptor
    diff_policy: Literal['git_source_changes_v1']


class BasketV2(WireModel):
    """Represent the BasketV2 object in the basket-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    meta: BasketMeta
    tasks: list[BasketRecord]


BasketV2.model_rebuild()
