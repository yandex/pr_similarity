"""Source HTTP sessions keep one loop and close every owned resource."""

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from basket_collector.config import GitHubConfig, SourceConfig
from basket_collector.providers.github import GitHubClient, GitHubError
from basket_collector.providers.gitlab import GitLabClient
from basket_collector.providers.remote import RemoteError
from basket_collector.runtime.http_runtime import HTTPRuntime


def test_one_loop_no_cookie_replay_and_borrowed_transport() -> None:
    """Reuse the loop without introducing cookie state or closing caller resources."""
    loops = []
    cookies = []

    class Transport(httpx.MockTransport):
        """Count close calls made by the transport owner."""

        closed = 0

        async def aclose(self) -> None:
            """Record explicit transport cleanup."""
            self.closed += 1

    async def handler(request: httpx.Request) -> httpx.Response:
        """Report the owning loop and offer a cookie that must not persist."""
        loops.append(asyncio.get_running_loop())
        cookies.append(request.headers.get('cookie'))
        return httpx.Response(200, json={'ok': True}, headers={'set-cookie': 'session=fixture; Path=/'})

    transport = Transport(handler)
    runtime = HTTPRuntime(transport=transport)
    assert not runtime.is_alive
    for _ in range(3):
        response = runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=2)
        assert response.is_closed and response.json() == {'ok': True}
    assert len(set(loops)) == 1 and cookies == [None, None, None]
    runtime.close()
    runtime.close()
    assert not runtime.is_alive and transport.closed == 0
    asyncio.run(transport.aclose())
    assert transport.closed == 1
    with pytest.raises(RuntimeError, match='closed'):
        runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=2)


def test_concurrent_callers_share_one_loop() -> None:
    """Source workers share a pool without sharing an event loop across threads."""
    loops = set()
    threads = set()

    async def handler(request: httpx.Request) -> httpx.Response:
        """Overlap requests inside one owning loop."""
        loops.add(asyncio.get_running_loop())
        threads.add(threading.get_ident())
        await asyncio.sleep(0.02)
        return httpx.Response(200, text=request.url.path)

    runtime = HTTPRuntime(transport=httpx.MockTransport(handler))
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda number: runtime.request('GET', f'https://fixture.invalid/{number}', timeout_seconds=2).text,
                    range(12),
                )
            )
        assert results == [f'/{number}' for number in range(12)]
        assert len(loops) == len(threads) == 1
    finally:
        runtime.close()
    assert not runtime.is_alive


def test_cancel_reaps_response_stream() -> None:
    """Return an interruption only after the async stream has actually closed."""
    entered = threading.Event()
    closed = threading.Event()
    stop = threading.Event()

    class Stream(httpx.AsyncByteStream):
        """Hold the body until runtime cancellation."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            """Wait without completing the HTTP body."""
            entered.set()
            await asyncio.sleep(60)
            yield b'never'

        async def aclose(self) -> None:
            """Confirm that cleanup precedes returning to the caller."""
            closed.set()

    class Interrupted(Exception):
        """A synthetic provider-specific interruption."""

    def check_stop() -> None:
        """Use the same callback boundary as a hosting client."""
        if stop.is_set():
            raise Interrupted()

    runtime = HTTPRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                runtime.request, 'GET', 'https://fixture.invalid/slow', timeout_seconds=2, check_stop=check_stop
            )
            assert entered.wait(2)
            stop.set()
            with pytest.raises(Interrupted):
                future.result(timeout=2)
            assert closed.is_set()
    finally:
        runtime.close()
    assert not runtime.is_alive


def test_deadline_includes_response_body() -> None:
    """A buffered response still obeys the configured overall deadline."""

    async def handler(request: httpx.Request) -> httpx.Response:
        """Delay past the short fixture deadline."""
        await asyncio.sleep(10)
        return httpx.Response(200)

    runtime = HTTPRuntime(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TimeoutError):
            runtime.request('GET', 'https://fixture.invalid/slow', timeout_seconds=0.1)
    finally:
        runtime.close()
    assert not runtime.is_alive


def test_owner_loop_call_is_rejected() -> None:
    """Reject waiting synchronously on the owning loop instead of deadlocking."""
    runtime = None

    async def handler(request: httpx.Request) -> httpx.Response:
        """Attempt a recursive synchronous call from the HTTP loop."""
        assert runtime is not None
        with pytest.raises(RuntimeError, match='owning loop'):
            runtime.request('GET', 'https://fixture.invalid/nested', timeout_seconds=1)
        return httpx.Response(200)

    runtime = HTTPRuntime(transport=httpx.MockTransport(handler))
    try:
        assert runtime.request('GET', 'https://fixture.invalid/outer', timeout_seconds=2).status_code == 200
    finally:
        runtime.close()


def test_loop_startup_failure_is_reported_without_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed loop constructor wakes the caller and leaves no owned thread."""

    def broken_loop() -> asyncio.AbstractEventLoop:
        """Simulate a sandbox rejecting the event loop's socket pair."""
        raise OSError('fixture loop setup failed')

    monkeypatch.setattr(asyncio, 'new_event_loop', broken_loop)
    runtime = HTTPRuntime()
    try:
        with pytest.raises(OSError, match='fixture loop setup'):
            runtime.request('GET', 'https://fixture.invalid/', timeout_seconds=0.1)
    finally:
        runtime.close()
    assert not runtime.is_alive


def test_close_during_async_client_startup_reaps_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unfinished client context is cancelled and reaped before loop closure."""
    entered = threading.Event()
    reaped = threading.Event()
    original = httpx.AsyncClient.__aenter__

    async def slow_enter(client: httpx.AsyncClient) -> httpx.AsyncClient:
        """Keep startup pending after entering the real client context."""
        result = await original(client)
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            reaped.set()
        return result

    monkeypatch.setattr(httpx.AsyncClient, '__aenter__', slow_enter)
    runtime = HTTPRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.request, 'GET', 'https://fixture.invalid/', timeout_seconds=2)
        assert entered.wait(2)
        runtime.close()
        with pytest.raises(RuntimeError):
            future.result(timeout=2)
    assert reaped.is_set() and not runtime.is_alive


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
@pytest.mark.parametrize('pending_body', [True, False])
def test_slow_stream_cleanup_preserves_provider_timeout(provider: str, pending_body: bool) -> None:
    """Cleanup may use the configured budget without losing typed retryable errors."""
    closed = threading.Event()

    class SlowClose(httpx.AsyncByteStream):
        """Delay cleanup beyond the old hardcoded one-second reap wait."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            """Keep the body pending until the overall request deadline."""
            if pending_body:
                await asyncio.sleep(60)
            yield b'{}'

        async def aclose(self) -> None:
            """Complete within request_timeout + one second of cleanup budget."""
            await asyncio.sleep(1.25)
            closed.set()

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowClose()))
    if provider == 'github':
        client = GitHubClient(
            GitHubConfig(request_timeout_seconds=1, max_retries=0),
            transport=transport,
            token='synthetic-fixture-only',
        )
        try:
            with pytest.raises(GitHubError) as caught:
                client._request('GET', '/fixture')
            assert caught.value.code == 'github_timeout' and caught.value.retryable
            assert closed.is_set()
        finally:
            client.close()
    else:
        client = GitLabClient(
            SourceConfig(provider='gitlab', project='fixture/project', request_timeout_seconds=1, max_retries=0),
            transport=transport,
            token='synthetic-fixture-only',
        )
        try:
            with pytest.raises(RemoteError) as caught:
                client.request('projects/1')
            assert caught.value.code == 'source_transport_error' and caught.value.retryable
            assert closed.is_set()
        finally:
            client.close()


@pytest.mark.parametrize('cancel_body', [True, False])
def test_close_waits_for_inflight_stream_cleanup(cancel_body: bool) -> None:
    """Concurrent owner close neither recancels nor truncates response cleanup."""
    entered = threading.Event()
    closing = threading.Event()
    closed = threading.Event()
    stop = threading.Event()

    class Stream(httpx.AsyncByteStream):
        """Exercise both normal and cancellation-driven slow close."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            """Optionally keep the body pending until cancellation."""
            entered.set()
            if cancel_body:
                await asyncio.sleep(60)
            yield b'{}'

        async def aclose(self) -> None:
            """Spend more than the configured timeout inside its cleanup budget."""
            closing.set()
            await asyncio.sleep(1.25)
            closed.set()

    def check_stop() -> None:
        """Use a typed interruption independently of owner shutdown."""
        if stop.is_set():
            raise InterruptedError('fixture stop')

    runtime = HTTPRuntime(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())), shutdown_timeout=1
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                runtime.request, 'GET', 'https://fixture.invalid/', timeout_seconds=5, check_stop=check_stop
            )
            assert entered.wait(2)
            if cancel_body:
                stop.set()
            assert closing.wait(2)
            runtime.close()
            assert closed.is_set() and not runtime.is_alive
            if cancel_body:
                with pytest.raises(InterruptedError, match='fixture stop'):
                    future.result(timeout=1)
            else:
                assert future.result(timeout=1).json() == {}
    finally:
        runtime.close()


def test_cleanup_budget_closes_pool_and_loop_when_a_task_ignores_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancellation-resistant transport task cannot keep the owned loop alive."""
    tasks = []
    release = None
    pool_closed = threading.Event()
    original_close = httpx.AsyncClient.aclose

    async def stubborn() -> None:
        """Ignore cancellation until the independent pool close releases resources."""
        assert release is not None
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        """Simulate a transport that owns an extra coroutine on the runtime loop."""
        nonlocal release
        release = asyncio.Event()
        tasks.append(asyncio.create_task(stubborn()))
        await asyncio.sleep(0)
        return httpx.Response(200)

    async def close_pool(client: httpx.AsyncClient) -> None:
        """Verify the pool still closes independently of a stalled request reap."""
        await original_close(client)
        pool_closed.set()
        assert release is not None
        release.set()

    monkeypatch.setattr(httpx.AsyncClient, 'aclose', close_pool)
    runtime = HTTPRuntime(transport=httpx.MockTransport(handler), shutdown_timeout=1)
    assert runtime.request('GET', 'https://fixture.invalid/', timeout_seconds=2).status_code == 200
    with pytest.raises(RuntimeError, match='resource cleanup failed'):
        runtime.close()
    assert pool_closed.is_set() and not runtime.is_alive
    assert tasks[0].done()


def test_keepalive_reuses_real_tcp_connection() -> None:
    """A loopback HTTP/1.1 server observes one connection for three requests."""
    connections = []

    class Handler(BaseHTTPRequestHandler):
        """Serve complete small responses over a persistent connection."""

        protocol_version = 'HTTP/1.1'

        def setup(self) -> None:
            """Count accepted connections rather than synthetic HTTP calls."""
            super().setup()
            connections.append(self.client_address)

        def log_message(self, format: str, *args: object) -> None:
            """Keep fixture requests out of terminal output."""

        def do_GET(self) -> None:
            """Return a framed body so HTTPX can reuse the socket."""
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    runtime = HTTPRuntime()
    try:
        for _ in range(3):
            response = runtime.request('GET', f'http://127.0.0.1:{server.server_port}/', timeout_seconds=2)
            assert response.text == 'OK'
        assert len(connections) == 1
    finally:
        runtime.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert not runtime.is_alive and not thread.is_alive()


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
@pytest.mark.parametrize('failure', ['gzip', 'proxy', 'local_protocol', 'unsupported_protocol', 'programming'])
def test_provider_normalizes_only_expected_external_failures(provider: str, failure: str) -> None:
    """Keep corrupt responses retryable while local request defects still escape the adapter."""
    errors = {
        'proxy': httpx.ProxyError,
        'local_protocol': httpx.LocalProtocolError,
        'unsupported_protocol': httpx.UnsupportedProtocol,
        'programming': RuntimeError,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Exercise actual response decoding or an explicit HTTP transport failure."""
        if failure == 'gzip':
            return httpx.Response(200, headers={'Content-Encoding': 'gzip'}, content=b'broken gzip')
        raise errors[failure]('fixture failure')

    transport = httpx.MockTransport(handler)
    expected = RemoteError if failure in ('gzip', 'proxy') else errors[failure]
    if provider == 'github':
        with GitHubClient(GitHubConfig(max_retries=0), transport=transport) as client:
            with pytest.raises(expected):
                client.get_pull_request('o/r', 1)
    else:
        client = GitLabClient(
            SourceConfig(provider='gitlab', project='o/r', max_retries=0), transport=transport, token='fixture'
        )
        try:
            with pytest.raises(expected):
                client.request('projects/1')
        finally:
            client.close()
