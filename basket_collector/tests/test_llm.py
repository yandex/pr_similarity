"""Exercise bounded LLM requests and strict structured responses offline."""

import asyncio
import json
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

import httpx
import pytest
from basket_collector.config import GenerationConfig
from basket_collector.llm import (
    GenerationInterrupted,
    LLMAuthenticationError,
    LLMCallError,
    LLMClient,
    LLMConfigurationError,
    RateLimiter,
)
from basket_collector.transport_scope import generation_requests, record_scope


def _config(api: str = 'openai_responses', **overrides: object) -> GenerationConfig:
    """Build the local configuration fixture for this test module."""
    return GenerationConfig.model_validate(
        {
            'source': 'llm',
            'max_retries': 0,
            'llm': {
                'api': api,
                'model': 'test-model',
                'base_url': 'https://example.test/v1',
                'api_key_env': 'TEST_LLM_KEY',
            },
            **overrides,
        }
    )


def _response(api: str, content: str = '{"task":"Implement change","subtasks":[]}') -> dict[str, object]:
    """Build a valid structured task response for the selected API."""
    if api == 'openai_completions':
        return {
            'id': 'chat-1',
            'model': 'model-snapshot',
            'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
        }
    return {
        'id': 'resp-1',
        'model': 'model-snapshot',
        'status': 'completed',
        'output': [{'type': 'message', 'status': 'completed', 'content': [{'type': 'output_text', 'text': content}]}],
        'usage': {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5},
    }


@pytest.mark.parametrize(
    'api,path,field',
    [
        ('openai_completions', '/v1/chat/completions', 'response_format'),
        ('openai_responses', '/v1/responses', 'text'),
    ],
)
def test_strict_payload_and_normalized_result(monkeypatch: pytest.MonkeyPatch, api: str, path: str, field: str) -> None:
    """Verify strict API payloads and normalized successful task metadata."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the selected API response and inspect its request."""
        assert request.url.path == path
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload['store'] is False
        assert payload[field]['format' if api == 'openai_responses' else 'json_schema']['strict'] is True
        assert 'secret' not in json.dumps(payload)
        return httpx.Response(200, json=_response(api))

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(api), http_client=transport) as client:
            result = client.generate('System', 'User')
    assert len(seen) == 1
    assert result.task.task == 'Implement change'
    assert result.attempts == 1
    assert result.response_model == 'model-snapshot'
    assert result.usage is not None and result.usage.total_tokens == 5
    assert len(result.request_sha256) == 64


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
@pytest.mark.parametrize(
    'content',
    [
        '{"task":" ","subtasks":[]}',
        '{"task":"Work","subtasks":[],"extra":1}',
        '```json\n{"task":"Work","subtasks":[]}\n```',
    ],
)
def test_invalid_content_is_per_pr_failure(monkeypatch: pytest.MonkeyPatch, api: str, content: str) -> None:
    """Verify invalid structured model content fails only its PR."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_response(api, content)))
    ) as transport:
        with LLMClient(_config(api), http_client=transport) as client:
            with pytest.raises(LLMCallError) as caught:
                client.generate('system', 'user')
    assert caught.value.code == 'invalid_output'
    assert caught.value.attempts == 1


def test_default_retries_have_four_total_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify default retry settings allow four total HTTP attempts."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: default retry settings allow four total HTTP attempts."""
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={'Retry-After': '0'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(max_retries=3, requests_per_second=100000), http_client=transport) as client:
            with pytest.raises(LLMCallError) as caught:
                client.generate('system', 'user')
    assert calls == 4
    assert caught.value.attempts == 4
    assert caught.value.retryable is True


@pytest.mark.parametrize('key', [None, ''])
def test_missing_key_is_recoverable_without_making_a_request(
    monkeypatch: pytest.MonkeyPatch,
    key: str | None,
) -> None:
    """Treat absent and empty keys as repairable credentials before HTTP starts."""
    if key is None:
        monkeypatch.delenv('TEST_LLM_KEY', raising=False)
    else:
        monkeypatch.setenv('TEST_LLM_KEY', key)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail('request started without credentials'))
    ) as transport:
        with LLMClient(_config(max_retries=3), http_client=transport) as client:
            with pytest.raises(
                LLMAuthenticationError, match='missing API key environment variable TEST_LLM_KEY'
            ) as caught:
                client.generate('system', 'user')
    assert isinstance(caught.value, LLMConfigurationError)


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
@pytest.mark.parametrize('status', [401, 403])
def test_auth_error_is_distinct_secret_safe_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    api: str,
    status: int,
) -> None:
    """Verify authentication errors are distinct, secret-safe, and not retried."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret-value')
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return an authentication rejection without exposing its body."""
        nonlocal calls
        calls += 1
        return httpx.Response(status, text='secret-value was rejected')

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(api, max_retries=3), http_client=transport) as client:
            with pytest.raises(LLMAuthenticationError) as caught:
                client.generate('system', 'user')
    assert calls == 1
    assert str(status) in str(caught.value)
    assert 'secret-value' not in str(caught.value)


def test_stop_prevents_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a preexisting stop signal prevents the first LLM request."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    stop = threading.Event()
    stop.set()
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail('request started after stop'))
    ) as transport:
        with LLMClient(_config(), http_client=transport, stop_event=stop) as client:
            with pytest.raises(GenerationInterrupted):
                client.generate('system', 'user')


@pytest.mark.parametrize(
    'api,mutation,code',
    [
        ('openai_completions', lambda value: value['choices'][0]['message'].update(refusal='No'), 'model_refusal'),
        ('openai_completions', lambda value: value['choices'][0].update(finish_reason='length'), 'incomplete_response'),
        ('openai_responses', lambda value: value['output'][0]['content'][0].update(type='refusal'), 'model_refusal'),
        ('openai_responses', lambda value: value.update(status='incomplete'), 'incomplete_response'),
    ],
)
def test_refusal_and_incomplete(
    monkeypatch: pytest.MonkeyPatch, api: str, mutation: Callable[[dict[str, object]], object], code: str
) -> None:
    """Verify model refusal and incomplete output become typed task failures."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    response = _response(api)
    mutation(response)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as transport:
        with LLMClient(_config(api), http_client=transport) as client:
            with pytest.raises(LLMCallError) as caught:
                client.generate('system', 'user')
    assert caught.value.code == code
    assert caught.value.attempts == 1


class SlowDrip(httpx.AsyncByteStream):
    """Emit a never-ending response body and record when it closes."""

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        """Save an optional stop signal and create a closure marker."""
        self.closed = threading.Event()
        self.stop_event = stop_event

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield delayed bytes until the request is cancelled."""
        try:
            while True:
                await asyncio.sleep(0.1)
                if self.stop_event is not None:
                    self.stop_event.set()
                yield b' '
        finally:
            self.closed.set()

    async def aclose(self) -> None:
        """Mark the streaming response as closed."""
        self.closed.set()


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
def test_overall_deadline_stops_slow_drip_body(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    """Verify a slow streaming body is closed at the overall deadline."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    stream = SlowDrip()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: a slow streaming body is closed at the overall deadline."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=stream)

    start = time.monotonic()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(api, request_timeout_seconds=1), http_client=transport) as client:
            with pytest.raises(LLMCallError) as caught:
                client.generate('system', 'user')
    elapsed = time.monotonic() - start
    assert calls == 1
    assert caught.value.code == 'request_failed'
    assert caught.value.attempts == 1
    assert stream.closed.is_set()
    assert elapsed < 2.5


def test_stop_during_slow_drip_prevents_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify shutdown during a streaming body closes it without retry."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    stop = threading.Event()
    stream = SlowDrip(stop)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: shutdown during a streaming body closes it without retry."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(
            _config(max_retries=3, request_timeout_seconds=1), http_client=transport, stop_event=stop
        ) as client:
            with pytest.raises(GenerationInterrupted):
                client.generate('system', 'user')
    assert calls == 1
    assert stream.closed.is_set()


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
def test_stop_during_terminal_read_timeout_leaves_request_pending(
    monkeypatch: pytest.MonkeyPatch,
    api: str,
) -> None:
    """Verify shutdown during the final read timeout leaves generation pending."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    stop = threading.Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Set shutdown and raise a read timeout on the final attempt."""
        nonlocal calls
        calls += 1
        stop.set()
        raise httpx.ReadTimeout('response body timed out', request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(api, max_retries=0), http_client=transport, stop_event=stop) as client:
            with pytest.raises(GenerationInterrupted):
                client.generate('system', 'user')
    assert calls == 1


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
def test_successful_response_during_stop_is_retained(monkeypatch: pytest.MonkeyPatch, api: str) -> None:
    """Verify a completed response remains successful when shutdown starts."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    stop = threading.Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Set shutdown while returning a successful response."""
        nonlocal calls
        calls += 1
        stop.set()
        return httpx.Response(200, json=_response(api))

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(api, max_retries=0), http_client=transport, stop_event=stop) as client:
            result = client.generate('system', 'user')
    assert calls == 1
    assert result.task.task == 'Implement change'
    assert result.attempts == 1


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
def test_stop_during_terminal_retryable_http_error_leaves_request_pending(
    monkeypatch: pytest.MonkeyPatch,
    api: str,
) -> None:
    """Verify shutdown during a final retryable HTTP error leaves work pending."""
    monkeypatch.setenv('TEST_LLM_KEY', 'secret')
    stop = threading.Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Set shutdown and return a retryable HTTP error."""
        nonlocal calls
        calls += 1
        stop.set()
        return httpx.Response(429, headers={'Retry-After': '0'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(api, max_retries=0), http_client=transport, stop_event=stop) as client:
            with pytest.raises(GenerationInterrupted):
                client.generate('system', 'user')
    assert calls == 1


@pytest.mark.parametrize('status', [429, 503])
def test_retry_after_delays_next_record_after_exhaustion(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """An exhausted record leaves a shared uncapped cooldown for the next record."""
    clock = [100.0]
    monkeypatch.setattr('basket_collector.llm.time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setenv('TEST_LLM_KEY', 'fixture')
    sent: list[float] = []

    class ClockEvent(threading.Event):
        """Advance virtual time only when the limiter actually waits."""

        def wait(self, timeout: float | None = None) -> bool:
            """Simulate an uninterrupted wait without delaying the test."""
            assert timeout is not None
            clock[0] += timeout
            return False

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail each record on its only allowed attempt."""
        sent.append(clock[0])
        return httpx.Response(status, headers={'Retry-After': '60'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(requests_per_second=100), http_client=transport, stop_event=ClockEvent()) as client:
            for _ in range(2):
                with record_scope(), pytest.raises(LLMCallError) as failure:
                    client.generate('system', 'user')
                assert failure.value.retry_after == 60
    assert sent[1] >= sent[0] + 60


@pytest.mark.parametrize('error', [httpx.LocalProtocolError, httpx.UnsupportedProtocol, RuntimeError])
def test_local_llm_request_defects_are_not_record_failures(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """Do not normalize programming or request configuration defects into retryable external failures."""
    monkeypatch.setenv('TEST_LLM_KEY', 'fixture')

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail before receiving any remote response."""
        raise error('fixture local defect')

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(_config(), http_client=transport) as client, pytest.raises(error):
            client.generate('system', 'user')


def test_shared_limiter_rechecks_extended_cooldown_for_parallel_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Waiting workers share extensions and wake promptly on cancellation without consuming slots."""
    clock = [100.0]
    monkeypatch.setattr('basket_collector.llm.time', SimpleNamespace(monotonic=lambda: clock[0]))
    limiter = RateLimiter(100)
    limiter.defer(60)
    limiter.defer(5)
    observations: queue.Queue[tuple[int, float]] = queue.Queue()

    class ObservedEvent(threading.Event):
        """Observe actual limiter waits without advancing virtual time."""

        def wait(self, timeout: float | None = None) -> bool:
            """Expose the requesting thread and its current virtual timestamp."""
            observations.put((threading.get_ident(), clock[0]))
            return super().wait(0.01)

    stop = ObservedEvent()
    entered = threading.Barrier(3)
    admitted: list[float] = []
    cancelled: list[bool] = []

    def worker() -> None:
        """Wait for shared admission, recording interruption instead of issuing a request."""
        entered.wait(timeout=2)
        try:
            limiter.acquire(stop)
            admitted.append(clock[0])
        except GenerationInterrupted:
            cancelled.append(True)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    entered.wait(timeout=2)
    seen = set()
    while len(seen) < 2:
        identity, instant = observations.get(timeout=2)
        assert instant == 100
        seen.add(identity)
    assert admitted == []
    clock[0] = 150
    limiter.defer(60)
    clock[0] = 170
    seen.clear()
    while len(seen) < 2:
        identity, instant = observations.get(timeout=2)
        if instant == 170:
            seen.add(identity)
    assert admitted == []
    stop.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert cancelled == [True, True] and admitted == []
    clock[0] = 210
    limiter.acquire(threading.Event())


def test_cooldown_arriving_during_durable_request_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker delayed by fsync must honor another worker's Retry-After before sending HTTP."""
    clock = [100.0]
    monkeypatch.setattr('basket_collector.llm.time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setenv('TEST_LLM_KEY', 'fixture')
    reserved, release, waiting = threading.Event(), threading.Event(), threading.Event()
    sent: list[float] = []
    errors: list[BaseException] = []
    reservations: list[bool] = []

    class ObservedEvent(threading.Event):
        """Expose final admission waiting while keeping cancellation functional."""

        def wait(self, timeout: float | None = None) -> bool:
            """Signal a wait without advancing the shared virtual clock."""
            waiting.set()
            return super().wait(0.01)

    def reserve() -> None:
        """Block one worker's durable launch acknowledgement."""
        reservations.append(True)
        reserved.set()
        assert release.wait(2)

    def handler(request: httpx.Request) -> httpx.Response:
        """Return rate limiting to the other worker while the first saves its reservation."""
        sent.append(clock[0])
        if len(sent) == 1:
            return httpx.Response(429, headers={'Retry-After': '60'})
        return httpx.Response(200, json=_response('openai_responses'))

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        stop = ObservedEvent()
        with LLMClient(_config(requests_per_second=100), http_client=transport, stop_event=stop) as client:

            def worker() -> None:
                """Run an actual record request with a slow coordinator acknowledgement."""
                try:
                    with record_scope(), generation_requests(reserve):
                        client.generate('system', 'blocked worker')
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=worker)
            thread.start()
            try:
                assert reserved.wait(2)
                clock[0] = 101
                with record_scope(), pytest.raises(LLMCallError):
                    client.generate('system', 'rate limited worker')
                release.set()
                assert waiting.wait(2)
                assert sent == [101]
                clock[0] = 161
                thread.join(2)
                assert not thread.is_alive() and not errors
                assert sent == [101, 161] and reservations == [True]
            finally:
                release.set()
                stop.set()
                thread.join(2)
