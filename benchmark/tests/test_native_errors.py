"""Provider failures remain native facts independent of capture and host scoring."""

from pathlib import Path

import pytest
from benchmark import attempt as executor
from benchmark.artifact_refs import read_reference
from benchmark.errors import BenchmarkError
from benchmark.layout import attempt_path
from benchmark.lifecycle import Stop, retry_reasons
from benchmark.provisioning import Runtime
from benchmark.result import pending_task
from benchmark.storage import read_json
from test_attempt_executor import controlled_native, reservation
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
            registration['basket_row'],
            tmp_path,
            identity,
            Runtime(Path('/usr/bin/true'), {}),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            stop,
            registration=registration,
        )
    finally:
        command.close()
    native = read_reference(tmp_path, outcome['seal'])['task']
    assert native['execution']['status'] == 'error' and native['execution']['exit_code'] == 0
    assert read_json(directory / 'agent-error.json')['code'] == expected
    infrastructure = native['infrastructure_error']
    assert (None if infrastructure is None else infrastructure['code']) == (
        None if expected == 'agent_error' else expected
    )
    assert native['changes']['status'] == 'nonempty'
    assert outcome['task']['infrastructure_error'] == infrastructure
    assert outcome['task']['processing_status'] in ('completed', 'interrupted')
    assert 'retry_reasons' not in outcome
    assert stop.is_set() == (expected == 'auth_required')
    assert (None if outcome['fatal'] is None else outcome['fatal']['code']) == (
        expected if expected == 'auth_required' else None
    )


@pytest.mark.parametrize('diff', ['empty', 'nonempty'])
def test_agent_error_retry_remains_opt_in_for_coordinator(diff: str) -> None:
    """Native policy ignores diff content for terminal agent errors."""
    row = pending_task(payload()['tasks'][0])
    row.update(processing_status='completed')
    row['execution']['status'] = 'error'
    row['changes']['status'] = diff
    assert retry_reasons(row, []) == []
    assert retry_reasons(row, ['agent_error']) == ['agent_error']
    row['infrastructure_error'] = {
        'stage': 'agent',
        'code': 'provider_unavailable',
        'message': 'Provider unavailable.',
        'paths': [],
    }
    assert retry_reasons(row, ['agent_error']) == ['infrastructure_error']
    row['processing_status'] = 'interrupted'
    assert retry_reasons(row, ['agent_error']) == []


def test_agent_error_category_is_valid_configuration(tmp_path: Path) -> None:
    """Configuration retains the original shared budget and explicit outcome categories."""
    config, _registration, _identity, _attempt, command = reservation(tmp_path)
    command.close()
    assert config.identity()['retries'] == {'max_retries': 3, 'categories': ['agent_error', 'no_changes']}
