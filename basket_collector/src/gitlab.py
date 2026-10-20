"""Bounded GitLab REST API reads with explicit access and pagination failures."""

import email.utils
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
from basket_collector.config import SourceConfig
from basket_collector.http_runtime import HTTPRuntime
from basket_collector.remote import RemoteError, RemoteInterrupted
from basket_collector.transport_scope import HTTP_OPERATION_ERRORS, record_owned, retry_after_seconds


class GitLabClient:
    """Own read-only request limits and token scope for one configured instance."""

    def __init__(
        self,
        config: SourceConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Bind credentials in memory while leaving Git transport auth to the operator."""
        self.config = config
        self.api_url = config.server_url + '/api/v4/'
        self.transport = transport
        self.stop = stop_event or threading.Event()
        self.closed = False
        self.wait_callback: Callable[[str], None] | None = None
        self._runtime = HTTPRuntime(transport=transport, shutdown_timeout=config.request_timeout_seconds)
        self.token = token if token is not None else os.environ.get(config.token_env)
        self.rate_lock = threading.Lock()
        self.next_request = 0.0
        self.cooldown_until = 0.0

    def close(self) -> None:
        """Prevent new calls without touching operator credentials or the shared stop event."""
        self.closed = True
        self._runtime.close()

    def _check_stop(self) -> None:
        """Stop before another API operation when the collector is draining."""
        if self.closed or self.stop.is_set():
            raise RemoteInterrupted('GitLab request interrupted.', code='source_interrupted', retryable=True)

    def _wait(self, seconds: float) -> None:
        """Wait cooperatively for a bounded rate/retry delay."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_stop()
            self.stop.wait(min(0.1, deadline - time.monotonic()))

    def _throttle(self) -> None:
        """Serialize reservation of the next request slot across all stage workers."""
        self._check_stop()
        self._notify('rate limit')
        with self.rate_lock:
            now = time.monotonic()
            reserved = max(now, self.next_request)
            self.next_request = reserved + 1 / self.config.requests_per_second
        while True:
            self._check_stop()
            with self.rate_lock:
                remaining = max(reserved, self.cooldown_until) - time.monotonic()
            if remaining > self.config.max_retry_delay_seconds and not record_owned():
                raise RemoteError(
                    'GitLab cooldown exceeds the configured retry delay; resume later.',
                    code='source_rate_limited',
                    retryable=True,
                )
            if remaining <= 0:
                return
            self._wait(min(0.1, remaining))

    def _notify(self, reason: str) -> None:
        """Submit only ephemeral wait state to an optional observer."""
        try:
            if self.wait_callback is not None:
                self.wait_callback(reason)
        except Exception:
            pass

    def _retry_wait(self, seconds: float) -> None:
        """Display retry waiting without changing delay or cancellation."""
        self._notify('retry backoff')
        try:
            self._wait(seconds)
        finally:
            self._notify('HTTP request')

    def request(self, path: str, params: dict[str, str | int] | None = None) -> tuple[object, httpx.Headers]:
        """Read through a single source session and clear the worker's wait state."""
        try:
            return self._request(path, params)
        finally:
            self._notify('')

    def _check_request(self, path: str) -> None:
        """Validate shared credentials and resource ownership before any request attempt."""
        self._check_stop()
        if not self.token or not self.token.strip():
            raise RemoteError(
                'GitLab collection requires '
                + self.config.token_env
                + '; configure it in the environment or .env with API read permissions, then resume.',
                code='source_auth_error',
                retryable=True,
            )
        if path.startswith('/') or '://' in path or any(part in ('.', '..') for part in path.split('/')):
            raise ValueError('GitLab API path must be a relative resource path')

    def _request(self, path: str, params: dict[str, str | int] | None = None) -> tuple[object, httpx.Headers]:
        """Use the stage retry owner for records and source request policy for discovery."""
        self._check_request(path)
        max_retries = 0 if record_owned() else self.config.max_retries
        for attempt in range(max_retries + 1):
            self._throttle()
            try:
                self._notify('HTTP request')
                response = self._runtime.request(
                    'GET',
                    self.api_url + path,
                    params=params or {},
                    headers={'Accept': 'application/json', 'PRIVATE-TOKEN': self.token},
                    timeout_seconds=self.config.request_timeout_seconds,
                    check_stop=self._check_stop,
                )
            except HTTP_OPERATION_ERRORS as exc:
                if attempt == max_retries:
                    raise RemoteError(
                        'Cannot reach GitLab API; check instance URL, TLS and network.',
                        code='source_transport_error',
                        retryable=True,
                    ) from exc
                self._retry_wait(min(2**attempt, self.config.max_retry_delay_seconds))
                continue
            status = response.status_code
            logging.getLogger('basket_collector.diagnostics').debug('GitLab API %s status=%s', path, status)
            if status == 401:
                raise RemoteError(
                    'GitLab authentication failed; check '
                    + self.config.token_env
                    + ' expiry and API read permissions.',
                    code='source_auth_error',
                    retryable=True,
                )
            limited = status == 429 or (status == 403 and response.headers.get('ratelimit-remaining') == '0')
            if status == 403 and not limited:
                raise RemoteError(
                    'GitLab denied API access; check project and read_api permissions.',
                    code='source_access_denied',
                    retryable=True,
                )
            if status == 404:
                raise RemoteError(
                    'GitLab resource is missing or hidden; check project locator and token permissions.',
                    code='source_unavailable_or_forbidden',
                    retryable=True,
                )
            if limited or status >= 500:
                delay = min(2**attempt, self.config.max_retry_delay_seconds)
                header = response.headers.get('retry-after')
                if header is not None:
                    try:
                        supplied = float(header)
                        if math.isfinite(supplied) and supplied >= 0:
                            delay = max(delay, supplied)
                    except ValueError:
                        try:
                            instant = email.utils.parsedate_to_datetime(header)
                            delay = max(delay, instant.timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            pass
                with self.rate_lock:
                    self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
                if delay > self.config.max_retry_delay_seconds:
                    raise RemoteError(
                        'GitLab requested a longer cooldown; resume after Retry-After.',
                        code='source_rate_limited',
                        retryable=True,
                        retry_after=delay,
                    )
                if attempt == max_retries:
                    raise RemoteError(
                        'GitLab rate limit or server failure; retry later with the saved checkpoint.',
                        code='source_rate_limited' if limited else 'source_server_error',
                        retryable=True,
                        retry_after=delay,
                    )
                self._retry_wait(delay)
                continue
            if status != 200:
                raise RemoteError(
                    'GitLab API operation failed; check server API capabilities and permissions.',
                    code='source_bad_request' if status == 400 else 'source_api_error',
                    retryable=False,
                    retry_after=retry_after_seconds(response.headers),
                )
            try:
                return response.json(), response.headers
            except (ValueError, UnicodeError) as exc:
                raise RemoteError(
                    'GitLab returned invalid JSON; check the API endpoint.', code='invalid_source_metadata'
                ) from exc
        raise AssertionError('request retry loop exhausted without an outcome')

    def _next_page(self, path: str, page: int, headers: httpx.Headers, count: int) -> int | None:
        """Validate provider cursors without following arbitrary pagination URLs."""
        advertised = headers.get('x-next-page')
        if advertised is None and headers.get('link'):
            links = httpx.Response(200, headers=headers).links
            next_link = links.get('next', {}).get('url')
            if next_link is not None:
                target = urlsplit(next_link)
                expected = urlsplit(self.api_url + path)
                if target.scheme != expected.scheme or target.netloc != expected.netloc or target.path != expected.path:
                    raise RemoteError(
                        'GitLab pagination URL left the configured API resource.', code='invalid_pagination'
                    )
                query = parse_qs(target.query)
                advertised = query.get('page', [''])[0]
            else:
                return None
        if advertised is None:
            return page + 1 if count == 100 else None
        if advertised == '':
            return None
        if not advertised.isascii() or not advertised.isdecimal() or int(advertised) != page + 1:
            raise RemoteError('GitLab returned an invalid or repeated pagination cursor.', code='invalid_pagination')
        return int(advertised)

    def pages(
        self, path: str, params: dict[str, str | int] | None = None, *, start_page: int = 1
    ) -> Iterator[tuple[int, list[dict]]]:
        """Exhaust each list response and reject truncated or malformed page data."""
        page = start_page
        if page < 1:
            raise ValueError('start_page must be positive')
        while True:
            if page > self.config.max_pages:
                raise RemoteError(
                    'GitLab pagination exceeded source.max_pages; narrow the selection or increase the limit '
                    'and rebuild with OVERWRITE=1.',
                    code='source_pagination_budget',
                    retryable=True,
                )
            data, headers = self.request(path, {**(params or {}), 'page': page, 'per_page': 100})
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise RemoteError('GitLab returned an incomplete list response.', code='invalid_source_metadata')
            yield page, data
            next_page = self._next_page(path, page, headers, len(data))
            if next_page is None:
                return
            page = next_page
