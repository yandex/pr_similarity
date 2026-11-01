"""Provider failures remain native facts independent of capture and host scoring."""

import json
import threading
from pathlib import Path
from typing import Literal

import pytest
from benchmark.errors import BenchmarkError
from benchmark.execution import opencode
from benchmark.execution.opencode import EventTracker, Invocation
from benchmark.execution.process import ProcessResult
from benchmark.models.basket import BasketRecord
from benchmark.models.evidence import Seal
from benchmark.models.report import ReportError, ReportInfra
from benchmark.persistence.artifact_refs import read_reference
from benchmark.persistence.layout import attempt_path
from benchmark.persistence.storage import read_json
from benchmark.presentation.result import pending_task
from benchmark.runtime import attempt as executor
from benchmark.runtime.lifecycle import Stop, retry_reasons
from test_attempt_executor import controlled_native, fixture_runtime, reservation
from test_basket import payload


@pytest.mark.parametrize(
    ('errors', 'expected'),
    [
        ([{'name': 'APIError', 'data': {'statusCode': 429}}], 'agent_error'),
        ([{'name': 'ContextOverflowError'}], 'agent_error'),
        ([{'name': 'ProviderModelNotFoundError'}], 'agent_error'),
        ([{'name': 'APIError', 'data': {'statusCode': 503}}], 'provider_unavailable'),
        ([{'name': 'APIError', 'data': {'statusCode': 401}}], 'auth_required'),
        ([{'name': 'APIError', 'data': {'statusCode': 403}}], 'auth_required'),
        (
            [{'name': 'APIError', 'data': {'statusCode': 401}}, {'name': 'APIError', 'data': {'statusCode': 503}}],
            'auth_required',
        ),
        ([{'name': 'UnknownError', 'data': 'unexpected payload'}], 'agent_error'),
    ],
)
def test_provider_errors_survive_native_seal_and_host_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, errors: list[dict], expected: str
) -> None:
    """Exercise real native event routing, source capture and immutable owner references."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    controlled_native(monkeypatch, registration, errors=errors)
    stop = Stop()
    directory = tmp_path / attempt_path(registration, 1)

    def score(*args: object, **kwargs: object) -> dict:
        """Fail host work independently of the already sealed native result."""
        raise BenchmarkError('operation_cancelled' if stop.is_set() else 'scoring_failed', 'Host scoring stopped.')

    monkeypatch.setattr(executor, 'score_saved', score)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            stop,
            registration=registration,
        )
    finally:
        command.close()
    native = read_reference(tmp_path, outcome.seal, Seal).task
    assert native.execution.status == 'error' and native.execution.exit_code == 0
    assert read_json(directory / 'agent-error.json')['code'] == expected
    infrastructure = native.infrastructure_error
    assert (None if infrastructure is None else infrastructure.code) == (
        None if expected == 'agent_error' else expected
    )
    assert native.changes.status == 'nonempty'
    assert outcome.task.infrastructure_error == infrastructure
    assert outcome.task.processing_status in ('completed', 'interrupted')
    assert 'retry_reasons' not in outcome.model_fields_set
    assert stop.is_set() == (expected == 'auth_required')
    assert (None if outcome.fatal is None else outcome.fatal.code) == (
        expected if expected == 'auth_required' else None
    )


@pytest.mark.parametrize('diff', ['empty', 'nonempty'])
def test_agent_error_retry_remains_opt_in_for_coordinator(diff: Literal['empty', 'nonempty']) -> None:
    """Native policy ignores diff content for terminal agent errors."""
    row = pending_task(BasketRecord.model_validate(payload()['tasks'][0]))
    row.processing_status = 'completed'
    row.execution.status = 'error'
    row.changes.status = diff
    assert retry_reasons(row, []) == []
    assert retry_reasons(row, ['agent_error']) == ['agent_error']
    row.infrastructure_error = ReportInfra(
        stage='agent', code='provider_unavailable', message='Provider unavailable.', paths=[]
    )
    assert retry_reasons(row, ['agent_error']) == ['infrastructure_error']
    row.processing_status = 'interrupted'
    assert retry_reasons(row, ['agent_error']) == []


def test_agent_error_category_is_valid_configuration(tmp_path: Path) -> None:
    """Configuration retains the original shared budget and explicit outcome categories."""
    config, _registration, _identity, _attempt, command = reservation(tmp_path)
    command.close()
    assert config.identity().retries.to_wire() == {'max_retries': 3, 'categories': ['agent_error', 'no_changes']}


@pytest.mark.parametrize(
    ('auth_mode', 'stream_failure'), [('chatgpt_oauth', False), ('api_key', False), ('chatgpt_oauth', True)]
)
def test_quota_error_preserves_native_facts_and_stops_only_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_mode: str, stream_failure: bool
) -> None:
    """A terminal quota event stops OAuth work while preserving captured source."""
    overrides = {
        'auth': {'mode': auth_mode},
        'config': {'opencode_json': {'model': 'openai/gpt-6.1-sol'}},
    }
    if auth_mode == 'chatgpt_oauth':
        overrides['auth']['profile'] = str(tmp_path / 'profile')
    config, registration, identity, attempt, command = reservation(tmp_path, agent_overrides=overrides)
    error = {
        'name': 'APIError',
        'data': {
            'statusCode': 429,
            'isRetryable': True,
            'responseBody': json.dumps({'error': {'type': 'usage_limit_reached', 'resets_at': 1793434685}}),
        },
    }
    controlled_native(monkeypatch, registration, errors=[error], model='openai/gpt-6.1-sol')
    if stream_failure:
        original_run = opencode.Invocation.run

        def fail_after_event(
            invocation: Invocation, prompt: bytes, cancel: threading.Event
        ) -> tuple[ProcessResult, EventTracker]:
            """Fail streams after the real child has delivered its quota event."""
            original_run(invocation, prompt, cancel)
            raise OSError('Synthetic stream failure after quota.')

        monkeypatch.setattr(opencode.Invocation, 'run', fail_after_event)
    monkeypatch.setattr(executor, '_oauth', lambda *_args, **_kwargs: None)
    stop = Stop()

    def score(*args: object, **kwargs: object) -> dict:
        """Keep cancellation independent of native evidence and quota classification."""
        raise BenchmarkError('operation_cancelled' if stop.is_set() else 'scoring_failed', 'Scoring stopped.')

    monkeypatch.setattr(executor, 'score_saved', score)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            stop,
            registration=registration,
        )
    finally:
        command.close()
    native = read_reference(tmp_path, outcome.seal, Seal).task
    assert native.execution.status == 'error'
    assert native.changes.status == 'nonempty'
    if auth_mode == 'chatgpt_oauth':
        assert stop.is_set()
        assert native.infrastructure_error is not None
        assert native.infrastructure_error.code == 'provider_quota_exhausted'
        assert native.processing_status == 'interrupted'
        assert outcome.fatal is not None and outcome.fatal.code == 'provider_quota_exhausted'
        assert outcome.task.processing_status == 'interrupted'
        assert '2026-10-31T08:18:05Z' in outcome.fatal.message
    else:
        assert not stop.is_set()
        assert native.infrastructure_error is None and outcome.fatal is None


def test_sibling_provider_error_survives_quota_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sibling's own terminal 503 remains infrastructure evidence after another task exhausts quota."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    controlled_native(monkeypatch, registration, errors=[{'name': 'APIError', 'data': {'statusCode': 503}}])
    stop = Stop()
    original_accept = EventTracker.accept_event

    def accept(tracker: EventTracker, event: dict) -> None:
        """Install another worker's quota stop exactly when the sibling error arrives."""
        original_accept(tracker, event)
        if event.get('type') == 'error':
            stop.fail(ReportError(code='provider_quota_exhausted', message='Another task reached quota.'))

    monkeypatch.setattr(EventTracker, 'accept_event', accept)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            stop,
            registration=registration,
        )
    finally:
        command.close()
    native = read_reference(tmp_path, outcome.seal, Seal).task
    assert native.execution.status == 'error'
    assert native.infrastructure_error is not None and native.infrastructure_error.code == 'provider_unavailable'
    assert outcome.fatal is None
    assert stop.failure is not None and stop.failure.code == 'provider_quota_exhausted'
