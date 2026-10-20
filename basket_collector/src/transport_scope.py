"""Explicit per-record retry ownership shared safely by synchronous and async adapters."""

import math
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from email.utils import parsedate_to_datetime

import httpx

# Expected external failures only; invalid local protocol/configuration and stream misuse remain fatal.
HTTP_OPERATION_ERRORS = (
    TimeoutError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.DecodingError,
)

_RECORD_OWNER: ContextVar[bool] = ContextVar('collector_record_retry_owner', default=False)
_LLM_REQUEST: ContextVar[Callable[[], None] | None] = ContextVar('collector_llm_request', default=None)


@contextmanager
def generation_requests(reserve: Callable[[], None]) -> Iterator[None]:
    """Bind a durable request counter to this worker without exposing credentials."""
    token = _LLM_REQUEST.set(reserve)
    try:
        yield
    finally:
        _LLM_REQUEST.reset(token)


def reserve_generation_request() -> None:
    """Acknowledge an actual LLM request after local validation and before sending it."""
    callback = _LLM_REQUEST.get()
    if callback is not None:
        callback()


def record_owned() -> bool:
    """Report whether the current call belongs to a durable stage attempt."""
    return _RECORD_OWNER.get()


@contextmanager
def record_scope() -> Iterator[None]:
    """Disable nested transport retries only for the current worker's record operation."""
    token = _RECORD_OWNER.set(True)
    try:
        yield
    finally:
        _RECORD_OWNER.reset(token)


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Read an uncapped server deadline without confusing it with the local backoff cap."""
    hint = headers.get('retry-after')
    if hint is None:
        return None
    try:
        delay = float(hint)
    except ValueError:
        try:
            instant = parsedate_to_datetime(hint)
            if instant.tzinfo is None:
                return None
            delay = instant.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if math.isfinite(delay) else None
