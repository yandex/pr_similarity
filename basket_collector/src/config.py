"""Strict YAML configuration and stage-specific effective settings."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from basket_collector.contracts import JsonValue, TaskContent
from basket_collector.sources import canonical_server
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


class ConfigModel(BaseModel):
    """Base model that rejects unknown fields and non-finite values."""

    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


def _positive_float(value: float) -> float:
    """Return a positive value or reject it as invalid configuration."""
    if value <= 0:
        raise ValueError('must be positive')
    return value


def _fraction(value: float) -> float:
    """Validate a fraction in the inclusive unit interval."""
    if not 0 <= value <= 1:
        raise ValueError('fraction must be in [0, 1]')
    return value


def _regexes(values: list[str]) -> list[str]:
    """Validate each configured regular expression without changing its text.

    Raises:
        ValueError: A pattern cannot be compiled; the message includes its index.

    """
    for index, value in enumerate(values):
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f'invalid regex at index {index}: {exc}') from exc
    return values


def _url(value: str) -> str:
    """Validate an HTTPS API URL without credentials or request components."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != 'https'
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError('expected HTTPS URL without credentials, query or fragment')
    return value.rstrip('/')


class DatasetConfig(ConfigModel):
    """Human-readable identity and maintainers of the exported dataset."""

    name: StrictStr = Field(min_length=1)
    description: StrictStr
    maintainers: list[StrictStr] = Field(min_length=1)


class GitHubConfig(ConfigModel):
    """GitHub credential name and request limits for collection."""

    token_env: StrictStr = 'GITHUB_TOKEN'
    api_version: StrictStr = '2026-03-10'
    request_timeout_seconds: StrictInt = Field(default=60, gt=0)
    max_retries: StrictInt = Field(default=3, ge=0)
    requests_per_second: StrictInt = Field(default=2, gt=0)
    max_retry_delay_seconds: StrictInt = Field(default=120, gt=0)
    max_pages: StrictInt = Field(default=1000, gt=0)

    @field_validator('token_env')
    @classmethod
    def check_env(cls, value: str) -> str:
        """Require a valid environment variable name for the GitHub token."""
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value) is None:
            raise ValueError('invalid environment variable name')
        return value


class SourceConfig(ConfigModel):
    """Locate one hosting project and configure bounded API reads independently of Git."""

    provider: Literal['github', 'gitlab']
    project: StrictStr
    server_url: StrictStr = ''
    token_env: StrictStr = ''
    api_version: StrictStr = ''
    request_timeout_seconds: StrictInt = Field(default=60, gt=0)
    max_retries: StrictInt = Field(default=3, ge=0)
    requests_per_second: StrictInt = Field(default=2, gt=0)
    max_retry_delay_seconds: StrictInt = Field(default=120, gt=0)
    max_pages: StrictInt = Field(default=1000, gt=0)

    @model_validator(mode='after')
    def check_source(self) -> 'SourceConfig':
        """Resolve provider defaults without adding a visibility mode."""
        self.server_url = canonical_server(
            self.server_url or 'https://' + ('github.com' if self.provider == 'github' else 'gitlab.com')
        )
        self.token_env = self.token_env or ('GITHUB_TOKEN' if self.provider == 'github' else 'GITLAB_TOKEN')
        self.api_version = self.api_version or ('2026-03-10' if self.provider == 'github' else 'v4')
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', self.token_env) is None:
            raise ValueError('invalid source.token_env variable name')
        if self.provider == 'github' and (self.server_url != 'https://github.com' or len(self.project.split('/')) != 2):
            raise ValueError('GitHub source requires github.com and owner/repository')
        if self.provider == 'gitlab' and self.api_version != 'v4':
            raise ValueError('GitLab source requires REST API v4')
        if not self.project or any(part in ('', '.', '..') for part in self.project.split('/')) or '\\' in self.project:
            raise ValueError('source.project must be a project ID or normalized namespace/project')
        return self


class GitConfig(ConfigModel):
    """Bound Git object retrieval and cold-cache source verification."""

    timeout_seconds: StrictInt = Field(default=120, gt=0)
    initial_depth: StrictInt = Field(default=1, gt=0)
    max_depth: StrictInt = Field(default=4096, gt=0)
    max_transfer_bytes: StrictInt = Field(default=536870912, gt=0)

    @model_validator(mode='after')
    def check_depths(self) -> 'GitConfig':
        """Keep the first retrieval within the configured total history bound."""
        if self.initial_depth > self.max_depth:
            raise ValueError('filter.git.initial_depth exceeds filter.git.max_depth')
        return self


class RetryPolicy(ConfigModel):
    """Bound additional record attempts and interruptible exponential backoff."""

    max_retries: StrictInt = Field(default=3, ge=0)
    retry_delay_seconds: StrictInt = Field(default=10, gt=0)
    max_retry_delay_seconds: StrictInt = Field(default=120, gt=0)

    @model_validator(mode='after')
    def check_retry_delays(self) -> 'RetryPolicy':
        """Reject reversed delay bounds independently of whether retries are enabled."""
        if self.retry_delay_seconds > self.max_retry_delay_seconds:
            raise ValueError('retry_delay_seconds exceeds max_retry_delay_seconds')
        return self


class CollectConfig(RetryPolicy):
    """Repository and mutually exclusive PR selection settings."""

    selection: Literal['date_range', 'change_ids'] = 'date_range'
    default_branch_only: StrictBool = True
    merged_from: StrictStr | None = None
    merged_before: StrictStr | None = None
    change_ids: list[StrictInt] | None = None
    max_changes: StrictInt = Field(default=1000, gt=0)

    @model_validator(mode='after')
    def check_source(self) -> 'CollectConfig':
        """Validate the selected PR source and its required bounds or IDs."""
        if self.selection == 'change_ids':
            if self.merged_from is not None or self.merged_before is not None:
                raise ValueError('change_ids source forbids date boundaries')
            if (
                not self.change_ids
                or any(item <= 0 for item in self.change_ids)
                or len(set(self.change_ids)) != len(self.change_ids)
                or len(self.change_ids) > self.max_changes
            ):
                raise ValueError('change_ids must be a nonempty unique positive list within max_changes')
        else:
            if self.change_ids is not None or self.merged_from is None or self.merged_before is None:
                raise ValueError('date_range needs both UTC boundaries and no change_ids')
            try:
                if (
                    re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', self.merged_from) is None
                    or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', self.merged_before) is None
                ):
                    raise ValueError('expected UTC RFC 3339 timestamps ending in Z')
                start = datetime.fromisoformat(self.merged_from.replace('Z', '+00:00'))
                end = datetime.fromisoformat(self.merged_before.replace('Z', '+00:00'))
            except ValueError as exc:
                raise ValueError('invalid UTC date boundary') from exc
            for value, instant in ((self.merged_from, start), (self.merged_before, end)):
                if not value.endswith('Z') or instant.utcoffset() != timezone.utc.utcoffset(instant):
                    raise ValueError('date boundaries must be UTC and end in Z')
            if start >= end:
                raise ValueError('merged_from must precede merged_before')
        return self


class ScopeConfig(ConfigModel):
    """Optional path prefix and affected subfolder limit."""

    enabled: StrictBool = False
    prefix: StrictStr = ''
    subfolders_affected: StrictInt | None = Field(default=None, gt=0)

    @field_validator('prefix')
    @classmethod
    def check_prefix(cls, value: str) -> str:
        """Require a normalized relative POSIX path prefix."""
        if (
            value.startswith('/')
            or '\\' in value
            or '\x00' in value
            or (value and any(part in ('', '.', '..') for part in value.split('/')))
        ):
            raise ValueError('scope prefix must be normalized relative POSIX path')
        return value


class SizeConfig(ConfigModel):
    """Independent size bounds for changed text, lines, and files."""

    enabled: StrictBool = True
    min_chars: StrictInt | None = Field(default=None, ge=0)
    max_chars: StrictInt | None = Field(default=1000000, ge=0)
    min_lines: StrictInt | None = Field(default=1, ge=0)
    max_lines: StrictInt | None = Field(default=10000, ge=0)
    min_files: StrictInt | None = Field(default=1, ge=0)
    max_files: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def check_ranges(self) -> 'SizeConfig':
        """Reject size ranges whose lower bound exceeds the upper bound."""
        for name in ('chars', 'lines', 'files'):
            lower, upper = getattr(self, f'min_{name}'), getattr(self, f'max_{name}')
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f'min_{name} exceeds max_{name}')
        return self


class FilesConfig(ConfigModel):
    """Source file proportions and path pattern filters."""

    enabled: StrictBool = False
    source_extensions: list[StrictStr] = Field(
        default_factory=lambda: ['.py', '.go', '.java', '.js', '.ts', '.cpp', '.h', '.rs']
    )
    min_source_proportion: StrictFloat | None = None
    max_source_proportion: StrictFloat | None = None
    allowed_path_patterns: list[StrictStr] = Field(default_factory=list)
    forbidden_path_patterns: list[StrictStr] = Field(default_factory=list)
    required_path_patterns: list[StrictStr] = Field(default_factory=list)

    @field_validator('min_source_proportion', 'max_source_proportion')
    @classmethod
    def check_fraction(cls, value: float | None) -> float | None:
        """Validate an optional source file proportion."""
        return _fraction(value) if value is not None else None

    @field_validator('allowed_path_patterns', 'forbidden_path_patterns', 'required_path_patterns')
    @classmethod
    def check_patterns(cls, value: list[str]) -> list[str]:
        """Check configured path patterns before any stage begins."""
        return _regexes(value)

    @model_validator(mode='after')
    def check_range(self) -> 'FilesConfig':
        """Check source proportion bounds and extension syntax."""
        if (
            self.min_source_proportion is not None
            and self.max_source_proportion is not None
            and self.min_source_proportion > self.max_source_proportion
        ):
            raise ValueError('min_source_proportion exceeds max_source_proportion')
        if any(re.fullmatch(r'\.[a-z0-9]+', item) is None for item in self.source_extensions):
            raise ValueError('source_extensions must be lowercase dotted suffixes')
        return self


class IssuesConfig(ConfigModel):
    """Policy for PRs with zero or multiple linked issues."""

    allow_no_issue: StrictBool = False
    allow_multiple_issues: StrictBool = False


class PercentileConfig(ConfigModel):
    """Optional percentile trimming of selected PRs."""

    enabled: StrictBool = False
    metric: Literal['changed_lines', 'files_count', 'patch_chars'] = 'changed_lines'
    lower_fraction: StrictFloat = 0.05
    upper_fraction: StrictFloat = 0.05

    @model_validator(mode='after')
    def check_fractions(self) -> 'PercentileConfig':
        """Keep both percentile tails below half of the sample."""
        if (
            not 0 <= self.lower_fraction < 0.5
            or not 0 <= self.upper_fraction < 0.5
            or self.lower_fraction + self.upper_fraction >= 1
        ):
            raise ValueError('percentile tails must be in [0, 0.5) and sum below one')
        return self


class SelectionConfig(ConfigModel):
    """Shuffle seed and final filtered PR limit."""

    shuffle: StrictBool = True
    seed: StrictInt = 42
    limit: StrictInt | None = Field(default=100, gt=0)


class FilterConfig(RetryPolicy):
    """Combined scope, size, file, issue, and selection policies."""

    git: GitConfig = Field(default_factory=GitConfig)

    workers: StrictInt = Field(default=4, gt=0)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    size: SizeConfig = Field(default_factory=SizeConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    issues: IssuesConfig = Field(default_factory=IssuesConfig)
    percentile: PercentileConfig = Field(default_factory=PercentileConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)


class TemplatesConfig(ConfigModel):
    """Paths to the system and optional user prompt templates."""

    system: StrictStr = './prompts/system.jinja2'
    user: StrictStr | None = None


class ReasoningConfig(ConfigModel):
    """Requested reasoning effort for the LLM API."""

    effort: Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh']


class LLMParameters(ConfigModel):
    """Provider-neutral generation parameters."""

    max_output_tokens: StrictInt | None = Field(default=4096, gt=0)
    temperature: StrictFloat | None = Field(default=None, ge=0, le=2)
    top_p: StrictFloat | None = Field(default=None, ge=0, le=1)
    reasoning: ReasoningConfig | None = None

    def api_parameters(self, api: str) -> dict[str, JsonValue]:
        """Translate generation parameters for the selected OpenAI API.

        Args:
            api: Either ``openai_responses`` or ``openai_completions``.

        Returns:
            Parameters using the selected API's field names.

        """
        result: dict[str, JsonValue] = {}
        if self.max_output_tokens is not None:
            result['max_output_tokens' if api == 'openai_responses' else 'max_completion_tokens'] = (
                self.max_output_tokens
            )
        if self.temperature is not None:
            result['temperature'] = self.temperature
        if self.top_p is not None:
            result['top_p'] = self.top_p
        if self.reasoning is not None:
            if api == 'openai_responses':
                result['reasoning'] = {'effort': self.reasoning.effort}
            else:
                result['reasoning_effort'] = self.reasoning.effort
        return result


class LLMConfig(ConfigModel):
    """LLM endpoint, model, credential name, and request parameters."""

    api: Literal['openai_completions', 'openai_responses'] = 'openai_responses'
    base_url: StrictStr = 'https://api.openai.com/v1'
    api_key_env: StrictStr = 'OPENAI_API_KEY'
    model: StrictStr = Field(min_length=1)
    parameters: LLMParameters = Field(default_factory=LLMParameters)

    @field_validator('base_url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Validate and normalize the configured LLM base URL."""
        return _url(value)

    @field_validator('api_key_env')
    @classmethod
    def check_env(cls, value: str) -> str:
        """Require a valid environment variable name for the LLM key."""
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value) is None:
            raise ValueError('invalid environment variable name')
        return value


class GenerationConfig(RetryPolicy):
    """Task generation source, templates, retry limits, and LLM setup."""

    source: Literal['issue', 'llm'] = 'llm'
    context: Literal['diff', 'issue', 'diff+issue'] = 'diff+issue'
    workers: StrictInt = Field(default=2, gt=0)
    request_timeout_seconds: StrictInt = Field(default=120, gt=0)
    requests_per_second: StrictInt = Field(default=1, gt=0)
    max_input_chars: StrictInt = Field(default=1200000, gt=0)
    templates: TemplatesConfig = Field(default_factory=TemplatesConfig)
    llm: LLMConfig | None = None

    @model_validator(mode='after')
    def check_llm(self) -> 'GenerationConfig':
        """Require LLM settings only when LLM generation is selected."""
        if self.source == 'llm' and self.llm is None:
            raise ValueError('generation.llm is required for source llm')
        return self


class ExportConfig(ConfigModel):
    """Control whether exported records include the golden diff."""

    include_golden_diff: StrictBool = False


class CollectorConfig(ConfigModel):
    """Complete collector configuration before local paths are resolved."""

    dataset: DatasetConfig
    env_file: StrictStr | None = './.env'
    output_dir: StrictStr = './output'
    cache_dir: StrictStr = './cache'
    schema_version: Literal[2]
    source: SourceConfig
    collect: CollectConfig
    filter: FilterConfig = Field(default_factory=FilterConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)

    @field_validator('schema_version', mode='before')
    @classmethod
    def check_version(cls, value: object) -> int:
        """Reject bool/float and pre-release formats before any external operation."""
        if type(value) is not int or value != 2:
            raise ValueError('schema_version must be integer 2; only collector v2 configuration is supported')
        return value


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys."""

    pass


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, object]:
    """Construct a YAML mapping while rejecting duplicate or non-string keys.

    Raises:
        ValueError: A mapping key is not a string or appears twice.

    """
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ValueError('YAML mapping keys must be strings')
        if key in result:
            raise ValueError(f'duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_config(path: str | Path) -> CollectorConfig:
    """Read YAML, reject duplicate/unknown keys and resolve local paths from it."""
    config_path = Path(path).expanduser().resolve(strict=True)
    source = config_path.read_text(encoding='utf-8')
    try:
        value = yaml.load(source, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        location = f' at line {mark.line + 1}, column {mark.column + 1}' if mark is not None else ''
        lines = source.splitlines()
        last_key = None
        for line in lines[: mark.line + 1 if mark is not None else len(lines)]:
            match = re.match(r'^\s*([A-Za-z_][A-Za-z_0-9]*)\s*:', line)
            if match is not None:
                last_key = match.group(1)
        field = f' near field {last_key}' if last_key is not None else ''
        problem = getattr(exc, 'problem', None) or getattr(exc, 'context', None) or type(exc).__name__
        raise ValueError(f'invalid YAML configuration{field}{location}: {problem}') from exc
    if not isinstance(value, dict):
        raise ValueError('configuration must be a YAML mapping')
    config = CollectorConfig.model_validate(value)
    generation = config.generation
    if generation.source == 'llm':
        user_default = {
            'diff': './prompts/diff.jinja2',
            'issue': './prompts/issue.jinja2',
            'diff+issue': './prompts/diff_issue.jinja2',
        }[generation.context]
        templates = generation.templates.model_copy(
            update={
                'system': str((config_path.parent / generation.templates.system).resolve(strict=True)),
                'user': str((config_path.parent / (generation.templates.user or user_default)).resolve(strict=True)),
            }
        )
        generation = generation.model_copy(update={'templates': templates})
    return config.model_copy(
        update={
            'env_file': str((config_path.parent / config.env_file).resolve()) if config.env_file is not None else None,
            'output_dir': str((config_path.parent / config.output_dir).resolve()),
            'cache_dir': str((config_path.parent / config.cache_dir).resolve()),
            'generation': generation,
        }
    )


def load_environment(config: CollectorConfig) -> None:
    """Fill missing process variables from the optional local env file."""
    if config.env_file is not None:
        load_dotenv(dotenv_path=config.env_file, override=False, interpolate=False)


@dataclass(frozen=True, slots=True)
class TemplateSnapshot:
    """Selected template contents shared by settings and every PR in one stage."""

    system: str
    user: str


def load_template_snapshot(generation: GenerationConfig) -> TemplateSnapshot | None:
    """Read both selected templates once before generating any PR."""
    if generation.source == 'issue':
        return None
    if generation.templates.user is None:
        raise ValueError('LLM generation requires a selected user template')
    return TemplateSnapshot(
        system=Path(generation.templates.system).read_text(encoding='utf-8'),
        user=Path(generation.templates.user).read_text(encoding='utf-8'),
    )


def _template(content: str) -> dict[str, JsonValue]:
    """Package prompt contents with their SHA-256 digest."""
    return {'content': content, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()}


def effective_settings(
    config: CollectorConfig,
    stage: str,
    *,
    template_snapshot: TemplateSnapshot | None = None,
) -> dict[str, JsonValue]:
    """Return only semantic, public inputs; output/cache paths never enter hashes."""
    if stage == 'collect':
        return {'collect': config.collect.model_dump(mode='json'), 'source': config.source.model_dump(mode='json')}
    if stage == 'filter':
        return {
            'filter': config.filter.model_dump(mode='json', exclude={'selection', 'percentile'}),
            'source': config.source.model_dump(mode='json'),
            'revision_policy': 'change_merge_base_v1',
            'diff_policy': 'git_source_changes_v1',
        }
    if stage == 'generate':
        generation = config.generation
        result: dict[str, JsonValue] = {
            'source': generation.source,
            'max_retries': generation.max_retries,
            'retry_delay_seconds': generation.retry_delay_seconds,
            'max_retry_delay_seconds': generation.max_retry_delay_seconds,
        }
        if generation.source == 'issue':
            return result
        if generation.llm is None or generation.templates.user is None:
            raise ValueError('LLM generation requires llm config and selected templates')
        snapshot = template_snapshot if template_snapshot is not None else load_template_snapshot(generation)
        assert snapshot is not None
        result.update(
            {
                'context': generation.context,
                'workers': generation.workers,
                'max_retries': generation.max_retries,
                'request_timeout_seconds': generation.request_timeout_seconds,
                'requests_per_second': generation.requests_per_second,
                'max_retry_delay_seconds': generation.max_retry_delay_seconds,
                'max_input_chars': generation.max_input_chars,
                'templates': {'system': _template(snapshot.system), 'user': _template(snapshot.user)},
                'result_schema': TaskContent.model_json_schema(),
                'llm': generation.llm.model_dump(mode='json'),
            }
        )
        return result
    if stage == 'export':
        return {
            'dataset': config.dataset.model_dump(mode='json'),
            'include_golden_diff': config.export.include_golden_diff,
            'task_format': 'task_text_v1',
        }
    raise ValueError(f'unknown stage: {stage}')
