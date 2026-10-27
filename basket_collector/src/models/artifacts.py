"""The sole persisted JSON and generated-task contracts for basket collector v3."""

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

from basket_collector.models.sources import (
    Repository,
    RetrievalReceipt,
    SourceDescriptor,
    change_id,
    source_hash,
)

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
StageStatus: TypeAlias = Literal['running', 'interrupted', 'failed', 'completed']
StageName: TypeAlias = Literal['collect', 'filter', 'generate', 'export']
Sha256 = StrictStr
GitSha = StrictStr


class StrictModel(BaseModel):
    """Base model that rejects extra fields and implicit type coercion."""

    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonnegativeInt = Annotated[StrictInt, Field(ge=0)]


class RecordCheckpoint(StrictModel):
    """Persist record attempt histories independently from semantic stage content."""

    attempts: dict[StrictStr, dict[str, JsonValue]] = Field(default_factory=dict)
    retry_ids: list[StrictStr] = Field(default_factory=list)


class CollectCheckpoint(RecordCheckpoint):
    """Preserve discovery cursor and the complete frozen collection queue."""

    phase: Literal['repository', 'discovery', 'records', 'ready']
    next_page: PositiveInt
    matches: list[list[StrictStr | PositiveInt]]
    numbers: list[PositiveInt]

    @field_validator('numbers')
    @classmethod
    def check_numbers(cls, value: list[int]) -> list[int]:
        """Require unique positive PR numbers without implicit coercion."""
        if len(value) != len(set(value)):
            raise ValueError('checkpoint queue contains duplicate PR numbers')
        return value

    @field_validator('matches')
    @classmethod
    def check_matches(cls, value: list[list[str | int]]) -> list[list[str | int]]:
        """Require ordered unique timestamp and PR-number discovery pairs."""
        pairs: list[tuple[str, int]] = []
        for item in value:
            if len(item) != 2:
                raise ValueError('checkpoint matches require timestamp and positive PR number')
            stamp, number = item
            if not isinstance(stamp, str) or not isinstance(number, int):
                raise ValueError('checkpoint matches require timestamp and positive PR number')
            if not stamp.endswith('Z'):
                raise ValueError('checkpoint match timestamp must be UTC')
            datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            pairs.append((stamp, number))
        if pairs != sorted(pairs) or len({number for _, number in pairs}) != len(pairs):
            raise ValueError('checkpoint matches must be sorted and unique')
        return value


class FilterCheckpoint(RecordCheckpoint):
    """Distinguish per-PR filtering, population selection and publication."""

    phase: Literal['local', 'selection', 'ready']


class GenerateCheckpoint(RecordCheckpoint):
    """Retain previous per-PR attempts while processing or publishing tasks."""

    phase: Literal['records', 'ready'] = 'records'


class ExportCheckpoint(StrictModel):
    """Represent a fully projected basket awaiting publication."""

    phase: Literal['ready']


def _sha(value: str, length: int) -> str:
    """Require a lowercase hexadecimal SHA of the specified length."""
    if re.fullmatch(rf'[0-9a-f]{{{length}}}', value) is None:
        raise ValueError(f'expected {length} lowercase hexadecimal characters')
    return value


def _utc(value: str) -> str:
    """Require a valid UTC RFC 3339 timestamp with a Z suffix."""
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', value) is None:
        raise ValueError('expected UTC RFC 3339 timestamp ending in Z')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('invalid UTC RFC 3339 timestamp') from exc
    return value


def settings_sha256(settings: dict[str, JsonValue]) -> str:
    """Hash semantic settings with the collector's canonical JSON encoding."""
    encoded = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _path(value: str) -> str:
    """Require a normalized repository-relative POSIX file path."""
    if (
        not value
        or value.startswith('/')
        or '\x00' in value
        or '\\' in value
        or any(part in ('', '.', '..') for part in value.split('/'))
    ):
        raise ValueError('expected normalized repository-relative POSIX path')
    return value


def _https_url(value: str) -> str:
    """Require an HTTPS URL without credentials or a fragment."""
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('expected HTTPS URL without credentials or fragment')
    return value


class StageError(StrictModel):
    """Persist a stage error and whether the operation can be retried."""

    code: StrictStr
    message: StrictStr
    retryable: StrictBool


class Counts(StrictModel):
    """Track the mutually exclusive outcomes of stage input records."""

    input: StrictInt = Field(ge=0)
    succeeded: StrictInt = Field(ge=0)
    rejected: StrictInt = Field(ge=0)
    failed: StrictInt = Field(ge=0)
    pending: StrictInt = Field(ge=0)

    @model_validator(mode='after')
    def check_total(self) -> 'Counts':
        """Require the outcome counts to sum to the input count."""
        if self.input != self.succeeded + self.rejected + self.failed + self.pending:
            raise ValueError('counts do not add up to input')
        return self


class Producer(StrictModel):
    """Identify the implementation and environment that wrote a stage."""

    version: StrictStr
    python_version: StrictStr
    git_version: StrictStr


class InputReceipt(StrictModel):
    """Identify the immediate input stage and its content digest."""

    stage: StageName
    sha256: Sha256

    @field_validator('sha256')
    @classmethod
    def validate_hash(cls, value: str) -> str:
        """Require a canonical SHA-256 digest for an input receipt."""
        return _sha(value, 64)


class LineageReceipt(InputReceipt):
    """Record an ancestor stage and its exact settings and producer."""

    settings_hash: Sha256
    settings: dict[str, JsonValue]
    producer: Producer

    @field_validator('settings_hash')
    @classmethod
    def validate_settings_hash(cls, value: str) -> str:
        """Require a canonical SHA-256 settings digest."""
        return _sha(value, 64)

    @model_validator(mode='after')
    def check_settings_hash(self) -> 'LineageReceipt':
        """Verify that the digest identifies the recorded settings."""
        if self.settings_hash != settings_sha256(self.settings):
            raise ValueError('lineage settings_hash does not match settings')
        return self


class Meta(StrictModel):
    """Describe the stage state, counts, settings, and complete lineage."""

    schema_family: Literal['pr_similarity_os.collector'] = 'pr_similarity_os.collector'
    schema_version: Literal[3]
    stage: Literal['collect', 'filter', 'generate']
    status: StageStatus
    input: InputReceipt | None
    settings: dict[str, JsonValue]
    settings_hash: Sha256
    producer: Producer
    lineage: list[LineageReceipt]
    repository: Repository
    started_at: StrictStr
    updated_at: StrictStr
    completed_at: StrictStr | None
    counts: Counts
    error: StageError | None

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Require explicit integer version three without legacy coercions."""
        if type(value) is not int or value != 3:
            raise ValueError('schema_version must be the integer 3')
        return value

    @field_validator('settings_hash')
    @classmethod
    def validate_hash(cls, value: str) -> str:
        """Require a canonical SHA-256 settings digest."""
        return _sha(value, 64)

    @field_validator('started_at', 'updated_at', 'completed_at')
    @classmethod
    def validate_time(cls, value: str | None) -> str | None:
        """Require UTC timestamps for each present lifecycle time."""
        return _utc(value) if value is not None else None

    @model_validator(mode='after')
    def check_stage(self) -> 'Meta':
        """Check stage lineage, input receipt, and lifecycle consistency.

        A completed stage needs a resolved default branch, completion time,
        zero pending records, and no error. An unfinished stage cannot have
        a completion time.
        """
        if self.settings_hash != settings_sha256(self.settings):
            raise ValueError('meta settings_hash does not match settings')
        predecessors: dict[str, list[str]] = {
            'collect': [],
            'filter': ['collect'],
            'generate': ['collect', 'filter'],
            'export': ['collect', 'filter', 'generate'],
        }
        expected = predecessors[self.stage]
        if [item.stage for item in self.lineage] != expected:
            raise ValueError('lineage must contain each preceding stage in order')
        if self.input is None:
            if self.stage != 'collect':
                raise ValueError('only collect may omit input')
        elif self.stage == 'collect':
            raise ValueError('collect must omit input')
        elif self.input.stage != expected[-1] or self.input.sha256 != self.lineage[-1].sha256:
            raise ValueError('input must match the last lineage receipt')
        if self.status == 'completed' and (self.counts.pending or self.completed_at is None or self.error is not None):
            raise ValueError('completed stage needs zero pending, completion time and no error')
        if self.status == 'completed' and (
            self.repository.default_branch is None or self.repository.project_id == 'unresolved'
        ):
            raise ValueError('completed stage needs resolved repository default branch')
        if self.status != 'completed' and self.completed_at is not None:
            raise ValueError('unfinished stage cannot have completion time')
        return self


class Discovery(StrictModel):
    """Summarize the matched and selected PRs in collection."""

    matched: StrictInt = Field(ge=0)
    selected: StrictInt = Field(ge=0)
    capped: StrictBool

    @model_validator(mode='after')
    def check_counts(self) -> 'Discovery':
        """Ensure selected PRs do not exceed matches and capped is accurate."""
        if self.selected > self.matched or self.capped != (self.selected < self.matched):
            raise ValueError('invalid discovery counts')
        return self


class CollectMeta(Meta):
    """Add PR discovery counts to collection stage metadata."""

    stage: Literal['collect'] = 'collect'
    discovery: Discovery


class FilterMeta(Meta):
    """Fix metadata to the filter stage."""

    stage: Literal['filter'] = 'filter'


class GenerateMeta(Meta):
    """Fix metadata to the generation stage."""

    stage: Literal['generate'] = 'generate'


class Omission(StrictModel):
    """Identify a known unprocessed candidate or an unsuccessful selected task."""

    id: StrictStr
    stage: Literal['collect', 'filter', 'generate']
    kind: Literal['failed', 'rejected']
    code: StrictStr
    result_sha256: Sha256

    @model_validator(mode='after')
    def check_omission(self) -> 'Omission':
        """Keep eligibility rejections out of incomplete-population coverage."""
        if re.fullmatch(r'change:[0-9a-f]{64}', self.id) is None:
            raise ValueError('invalid omitted change identity')
        _sha(self.result_sha256, 64)
        if self.kind == 'rejected' and self.stage != 'generate':
            raise ValueError('only selected generation rejections are omissions')
        return self


class SelectionReceipt(StrictModel):
    """Bind generation to an ordered selection and its complete per-task inputs."""

    schema_version: Literal[3]
    sha256: Sha256
    input_sha256: Sha256
    fingerprint: Sha256
    selected_ids: list[StrictStr]
    generation_keys: dict[StrictStr, Sha256]

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Require an explicit integer v3 on independently stored selection receipts."""
        if type(value) is not int or value != 3:
            raise ValueError('selection schema_version must be the integer 3')
        return value

    @model_validator(mode='after')
    def check_selection(self) -> 'SelectionReceipt':
        """Require exact keys and a stable ordered generation-input fingerprint."""
        for digest in (self.sha256, self.input_sha256, self.fingerprint, *self.generation_keys.values()):
            _sha(digest, 64)
        _check_ids(self.selected_ids)
        if set(self.selected_ids) != set(self.generation_keys):
            raise ValueError('selection generation keys differ from selected IDs')
        expected = settings_sha256(
            {'tasks': [[identity, self.generation_keys[identity]] for identity in self.selected_ids]}
        )
        if self.fingerprint != expected:
            raise ValueError('selection fingerprint differs from ordered inputs')
        return self


class Coverage(StrictModel):
    """Preserve the complete discovered and processed population behind an export."""

    discovery: Discovery
    collect: Counts
    filter: Counts
    selected: StrictInt = Field(ge=0)
    generate: Counts

    @model_validator(mode='after')
    def check_coverage(self) -> 'Coverage':
        """Ensure downstream queues contain only successful predecessor records."""
        if self.collect.input != self.discovery.selected or self.filter.input != self.collect.succeeded:
            raise ValueError('coverage predecessor populations disagree')
        if self.selected > self.filter.succeeded or self.generate.input != self.selected:
            raise ValueError('coverage selected population disagrees')
        if self.collect.pending or self.filter.pending or self.generate.pending:
            raise ValueError('a frozen export cannot omit pending records')
        return self


class BasketMeta(Meta):
    """Describe the v3 export, selected population and explicit omissions."""

    schema_family: Literal['pr_similarity_os.basket'] = 'pr_similarity_os.basket'
    stage: Literal['export'] = 'export'
    name: StrictStr
    description: StrictStr
    maintainers: list[StrictStr]
    partial: StrictBool
    omissions: list[Omission]
    coverage: Coverage
    selection: SelectionReceipt

    @model_validator(mode='after')
    def check_export(self) -> 'BasketMeta':
        """Verify coverage and omissions rather than inferring completeness from task count."""
        if self.partial != bool(self.omissions):
            raise ValueError('partial must agree with omissions')
        _check_ids([item.id for item in self.omissions])
        if self.coverage.selected != len(self.selection.selected_ids):
            raise ValueError('selection count differs from coverage')
        for stage in ('collect', 'filter', 'generate'):
            counts = getattr(self.coverage, stage)
            if sum(item.stage == stage and item.kind == 'failed' for item in self.omissions) != counts.failed:
                raise ValueError('failed omissions differ from coverage')
        if sum(item.kind == 'rejected' for item in self.omissions) != self.coverage.generate.rejected:
            raise ValueError('selected rejections differ from coverage')
        if self.coverage.generate.succeeded != self.counts.succeeded:
            raise ValueError('exported count differs from generation coverage')
        if (
            self.counts.rejected
            or self.counts.failed
            or self.counts.pending
            or self.counts.input != self.counts.succeeded
        ):
            raise ValueError('invalid export counts')
        return self


class SourceRevisions(StrictModel):
    """Preserve normalized hosting revision evidence before local Git resolution."""

    base_sha: GitSha
    head_sha: GitSha
    merge_commit_sha: GitSha | None
    head_repository: StrictStr | None
    expected_base_sha: GitSha | None
    squash_commit_sha: GitSha | None
    commits_count: StrictInt = Field(gt=0)
    target_branch: StrictStr = Field(min_length=1)
    diff_version_id: StrictStr | None = Field(pattern=r'^[1-9][0-9]*$')

    @field_validator('base_sha', 'head_sha', 'merge_commit_sha', 'expected_base_sha', 'squash_commit_sha')
    @classmethod
    def validate_sha(cls, value: str | None) -> str | None:
        """Require a Git commit SHA for each present source revision."""
        return _sha(value, 40) if value is not None else None


class ResolvedRevisions(StrictModel):
    """Record the verified merge-base policy and PR commit order."""

    policy: Literal['change_merge_base_v1']
    before: GitSha
    after: GitSha
    before_tree: GitSha
    after_tree: GitSha
    commits: list[GitSha] = Field(min_length=1)

    @field_validator('before', 'after', 'before_tree', 'after_tree')
    @classmethod
    def validate_sha(cls, value: str) -> str:
        """Require a Git commit SHA for each resolved endpoint."""
        return _sha(value, 40)

    @field_validator('commits')
    @classmethod
    def validate_commits(cls, value: list[str]) -> list[str]:
        """Require distinct Git commit SHAs in the saved sequence."""
        if len(set(value)) != len(value):
            raise ValueError('duplicate commit SHA')
        return [_sha(item, 40) for item in value]

    @model_validator(mode='after')
    def check_head(self) -> 'ResolvedRevisions':
        """Require the final listed commit to equal the after revision."""
        if self.commits[-1] != self.after:
            raise ValueError('last commit must be after revision')
        return self


class Revisions(StrictModel):
    """Wrap the source revisions saved during collection."""

    source: SourceRevisions


class FilterRevisions(Revisions):
    """Add locally verified endpoints and commits to source revisions."""

    resolved: ResolvedRevisions


class ChangeRequest(StrictModel):
    """Store GitHub PR identity and human-readable metadata."""

    kind: Literal['pull_request', 'merge_request']
    repository: StrictStr
    number: StrictInt = Field(gt=0)
    url: StrictStr
    title: StrictStr
    body: StrictStr
    author: StrictStr | None
    merged_at: StrictStr

    @field_validator('url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Require an HTTPS pull request URL."""
        return _https_url(value)

    @field_validator('merged_at')
    @classmethod
    def validate_time(cls, value: str) -> str:
        """Require a UTC merge timestamp."""
        return _utc(value)


class CollectRecord(StrictModel):
    """Store a normalized change and its frozen Git retrieval evidence."""

    id: StrictStr
    change_request: ChangeRequest
    revisions: Revisions
    source: SourceDescriptor

    @model_validator(mode='after')
    def check_native_version(self) -> 'CollectRecord':
        """Require the native GitLab diff version captured by authenticated collection."""
        if self.change_request.kind == 'merge_request' and self.revisions.source.diff_version_id is None:
            raise ValueError('GitLab source requires a native diff version ID')
        return self

    @field_validator('id')
    @classmethod
    def check_identity(cls, value: str) -> str:
        """Reject legacy IDs before document-level owner verification."""
        if re.fullmatch(r'change:[0-9a-f]{64}', value) is None:
            raise ValueError('invalid opaque change identity')
        return value


class ChangedFile(StrictModel):
    """Describe one changed regular file and its Git blob sides."""

    path: StrictStr
    status: Literal['added', 'modified', 'deleted']
    additions: StrictInt = Field(ge=0)
    deletions: StrictInt = Field(ge=0)
    before_blob: GitSha | None
    after_blob: GitSha | None
    before_mode: Literal['100644', '100755'] | None
    after_mode: Literal['100644', '100755'] | None

    @field_validator('path')
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Require a normalized repository-relative POSIX file path."""
        return _path(value)

    @field_validator('before_blob', 'after_blob')
    @classmethod
    def validate_sha(cls, value: str | None) -> str | None:
        """Require a Git blob SHA for each present file side."""
        return _sha(value, 40) if value is not None else None

    @model_validator(mode='after')
    def check_sides(self) -> 'ChangedFile':
        """Require blob and mode pairs that agree with the change status.

        Added files have only an after side, deleted files only a before
        side, and modified files both sides.
        """
        if (self.before_blob is None) != (self.before_mode is None) or (self.after_blob is None) != (
            self.after_mode is None
        ):
            raise ValueError('blob and mode presence must match')
        if self.status == 'added' and (self.before_blob is not None or self.after_blob is None):
            raise ValueError('added file needs only after side')
        if self.status == 'deleted' and (self.after_blob is not None or self.before_blob is None):
            raise ValueError('deleted file needs only before side')
        if self.status == 'modified' and (self.before_blob is None or self.after_blob is None):
            raise ValueError('modified file needs both sides')
        return self


class ChangeStats(StrictModel):
    """Summarize changed files, lines, and patch characters."""

    files_count: StrictInt = Field(ge=0)
    additions: StrictInt = Field(ge=0)
    deletions: StrictInt = Field(ge=0)
    changed_lines: StrictInt = Field(ge=0)
    patch_chars: StrictInt = Field(ge=0)

    @model_validator(mode='after')
    def check_lines(self) -> 'ChangeStats':
        """Require changed lines to equal additions plus deletions."""
        if self.changed_lines != self.additions + self.deletions:
            raise ValueError('changed_lines must equal additions plus deletions')
        return self


class CommitMessage(StrictModel):
    """Pair a verified PR commit SHA with its message."""

    sha: GitSha
    message: StrictStr

    @field_validator('sha')
    @classmethod
    def validate_sha(cls, value: str) -> str:
        """Require a Git commit SHA for the message."""
        return _sha(value, 40)


class Changes(StrictModel):
    """Store ordered files, statistics, diff, and commit messages."""

    files: list[ChangedFile]
    stats: ChangeStats
    diff: StrictStr
    commit_messages: list[CommitMessage]

    @model_validator(mode='after')
    def check_stats(self) -> 'Changes':
        """Verify file order, unique paths, and aggregate statistics.

        Patch size counts Unicode code points in the saved diff.
        """
        if [item.path for item in self.files] != sorted(set(item.path for item in self.files)):
            raise ValueError('files must be unique and sorted by path')
        if (
            self.stats.files_count != len(self.files)
            or self.stats.additions != sum(item.additions for item in self.files)
            or self.stats.deletions != sum(item.deletions for item in self.files)
        ):
            raise ValueError('file statistics do not match files')
        if self.stats.patch_chars != len(self.diff):
            raise ValueError('patch_chars must equal diff code points')
        return self


class Issue(StrictModel):
    """Store a linked GitHub issue and its task context."""

    id: StrictStr
    repository: StrictStr
    number: StrictInt = Field(gt=0)
    url: StrictStr
    title: StrictStr
    body: StrictStr

    @field_validator('url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Require an HTTPS issue URL."""
        return _https_url(value)

    @field_validator('id')
    @classmethod
    def check_identity(cls, value: str) -> str:
        """Require an opaque provider-qualified issue identity."""
        if re.fullmatch(r'issue:[0-9a-f]{64}', value) is None:
            raise ValueError('invalid opaque issue identity')
        return value


def _check_retrieval(source: SourceDescriptor, resolved: object, receipt: RetrievalReceipt) -> None:
    """Bind a cold-cache proof to the exact exported descriptor and frozen endpoints."""
    if receipt.source_sha256 != source_hash(source) or any(
        getattr(receipt, key) != getattr(resolved, key) for key in ('before', 'after', 'before_tree', 'after_tree')
    ):
        raise ValueError('retrieval receipt differs from frozen source/revisions')


class FilterRecord(CollectRecord):
    """Add verified changes and linked issues to a collected PR."""

    revisions: FilterRevisions
    changes: Changes
    issues: list[Issue]
    retrieval: RetrievalReceipt

    @model_validator(mode='after')
    def check_resolved(self) -> 'FilterRecord':
        """Keep source, resolved commits, messages, and issues consistent.

        The resolved head and commit count must match GitHub source data;
        messages follow commit order and issues are unique and sorted.
        """
        source = self.revisions.source
        resolved = self.revisions.resolved
        _check_retrieval(self.source, resolved, self.retrieval)
        if resolved.after != source.head_sha:
            raise ValueError('resolved after does not match source head SHA')
        if len(resolved.commits) != source.commits_count:
            raise ValueError('resolved commits count does not match source')
        if [item.sha for item in self.changes.commit_messages] != resolved.commits:
            raise ValueError('commit messages must match resolved commit order')
        if [item.id.casefold() for item in self.issues] != sorted(set(item.id.casefold() for item in self.issues)):
            raise ValueError('issues must be unique and sorted by identity')
        return self


class RejectedChangeRequest(StrictModel):
    """Keep identifying metadata for a PR excluded by filtering."""

    kind: Literal['pull_request', 'merge_request']
    repository: StrictStr
    number: StrictInt = Field(gt=0)
    url: StrictStr
    title: StrictStr
    author: StrictStr | None

    @field_validator('url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Require an HTTPS rejected PR URL."""
        return _https_url(value)


class RejectionReason(StrictModel):
    """Explain an exclusion with observed and expected values."""

    code: StrictStr
    message: StrictStr
    observed: JsonValue
    expected: JsonValue


class RejectedRecord(StrictModel):
    """Store a rejected PR and at least one exclusion reason."""

    id: StrictStr
    change_request: RejectedChangeRequest
    reasons: list[RejectionReason] = Field(min_length=1)

    @field_validator('id')
    @classmethod
    def check_identity(cls, value: str) -> str:
        """Retain the opaque ID of a rejected change."""
        if re.fullmatch(r'change:[0-9a-f]{64}', value) is None:
            raise ValueError('invalid opaque change identity')
        return value


class TaskContent(StrictModel):
    """Store a generated task and optional ordered subtasks."""

    task: StrictStr
    subtasks: list[StrictStr]

    @field_validator('task')
    @classmethod
    def validate_task(cls, value: str) -> str:
        """Require non-whitespace task text."""
        if not value.strip():
            raise ValueError('task must contain non-whitespace text')
        return value

    @field_validator('subtasks')
    @classmethod
    def validate_subtasks(cls, value: list[str]) -> list[str]:
        """Require non-whitespace text in every subtask."""
        if any(not item.strip() for item in value):
            raise ValueError('subtasks must contain non-whitespace text')
        return value


def format_agent_task(task: TaskContent) -> str:
    """Render the frozen benchmark instruction using task_text_v1."""
    result = f'Task: {task.task}'
    if task.subtasks:
        result += '\n\nSubtasks:\n' + '\n'.join('- ' + item.replace('\n', '\n  ') for item in task.subtasks)
    return result


class Usage(StrictModel):
    """Record optional token counts returned by generation."""

    input_tokens: StrictInt | None = Field(default=None, ge=0)
    output_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)


class Generation(StrictModel):
    """Record task provenance, attempts, and optional API details."""

    issue_ids: list[StrictStr]
    input_sha256: Sha256 | None = None
    reused_from: Sha256 | None = None
    attempts: StrictInt = Field(ge=0)
    response_id: StrictStr | None
    response_model: StrictStr | None
    usage: Usage | None
    request_sha256: Sha256 | None

    @field_validator('request_sha256', 'input_sha256', 'reused_from')
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        """Require a SHA-256 digest for a present API request."""
        return _sha(value, 64) if value is not None else None

    @model_validator(mode='after')
    def check_direct(self) -> 'Generation':
        """For direct issue tasks, forbid API response and request fields."""
        if self.attempts == 0 and any(
            item is not None for item in (self.response_id, self.response_model, self.usage, self.request_sha256)
        ):
            raise ValueError('direct issue generation has no API fields')
        return self


class TaskRecord(FilterRecord):
    """Add generated task text and provenance to a filtered PR."""

    task: TaskContent
    generation: Generation

    @model_validator(mode='after')
    def check_issue_provenance(self) -> 'TaskRecord':
        """Require generation issue IDs to match linked issues in order."""
        if self.generation.issue_ids != [issue.id for issue in self.issues]:
            raise ValueError('generation issue IDs must match linked issues in order')
        return self


class RecordIdentity(StrictModel):
    """The known identity of a discovered record before a complete API snapshot exists."""

    id: StrictStr
    kind: Literal['pull_request', 'merge_request']
    repository: StrictStr
    number: StrictInt = Field(gt=0)


class CollectFailure(StrictModel):
    """Keep an exhausted collection failure without inventing a source snapshot."""

    identity: RecordIdentity
    error: StageError
    attempts: StrictInt = Field(gt=0)


class FilterFailure(StrictModel):
    """Retain the exact collected input of an exhausted filter operation."""

    record: CollectRecord
    error: StageError
    attempts: StrictInt = Field(gt=0)


class FailedRecord(StrictModel):
    """Retain the exact selected input of an exhausted generation operation."""

    record: FilterRecord
    error: StageError
    attempts: StrictInt = Field(gt=0)


class SelectionSnapshot(SelectionReceipt):
    """Preserve population exclusions and explicit approval for upstream omissions."""

    settings: dict[str, JsonValue]
    generation_settings_sha256: Sha256
    excluded: list[RejectedRecord]
    omissions: list[Omission]

    @model_validator(mode='after')
    def check_snapshot(self) -> 'SelectionSnapshot':
        """Verify the complete immutable snapshot digest and disjoint populations."""
        _sha(self.generation_settings_sha256, 64)
        _check_ids([*self.selected_ids, *(item.id for item in self.excluded), *(item.id for item in self.omissions)])
        if any(item.stage == 'generate' for item in self.omissions):
            raise ValueError('selection cannot approve future generation omissions')
        if self.sha256 != settings_sha256(self.model_dump(mode='json', exclude={'sha256'})):
            raise ValueError('selection snapshot digest mismatch')
        return self


class CollectDocument(StrictModel):
    """Persist successful snapshots, deterministic exclusions and exhausted API failures."""

    meta: CollectMeta
    tasks: list[CollectRecord]
    rejected: list[RejectedRecord] = Field(default_factory=list)
    failed: list[CollectFailure] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_records(self) -> 'CollectDocument':
        """Validate disjoint identities without requiring data from a failed API call."""
        rows = [(item.id, item.change_request) for item in [*self.tasks, *self.rejected]]
        rows.extend((item.identity.id, item.identity) for item in self.failed)
        _check_ids([identity for identity, _ in rows])
        _check_records(self.meta.repository, rows)
        counts = self.meta.counts
        if (counts.succeeded, counts.rejected, counts.failed) != (
            len(self.tasks),
            len(self.rejected),
            len(self.failed),
        ):
            raise ValueError('collect counts do not match records')
        if counts.input != self.meta.discovery.selected:
            raise ValueError('collect input count does not match discovery selected')
        return self


class FilterDocument(StrictModel):
    """Retain all individually accepted candidates separately from population selection."""

    meta: FilterMeta
    tasks: list[FilterRecord]
    rejected: list[RejectedRecord]
    failed: list[FilterFailure] = Field(default_factory=list)
    selection: SelectionSnapshot | None = None

    @model_validator(mode='after')
    def check_records(self) -> 'FilterDocument':
        """Verify candidate outcomes and the complete selected/excluded partition."""
        rows = [(item.id, item.change_request) for item in [*self.tasks, *self.rejected]]
        rows.extend((item.record.id, item.record.change_request) for item in self.failed)
        _check_ids([identity for identity, _ in rows])
        _check_records(self.meta.repository, rows)
        counts = self.meta.counts
        if (counts.succeeded, counts.rejected, counts.failed) != (
            len(self.tasks),
            len(self.rejected),
            len(self.failed),
        ):
            raise ValueError('filter counts do not match records')
        if self.selection is not None:
            selected = set(self.selection.selected_ids)
            excluded = {item.id for item in self.selection.excluded}
            if selected | excluded != {item.id for item in self.tasks} or selected & excluded:
                raise ValueError('selection does not partition accepted filter candidates')
        return self


class TasksDocument(StrictModel):
    """Persist outcomes for exactly the accepted selection snapshot."""

    meta: GenerateMeta
    selection: SelectionReceipt
    tasks: list[TaskRecord]
    failed: list[FailedRecord]
    rejected: list[RejectedRecord] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_records(self) -> 'TasksDocument':
        """Require selected identities and exact generation inputs for successful records."""
        rows = [(item.id, item.change_request) for item in [*self.tasks, *self.rejected]]
        rows.extend((item.record.id, item.record.change_request) for item in self.failed)
        identities = [identity for identity, _ in rows]
        _check_ids(identities)
        _check_records(self.meta.repository, rows)
        if set(identities) - set(self.selection.selected_ids):
            raise ValueError('generation result is outside selection')
        for record in self.tasks:
            if record.generation.input_sha256 != self.selection.generation_keys[record.id]:
                raise ValueError('generation result input differs from selection')
        counts = self.meta.counts
        if counts.input != len(self.selection.selected_ids):
            raise ValueError('generation input differs from selection size')
        if (counts.succeeded, counts.rejected, counts.failed) != (
            len(self.tasks),
            len(self.rejected),
            len(self.failed),
        ):
            raise ValueError('generation counts do not match outcomes')
        return self


class BasketChangeRequest(StrictModel):
    """Expose the PR metadata needed by a frozen benchmark basket."""

    kind: Literal['pull_request', 'merge_request']
    repository: StrictStr
    number: StrictInt = Field(gt=0)
    url: StrictStr
    title: StrictStr
    author: StrictStr | None
    merged_at: StrictStr

    @field_validator('url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Require an HTTPS pull request URL."""
        return _https_url(value)

    @field_validator('merged_at')
    @classmethod
    def validate_time(cls, value: str) -> str:
        """Require a UTC merge timestamp."""
        return _utc(value)


class BasketRevisions(StrictModel):
    """Expose the frozen before and after Git revisions."""

    policy: Literal['change_merge_base_v1']
    before: GitSha
    after: GitSha
    before_tree: GitSha
    after_tree: GitSha

    @field_validator('before', 'after', 'before_tree', 'after_tree')
    @classmethod
    def validate_sha(cls, value: str) -> str:
        """Require a Git commit SHA for each basket endpoint."""
        return _sha(value, 40)


class BasketChanges(StrictModel):
    """Expose changed-file metadata without the reference patch."""

    files: list[ChangedFile]
    stats: ChangeStats

    @model_validator(mode='after')
    def check_stats(self) -> 'BasketChanges':
        """Require unique sorted files and matching file and line totals."""
        if [item.path for item in self.files] != sorted(set(item.path for item in self.files)):
            raise ValueError('files must be unique and sorted by path')
        if (
            self.stats.files_count != len(self.files)
            or self.stats.additions != sum(item.additions for item in self.files)
            or self.stats.deletions != sum(item.deletions for item in self.files)
        ):
            raise ValueError('file statistics do not match files')
        return self


class IssueRef(StrictModel):
    """Expose a linked issue identity and URL without its body."""

    id: StrictStr
    url: StrictStr

    @field_validator('url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Require an HTTPS linked issue URL."""
        return _https_url(value)


class Provenance(StrictModel):
    """Expose issue references and task-generation metadata."""

    issue_refs: list[IssueRef]
    generation: Generation
    retrieval: RetrievalReceipt
    source_revisions: SourceRevisions


class BasketRecord(StrictModel):
    """Store one frozen benchmark task and optional reference patch."""

    model_config = ConfigDict(
        json_schema_extra={
            'allOf': [
                {
                    'if': {'properties': {'change_request': {'properties': {'kind': {'const': 'merge_request'}}}}},
                    'then': {
                        'properties': {
                            'provenance': {
                                'properties': {
                                    'source_revisions': {
                                        'properties': {
                                            'diff_version_id': {'type': 'string', 'pattern': '^[1-9][0-9]*$'}
                                        }
                                    }
                                }
                            }
                        }
                    },
                }
            ]
        }
    )

    id: StrictStr
    change_request: BasketChangeRequest
    revisions: BasketRevisions
    source: SourceDescriptor
    diff_policy: Literal['git_source_changes_v1']
    changes: BasketChanges
    task: TaskContent
    agent_task: StrictStr
    workspace: StrictStr
    golden_diff: StrictStr | None = None
    provenance: Provenance

    @field_validator('workspace')
    @classmethod
    def check_workspace(cls, value: str) -> str:
        """Allow the repository root or a normalized relative POSIX directory."""
        return value if value == '.' else _path(value)

    @model_serializer(mode='wrap')
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Omit the optional golden diff unless it was explicitly provided.

        Args:
            handler: Pydantic serializer for the model fields.

        Returns:
            Serialized basket record with optional patch presence preserved.

        """
        data: dict[str, object] = handler(self)
        if 'golden_diff' not in self.model_fields_set:
            data.pop('golden_diff', None)
        return data

    @model_validator(mode='after')
    def check_identity(self) -> 'BasketRecord':
        """Require the basket ID, task, and issue IDs to match their sources.

        The record ID identifies the PR; agent_task follows task_text_v1;
        generation issue IDs preserve the order of linked issue references.
        """
        if re.fullmatch(r'change:[0-9a-f]{64}', self.id) is None:
            raise ValueError('invalid opaque change identity')
        _check_retrieval(self.source, self.revisions, self.provenance.retrieval)
        evidence = self.provenance.source_revisions
        if self.change_request.kind == 'merge_request' and evidence.diff_version_id is None:
            raise ValueError('GitLab source requires a native diff version ID')
        if evidence.head_sha != self.revisions.after or (
            evidence.expected_base_sha is not None and evidence.expected_base_sha != self.revisions.before
        ):
            raise ValueError('source revision evidence differs from frozen execution revisions')
        if self.agent_task != format_agent_task(self.task):
            raise ValueError('agent_task does not match task_text_v1')
        if self.provenance.generation.issue_ids != [issue.id for issue in self.provenance.issue_refs]:
            raise ValueError('generation issue IDs must match issue references in order')
        return self


class BasketDocument(StrictModel):
    """Persist the nonempty frozen basket and its export metadata."""

    meta: BasketMeta
    tasks: list[BasketRecord] = Field(min_length=1)

    @model_validator(mode='after')
    def check_records(self) -> 'BasketDocument':
        """Require unique identities, matching counts, and diff policy.

        Every record belongs to the metadata repository. golden_diff must
        appear exactly when include_golden_diff is true and cannot be null.
        """
        _validate_selected_basket(self.meta, self.tasks)
        return self


def _validate_selected_basket(meta: BasketMeta, tasks: list[BasketRecord]) -> None:
    """Bind frozen records to their complete selected population and optional diff policy."""
    _check_ids([item.id for item in tasks] + [item.id for item in meta.omissions])
    _check_records(meta.repository, [(item.id, item.change_request) for item in tasks])
    if meta.counts.succeeded != len(tasks):
        raise ValueError('export count does not match records')
    generated = {item.id for item in meta.omissions if item.stage == 'generate'}
    selected = meta.selection.selected_ids
    if [item.id for item in tasks] != [identity for identity in selected if identity not in generated]:
        raise ValueError('export task order or generation omissions differ from selection')
    for record in tasks:
        if record.provenance.generation.input_sha256 != meta.selection.generation_keys[record.id]:
            raise ValueError('export generation input differs from selection')
    include = meta.settings.get('include_golden_diff')
    if type(include) is not bool:
        raise ValueError('export settings need include_golden_diff bool')
    for record in tasks:
        present = 'golden_diff' in record.model_fields_set
        if present != include or (present and record.golden_diff is None):
            raise ValueError('golden_diff presence must match export setting')


def _check_ids(ids: list[str]) -> None:
    """Reject case-insensitive duplicate PR identities."""
    if len(ids) != len(set(item.casefold() for item in ids)):
        raise ValueError('duplicate PR identity')


def _check_records(
    repository: Repository,
    records: Sequence[tuple[str, ChangeRequest | RejectedChangeRequest | BasketChangeRequest | RecordIdentity]],
) -> None:
    """Bind each normalized task to the immutable project in stage metadata."""
    for identity, request in records:
        expected = change_id(
            repository.provider, repository.server_url, repository.project_id, request.kind, request.number
        )
        if request.repository != repository.path or identity != expected:
            raise ValueError('record identity differs from meta repository')
        expected_kind = 'pull_request' if repository.provider == 'github' else 'merge_request'
        if request.kind != expected_kind:
            raise ValueError('change kind differs from repository provider')
