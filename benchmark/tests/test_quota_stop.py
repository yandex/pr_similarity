"""Quota classification, live cancellation and fatal reason precedence."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from benchmark.execution import opencode
from benchmark.execution.opencode import Invocation
from benchmark.execution.process import ProcessResult
from benchmark.execution.provider_errors import classify_provider_error
from benchmark.execution.sandbox import Sandbox
from benchmark.models.report import ReportError
from benchmark.runtime.lifecycle import Stop
from test_attempt_executor import reservation


def quota_error(**reset: object) -> dict:
    """Provide synthetic provider data without private response headers."""
    return {
        'name': 'APIError',
        'data': {
            'statusCode': 429,
            'isRetryable': True,
            'responseBody': json.dumps({'error': {'type': 'usage_limit_reached', **reset}}),
        },
    }


@pytest.mark.parametrize(
    'body', ['{}', '[]', 'null', 'broken', '{"error":null}', '{"error":{"type":"rate_limit_exceeded"}}']
)
def test_ambiguous_429_is_not_quota(body: str) -> None:
    """A 429 without an explicit quota type retains ordinary native policy."""
    error = quota_error()
    error['data']['responseBody'] = body
    assert classify_provider_error(error, oauth_openai=True).code == 'agent_error'


@pytest.mark.parametrize(
    'reset',
    [
        {},
        {'resets_at': True},
        {'resets_at': '1793434685'},
        {'resets_at': -1},
        {'resets_at': 10**100},
        {'resets_in_seconds': False},
        {'resets_in_seconds': -1},
    ],
)
def test_bad_reset_does_not_disable_quota_stop(reset: dict) -> None:
    """Only trustworthy dates are shown; the terminal quota type remains decisive."""
    error = classify_provider_error(quota_error(**reset), oauth_openai=True)
    assert error.code == 'provider_quota_exhausted'
    assert 'reset time is unavailable' in error.message


def test_reset_message_and_api_isolation() -> None:
    """Render known fields while ignoring native retryability and arbitrary headers."""
    assert '10778 seconds' in classify_provider_error(quota_error(resets_in_seconds=10778), oauth_openai=True).message
    assert (
        '2026-10-31T08:18:05Z'
        in classify_provider_error(quota_error(resets_at=1793434685, resets_in_seconds=1), oauth_openai=True).message
    )
    assert classify_provider_error(quota_error(), oauth_openai=False).code == 'agent_error'


@pytest.mark.parametrize('kind', ['text', 'tool_use', 'error'])
def test_live_quota_stops_before_process_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Drive the production transcript callback before a controlled native process returns."""
    config, _registration, _identity, _attempt, command = reservation(
        tmp_path,
        agent_overrides={
            'auth': {'mode': 'chatgpt_oauth', 'profile': str(tmp_path / 'profile')},
            'config': {'opencode_json': {'model': 'openai/gpt-6.1-sol'}},
        },
    )
    command.close()
    source = tmp_path / 'source'
    source.mkdir()
    stop = Stop()
    seen = []

    def on_quota(error: ReportError) -> None:
        """Model the worker callback without waiting on the child process result."""
        seen.append(error)
        stop.fail(error)

    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/bin/true'))
    invocation = Invocation(config, sandbox, tmp_path / 'artifacts', tmp_path / 'logs', on_quota=on_quota)
    invocation._environment = {}

    def process(*args: object, **kwargs: object) -> ProcessResult:
        """Assert cancellation immediately after stdout dispatch, before returning native status."""
        callback = kwargs['on_output']
        assert callable(callback)
        callback('stdout', json.dumps({'type': kind, 'error': quota_error(), 'sessionID': 'root'}))
        assert stop.is_set() == (kind == 'error')
        return ProcessResult('cancelled' if stop.is_set() else 'completed', -15 if stop.is_set() else 0, 1.0, [])

    monkeypatch.setattr(opencode, 'run_process', process)
    result, tracker = invocation.run(b'task', stop)
    assert len(seen) == int(kind == 'error')
    assert len(tracker.errors) == int(kind == 'error')
    assert (result.status == 'cancelled') == (kind == 'error')
    assert (tmp_path / 'logs/events.jsonl').is_file()


def test_parallel_fatal_reason_preserves_first_quota_and_auth_priority() -> None:
    """Simultaneous workers select one quota cause; later auth dominates without signal loss."""
    stop = Stop()
    ready = threading.Barrier(3)

    def fail(index: int) -> bool:
        """Race requests at a barrier rather than relying on scheduler timing."""
        ready.wait(timeout=5)
        return stop.fail(ReportError(code='provider_quota_exhausted', message=str(index)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(fail, index) for index in range(2)]
        ready.wait(timeout=5)
        assert sum(result.result(timeout=5) for result in results) == 1
    assert stop.failure is not None and stop.failure.code == 'provider_quota_exhausted'
    stop.signal(2, None)
    assert stop.fail(ReportError(code='auth_required', message='Renew credentials.'))
    assert stop.failure is not None and stop.failure.code == 'auth_required'
    assert not stop.fail(ReportError(code='provider_quota_exhausted', message='later'))
    assert stop.signal_number == 2
