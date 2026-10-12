"""Strict, bounded HTTP adapters for generated task content."""

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from basket_collector.config import GenerationConfig
from basket_collector.contracts import TaskContent, Usage
from pydantic import ValidationError


class GenerationInterrupted(Exception):
    """The coordinator stopped before another external operation could start."""


class LLMConfigurationError(Exception):
    """An API, model, endpoint or credential cannot perform this generation."""


class LLMAuthenticationError(LLMConfigurationError):
    """A missing or rejected credential can be repaired before checkpoint resume."""


class LLMCallError(Exception):
    """A per-record generation failure with retryability and attempt count."""

    def __init__(self, code: str, message: str, retryable: bool, attempts: int) -> None:
        """Record the failure code, retryability, and consumed request attempts."""
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts


@dataclass(frozen=True)
class LLMResult:
    """Validated task content and durable metadata from one LLM generation."""

    task: TaskContent
    attempts: int
    response_id: str | None
    response_model: str | None
    usage: Usage | None
    request_sha256: str


class RateLimiter:
    """One shared request start interval, including retries across workers."""

    def __init__(self, requests_per_second: int) -> None:
        """Set the minimum interval between request starts across workers."""
        self._interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self, stop_event: threading.Event) -> None:
        """Wait for a request slot, interrupting promptly on shutdown."""
        while True:
            if stop_event.is_set():
                raise GenerationInterrupted()
            with self._lock:
                now = time.monotonic()
                remaining = self._next_at - now
                if remaining <= 0:
                    self._next_at = now + self._interval
                    return
            if stop_event.wait(min(remaining, 0.1)):
                raise GenerationInterrupted()


def _retry_delay(response: httpx.Response | None, attempt: int, maximum: int) -> float:
    """Bound exponential backoff by the configured limit and Retry-After hint."""
    delay = min(float(2 ** min(attempt - 1, 16)), float(maximum))
    if response is None:
        return delay
    hint = response.headers.get('retry-after')
    if hint is None:
        return delay
    try:
        hinted = float(hint)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(hint)
            if retry_at.tzinfo is None:
                return delay
            hinted = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return delay
    if not math.isfinite(hinted):
        return delay
    return min(max(hinted, 0.0), float(maximum))


def _object(value: object) -> dict[str, Any]:
    """Require a JSON object before reading response fields."""
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    return value


def _usage(value: object, api: str) -> Usage | None:
    """Normalize token counts from the selected OpenAI API response."""
    if value is None:
        return None
    item = _object(value)
    input_name = 'input_tokens' if api == 'openai_responses' else 'prompt_tokens'
    output_name = 'output_tokens' if api == 'openai_responses' else 'completion_tokens'
    return Usage.model_validate(
        {
            'input_tokens': item.get(input_name),
            'output_tokens': item.get(output_name),
            'total_tokens': item.get('total_tokens'),
        }
    )


def _parse_response(value: object, api: str, attempts: int, request_sha256: str) -> LLMResult:
    """Validate a structured task response and retain its provenance.

    Args:
        value: Parsed response JSON.
        api: Selected completions or responses API.
        attempts: Number of HTTP attempts consumed.
        request_sha256: Hash of the serialized request payload.

    Returns:
        Validated task, response identity, usage, and request hash.

    Raises:
        LLMCallError: If the model refused, stopped early, or returned invalid output.

    """
    try:
        response = _object(value)
        response_id = response.get('id')
        response_model = response.get('model')
        if response_id is not None and not isinstance(response_id, str):
            raise ValueError('invalid response id')
        if response_model is not None and not isinstance(response_model, str):
            raise ValueError('invalid response model')
        if api == 'openai_completions':
            choices = response['choices']
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError('expected one completion choice')
            choice = _object(choices[0])
            message = _object(choice['message'])
            if message.get('refusal') is not None:
                raise LLMCallError('model_refusal', 'model refused to generate a task', False, attempts)
            if choice.get('finish_reason') != 'stop':
                raise LLMCallError('incomplete_response', 'model response did not finish', False, attempts)
            content = message['content']
            if not isinstance(content, str):
                raise ValueError('missing completion content')
        else:
            if response.get('status') != 'completed':
                raise LLMCallError('incomplete_response', 'model response did not complete', False, attempts)
            output = response['output']
            if not isinstance(output, list):
                raise ValueError('missing response output')
            parts: list[str] = []
            for item_value in output:
                item = _object(item_value)
                if item.get('type') != 'message':
                    continue
                if item.get('status', 'completed') != 'completed':
                    raise LLMCallError('incomplete_response', 'model message did not complete', False, attempts)
                pieces = item.get('content')
                if not isinstance(pieces, list):
                    raise ValueError('missing message content')
                for part_value in pieces:
                    part = _object(part_value)
                    if part.get('type') == 'refusal':
                        raise LLMCallError('model_refusal', 'model refused to generate a task', False, attempts)
                    if part.get('type') == 'output_text':
                        if not isinstance(part.get('text'), str):
                            raise ValueError('missing output text')
                        parts.append(part['text'])
            if len(parts) != 1:
                raise ValueError('expected one structured output text')
            content = parts[0]
        task = TaskContent.model_validate_json(content)
        usage = _usage(response.get('usage'), api)
        return LLMResult(task, attempts, response_id, response_model, usage, request_sha256)
    except LLMCallError:
        raise
    except (KeyError, ValueError, TypeError, ValidationError) as exc:
        raise LLMCallError('invalid_output', 'model returned invalid structured task content', False, attempts) from exc


class LLMClient:
    """Shared rate limiter with a true deadline on each complete HTTP attempt."""

    def __init__(
        self,
        config: GenerationConfig,
        *,
        http_client: httpx.Client | httpx.AsyncClient | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Configure generation and an optional mock transport for tests."""
        if config.llm is None:
            raise ValueError('LLM settings are required')
        self.config = config
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.rate_limiter = RateLimiter(config.requests_per_second)
        self._closed = False
        if http_client is None:
            self._transport: httpx.AsyncBaseTransport | None = None
        elif isinstance(http_client, (httpx.Client, httpx.AsyncClient)):
            transport = getattr(http_client, '_transport', None)
            if not isinstance(transport, httpx.MockTransport):
                raise ValueError('injected HTTP client must use MockTransport')
            self._transport = transport
        else:
            raise TypeError('http_client must be an HTTPX Client or AsyncClient')

    async def _request(self, url: str, payload: dict[str, Any], key: str) -> httpx.Response:
        """Send one bounded HTTP request and fully read its response.

        Args:
            url: Configured API endpoint.
            payload: JSON request body.
            key: API credential passed only in the request header.

        Returns:
            Complete HTTP response for status and payload validation.

        """
        transport = self._transport if self._transport is not None else httpx.AsyncHTTPTransport(retries=0)
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self.config.request_timeout_seconds),
            follow_redirects=False,
        ) as client:

            async def read_response() -> httpx.Response:
                """Consume the full response before the timeout ends."""
                if self.stop_event.is_set():
                    raise GenerationInterrupted()
                async with client.stream(
                    'POST',
                    url,
                    json=payload,
                    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                    timeout=httpx.Timeout(self.config.request_timeout_seconds),
                ) as response:
                    await response.aread()
                    return response

            return await asyncio.wait_for(read_response(), timeout=self.config.request_timeout_seconds)

    def close(self) -> None:
        """Prevent any further generation requests."""
        self._closed = True

    def __enter__(self) -> 'LLMClient':
        """Return this client for scoped generation."""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close this client when its scope ends."""
        self.close()

    def _payload(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        """Build the selected API payload with a strict task JSON schema."""
        assert self.config.llm is not None
        llm = self.config.llm
        schema = TaskContent.model_json_schema()
        parameters = llm.parameters.api_parameters(llm.api)
        if llm.api == 'openai_completions':
            return '/chat/completions', {
                'model': llm.model,
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
                'response_format': {
                    'type': 'json_schema',
                    'json_schema': {'name': 'task_content', 'strict': True, 'schema': schema},
                },
                'store': False,
                **parameters,
            }
        return '/responses', {
            'model': llm.model,
            'input': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'text': {'format': {'type': 'json_schema', 'name': 'task_content', 'strict': True, 'schema': schema}},
            'store': False,
            **parameters,
        }

    def generate(self, system: str, user: str) -> LLMResult:
        """Generate one task with shared rate limiting and bounded retries.

        Args:
            system: Rendered system prompt.
            user: Rendered user prompt.

        Returns:
            Validated task and provenance for the successful request.

        Raises:
            LLMConfigurationError: If request settings are invalid.
            LLMAuthenticationError: If credentials are missing or rejected.
            LLMCallError: If retries are exhausted or the output is invalid.
            GenerationInterrupted: If graceful shutdown is requested.

        """
        assert self.config.llm is not None
        llm = self.config.llm
        endpoint, payload = self._payload(system, user)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        request_sha256 = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
        key = os.environ.get(llm.api_key_env)
        if not key:
            raise LLMAuthenticationError(f'missing API key environment variable {llm.api_key_env}')
        attempts = 0
        while True:
            if self._closed:
                raise RuntimeError('LLM client is closed')
            self.rate_limiter.acquire(self.stop_event)
            if self.stop_event.is_set():
                raise GenerationInterrupted()
            attempts += 1
            response: httpx.Response | None = None
            try:
                response = asyncio.run(self._request(f'{llm.base_url}{endpoint}', payload, key))
            except (TimeoutError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if self.stop_event.is_set():
                    raise GenerationInterrupted() from exc
                if attempts > self.config.max_retries:
                    raise LLMCallError('request_failed', 'LLM request failed after retries', True, attempts) from exc
            else:
                if response.status_code in (408, 409, 429) or 500 <= response.status_code <= 599:
                    if self.stop_event.is_set():
                        raise GenerationInterrupted()
                    if attempts > self.config.max_retries:
                        raise LLMCallError(
                            'request_failed', f'LLM HTTP {response.status_code} after retries', True, attempts
                        )
                elif response.status_code in (401, 403):
                    raise LLMAuthenticationError(f'LLM API rejected credentials with HTTP {response.status_code}')
                elif response.status_code >= 300:
                    raise LLMConfigurationError(f'LLM API rejected request with HTTP {response.status_code}')
                else:
                    try:
                        return _parse_response(response.json(), llm.api, attempts, request_sha256)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise LLMCallError('invalid_output', 'LLM returned non-JSON response', False, attempts) from exc
            if self.stop_event.is_set():
                raise GenerationInterrupted()
            delay = _retry_delay(response, attempts, self.config.max_retry_delay_seconds)
            if self.stop_event.wait(delay):
                raise GenerationInterrupted()
