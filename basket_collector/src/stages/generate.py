"""Convert a filtered PR into a task or a per-PR generation failure."""

import json
import threading

from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment

from basket_collector.config import (
    GenerationConfig,
    TemplateSnapshot,
    load_template_snapshot,
)
from basket_collector.filters import compact_rejection
from basket_collector.models.artifacts import (
    FilterRecord,
    Generation,
    RejectedRecord,
    RejectionReason,
    TaskContent,
    TaskRecord,
)
from basket_collector.providers.llm import (
    GenerationInterrupted,
    LLMClient,
    LLMConfigurationError,
)

_BLOCKED_NODES = (
    nodes.Call,
    nodes.Filter,
    nodes.Test,
    nodes.Include,
    nodes.Import,
    nodes.FromImport,
    nodes.Extends,
    nodes.Macro,
    nodes.Block,
    nodes.Assign,
    nodes.AssignBlock,
)


class _NoCallEnvironment(SandboxedEnvironment):
    """Render templates without invoking values supplied by the collector."""

    def is_safe_callable(self, obj: object) -> bool:
        """Reject every attempted callable expression in a template."""
        return False


def _rejection(record: FilterRecord, code: str, message: str) -> RejectedRecord:
    """Record a deterministic input exclusion without spending transport retries."""
    return compact_rejection(
        record, RejectionReason(code=code, message=message, observed=None, expected='valid generation input')
    )


def _direct_issue_task(record: FilterRecord) -> TaskContent:
    """Build task content directly from linked issue titles and bodies."""
    if not record.issues:
        raise ValueError('missing_issue')
    if any(not issue.title.strip() for issue in record.issues):
        raise ValueError('invalid_issue')
    if len(record.issues) == 1:
        issue = record.issues[0]
        return TaskContent(task=issue.title.strip(), subtasks=[issue.body.strip()] if issue.body.strip() else [])
    return TaskContent(
        task='\n\n'.join(f'[{issue.id}] {issue.title.strip()}' for issue in record.issues),
        subtasks=[f'[{issue.id}]\n{issue.body.strip()}' for issue in record.issues if issue.body.strip()],
    )


def _render(template_text: str, path: str, variables: dict[str, object]) -> str:
    """Render a restricted Jinja template from explicit variables.

    Callable constructs, includes, imports, assignments, and unknown
    variables are disallowed so templates cannot execute arbitrary logic.

    Raises:
        LLMConfigurationError: If parsing or rendering fails or uses blocked syntax.

    """
    environment = _NoCallEnvironment(undefined=StrictUndefined, autoescape=False, loader=None)
    try:
        ast = environment.parse(template_text)
    except TemplateError as exc:
        raise LLMConfigurationError('template syntax is invalid') from exc
    for node_type in _BLOCKED_NODES:
        if next(ast.find_all(node_type), None) is not None:
            raise LLMConfigurationError(f'unsupported Jinja construct in {path}')
    unknown = meta.find_undeclared_variables(ast) - variables.keys()
    if unknown:
        unknown_names = ', '.join(sorted(unknown))
        raise LLMConfigurationError(f'template uses unavailable variables: {unknown_names}')
    try:
        return environment.from_string(template_text).render(**variables)
    except TemplateError as exc:
        raise LLMConfigurationError('template could not be rendered') from exc


def _prompts(
    record: FilterRecord,
    config: GenerationConfig,
    template_snapshot: TemplateSnapshot | None,
) -> tuple[str, str]:
    """Render the frozen system and user templates for one filtered PR.

    Args:
        record: PR data available to the selected prompt context.
        config: Generation source, context, and template paths.
        template_snapshot: Previously captured template content, if any.

    Returns:
        Rendered system and user prompts for the same template snapshot.

    Raises:
        LLMConfigurationError: If a template is missing, invalid, or unreadable.

    """
    if config.templates.user is None:
        raise LLMConfigurationError('selected user template is missing')
    if template_snapshot is None:
        try:
            template_snapshot = load_template_snapshot(config)
        except (OSError, UnicodeError) as exc:
            raise LLMConfigurationError('template cannot be read') from exc
    assert template_snapshot is not None
    system = _render(
        template_snapshot.system,
        config.templates.system,
        {'result_schema': json.dumps(TaskContent.model_json_schema(), ensure_ascii=False, sort_keys=True)},
    )
    user_vars: dict[str, object] = {}
    if config.context in ('diff', 'diff+issue'):
        user_vars.update(
            {
                'pr_title': record.change_request.title,
                'pr_body': record.change_request.body,
                'commit_messages': [item.message for item in record.changes.commit_messages],
                'diff': record.changes.diff,
            }
        )
    if config.context in ('issue', 'diff+issue'):
        user_vars['issues'] = [{'id': issue.id, 'title': issue.title, 'body': issue.body} for issue in record.issues]
    return system, _render(template_snapshot.user, config.templates.user, user_vars)


def generate_record(
    record: FilterRecord,
    config: GenerationConfig,
    *,
    llm_client: LLMClient | None = None,
    stop_event: threading.Event | None = None,
    template_snapshot: TemplateSnapshot | None = None,
) -> TaskRecord | RejectedRecord:
    """Build one task while leaving interrupted work pending for the coordinator.

    Args:
        record: Filtered change and its selected issue and diff context.
        config: Task source, prompt templates and generation limits.
        llm_client: Optional caller-owned client, never closed here. When absent,
            the LLM path creates and closes its own client locally.
        stop_event: Cooperative cancellation signal checked before generation
            and passed to a locally created client.
        template_snapshot: Frozen prompt texts; absent snapshots are loaded
            from the configured template files.

    Returns:
        TaskRecord with task content and generation provenance on success.
        RejectedRecord for missing issue context, an invalid direct issue title
        or rendered prompts exceeding the configured input limit.

    Raises:
        GenerationInterrupted: If cancellation interrupts generation.
        LLMConfigurationError: If templates or client configuration are invalid.
        LLMCallError: If the provider call fails or its output is unusable.
        LLMAuthenticationError: If the provider rejects authentication.
        ValidationError: If the assembled task violates the persisted model.

    """
    if stop_event is not None and stop_event.is_set():
        raise GenerationInterrupted()
    issue_ids = [issue.id for issue in record.issues]
    if config.source == 'issue':
        try:
            task = _direct_issue_task(record)
        except ValueError as exc:
            code = str(exc)
            return _rejection(
                record,
                code,
                'linked issue is missing' if code == 'missing_issue' else 'linked issue has an empty title',
            )
        generation = Generation(
            issue_ids=issue_ids, attempts=0, response_id=None, response_model=None, usage=None, request_sha256=None
        )
    else:
        if config.context in ('issue', 'diff+issue') and not record.issues:
            return _rejection(record, 'missing_issue', 'linked issue is missing')
        system, user = _prompts(record, config, template_snapshot)
        if len(system) + len(user) > config.max_input_chars:
            return _rejection(record, 'input_too_large', 'rendered prompts exceed max_input_chars')
        if llm_client is None:
            with LLMClient(config, stop_event=stop_event) as local_client:
                result = local_client.generate(system, user)
        else:
            result = llm_client.generate(system, user)
        generation = Generation(
            issue_ids=issue_ids,
            attempts=result.attempts,
            response_id=result.response_id,
            response_model=result.response_model,
            usage=result.usage,
            request_sha256=result.request_sha256,
        )
        task = result.task
    return TaskRecord.model_validate(
        {
            **record.model_dump(mode='json'),
            'task': task.model_dump(mode='json'),
            'generation': generation.model_dump(mode='json'),
        }
    )
