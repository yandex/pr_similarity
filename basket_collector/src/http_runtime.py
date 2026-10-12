"""One owned asyncio loop and HTTP pool behind synchronous source API calls."""

import asyncio
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.cookiejar import Cookie, DefaultCookiePolicy
from typing import Any

import httpx


class _NoCookies(DefaultCookiePolicy):
    """Preserve the old per-attempt clients' absence of cookie replay."""

    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        """Reject every response cookie without touching a shared mutable jar."""
        return False


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Forward requests while leaving the supplied transport's lifetime to its owner."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        """Bind the transport that the caller has already prepared."""
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Perform one request on the owning runtime loop."""
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        """Do not close a caller-owned transport."""


@dataclass
class _Job:
    """Separate actual coroutine completion from cancellation of a proxy future."""

    result: Future[httpx.Response] = field(default_factory=Future)
    reaped: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[httpx.Response] | None = None


@dataclass
class _State:
    """Thread-owned resources without a reference back to the public runtime."""

    timeout: float
    transport: httpx.AsyncBaseTransport | None
    lock: Any = field(default_factory=threading.RLock)
    ready: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    loop: asyncio.AbstractEventLoop | None = None
    thread: threading.Thread | None = None
    client: httpx.AsyncClient | None = None
    tasks: set[asyncio.Task[httpx.Response]] = field(default_factory=set)
    closing_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    closed: bool = False
    startup_error: BaseException | None = None
    cleanup_error: BaseException | None = None


class _ClosingStream(httpx.AsyncByteStream):
    """Make the response's cleanup phase visible to the owning cancellation paths."""

    def __init__(self, stream: httpx.AsyncByteStream, state: _State) -> None:
        """Wrap the original stream without changing its bytes or ownership."""
        self.stream = stream
        self.state = state

    async def __aiter__(self):
        """Forward the original response body."""
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        """Finish a started close before exposing cancellation to the caller."""
        task = asyncio.current_task()
        assert task is not None
        self.state.closing_tasks.add(task)
        try:
            await self.stream.aclose()
        finally:
            self.state.closing_tasks.discard(task)


async def _open(state: _State) -> None:
    """Create and enter the client on its permanent loop."""
    transport = _BorrowedTransport(state.transport) if state.transport is not None else None
    state.client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    state.client.cookies.jar.set_policy(_NoCookies())
    await state.client.__aenter__()


async def _cleanup(state: _State) -> None:
    """Reap requests before closing the client and its pool."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + state.timeout + 1
    # Reserve service time to close the pool and finish the loop even when a
    # faulty transport ignores cancellation. wait_for(gather(...)) can itself
    # wait forever for such a coroutine to acknowledge cancellation.
    service_slice = min(0.1, (state.timeout + 1) / 10)
    # Include a context-manager startup task interrupted before run_forever.
    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    for task in pending:
        if not task.cancelling() and task not in state.closing_tasks:
            task.cancel()
    unfinished: set[asyncio.Task[Any]] = set()
    if pending:
        done, unfinished = await asyncio.wait(pending, timeout=max(0, deadline - loop.time() - 2 * service_slice))
        await asyncio.gather(*done, return_exceptions=True)
    if state.client is not None:
        closer = asyncio.create_task(state.client.aclose())
        done, open_pool = await asyncio.wait({closer}, timeout=max(0, deadline - loop.time() - service_slice))
        if open_pool:
            closer.cancel()
            raise RuntimeError('HTTP pool cleanup exceeded its deadline')
        closer.result()
    if unfinished:
        for task in unfinished:
            if task.done():
                if not task.cancelled():
                    task.exception()
            else:
                task.cancel()
        raise RuntimeError('HTTP requests did not finish cleanup before its deadline')


def _loop_main(state: _State) -> None:
    """Own the loop independently of the runtime object's garbage collection."""
    try:
        loop = asyncio.new_event_loop()
    except BaseException as error:
        state.startup_error = error
        state.ready.set()
        state.stopped.set()
        return
    state.loop = loop
    asyncio.set_event_loop(loop)
    try:
        try:
            loop.run_until_complete(_open(state))
        except BaseException as error:
            state.startup_error = error
        finally:
            state.ready.set()
        if state.startup_error is None and not state.closed:
            loop.run_forever()
    finally:
        try:
            loop.run_until_complete(_cleanup(state))
            loop.run_until_complete(loop.shutdown_asyncgens())
            # Python 3.12 accepts timeout; abstract-loop stubs omit this keyword.
            loop.run_until_complete(loop.shutdown_default_executor(timeout=1))  # pyright: ignore[reportCallIssue]
        except BaseException as error:
            state.cleanup_error = error
        finally:
            loop.close()
            state.stopped.set()


def _close(state: _State) -> None:
    """Close once and join the owned thread, including startup failures."""
    with state.lock:
        state.closed = True
        thread, loop = state.thread, state.loop
        if thread is None:
            return
        if thread is threading.current_thread():
            raise RuntimeError('HTTP runtime cannot close from its owning loop')
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
    thread.join(state.timeout + 1)
    if thread.is_alive():
        raise RuntimeError('HTTP runtime cleanup exceeded its deadline')
    if state.cleanup_error is not None:
        raise RuntimeError('HTTP runtime resource cleanup failed') from state.cleanup_error


async def _perform(
    state: _State,
    method: str,
    url: str,
    deadline: float,
    check_stop: Callable[[], None],
    kwargs: dict[str, Any],
) -> httpx.Response:
    """Perform and buffer exactly one attempt, including pool wait in its deadline."""
    check_stop()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('HTTP request exceeded its overall deadline')
    assert state.client is not None
    # The synchronous owner enforces the overall deadline. A second asyncio
    # timer can cancel stream cleanup already started by the first cancellation.
    async with state.client.stream(method, url, timeout=httpx.Timeout(remaining), **kwargs) as response:
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = _ClosingStream(response.stream, state)
        await response.aread()
        return response


async def _launch(state: _State, job: _Job, *args: Any) -> None:
    """Register completion before cancellation can prevent the coroutine from starting."""
    if state.closed:
        job.result.set_exception(RuntimeError('HTTP runtime is closed'))
        job.reaped.set()
        return
    task = asyncio.create_task(_perform(state, *args))
    job.task = task
    state.tasks.add(task)

    def finished(completed: asyncio.Task[httpx.Response]) -> None:
        """Expose the result only after response stream cleanup has completed."""
        state.tasks.discard(completed)
        try:
            result = completed.result()
        except asyncio.CancelledError:
            job.result.set_exception(RuntimeError('HTTP runtime request was cancelled'))
        except BaseException as error:
            job.result.set_exception(error)
        else:
            job.result.set_result(result)
        finally:
            job.reaped.set()

    task.add_done_callback(finished)
    if job.cancelled.is_set():
        task.cancel()


class HTTPRuntime:
    """Lazily own a bounded pool; hosting clients retain retries and rate limiting."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, shutdown_timeout: float = 5) -> None:
        """Configure resource ownership without opening a socket or starting a thread."""
        self._state = _State(shutdown_timeout, transport)
        self._finalizer = weakref.finalize(self, _close, self._state)

    @property
    def is_alive(self) -> bool:
        """Report whether the owned loop thread is still running."""
        thread = self._state.thread
        return thread is not None and thread.is_alive()

    def _start(self, deadline: float, check_stop: Callable[[], None]) -> asyncio.AbstractEventLoop:
        """Start once while honouring caller stop intent and the attempt deadline."""
        state = self._state
        with state.lock:
            if state.closed:
                raise RuntimeError('HTTP runtime is closed')
            if state.thread is threading.current_thread():
                raise RuntimeError('HTTP request cannot wait on its owning loop')
            if state.thread is None:
                state.thread = threading.Thread(target=_loop_main, args=(state,), name='basket-source-http')
                state.thread.start()
        while not state.ready.is_set():
            check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('HTTP client setup exceeded its overall deadline')
            state.ready.wait(min(0.1, remaining))
        if state.startup_error is not None:
            raise state.startup_error
        check_stop()
        assert state.loop is not None
        return state.loop

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout_seconds: float,
        check_stop: Callable[[], None] = lambda: None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Wait for one fully closed response or a reaped interruption."""
        deadline = time.monotonic() + timeout_seconds
        loop = self._start(deadline, check_stop)
        job = _Job()
        with self._state.lock:
            if self._state.closed:
                raise RuntimeError('HTTP runtime is closed')
            launch = asyncio.run_coroutine_threadsafe(
                _launch(self._state, job, method, url, deadline, check_stop, kwargs), loop
            )
        try:
            while not job.reaped.is_set():
                if launch.done():
                    launch.result()
                check_stop()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('HTTP request exceeded its overall deadline')
                job.reaped.wait(min(0.1, remaining))
            return job.result.result()
        except BaseException:
            completed_before_cancel = job.reaped.is_set() and job.result.exception() is None
            job.cancelled.set()

            def cancel() -> None:
                """Cancel the actual task rather than only its proxy future."""
                if (
                    job.task is not None
                    and not job.task.done()
                    and not job.task.cancelling()
                    and job.task not in self._state.closing_tasks
                ):
                    job.task.cancel()

            if not loop.is_closed():
                loop.call_soon_threadsafe(cancel)
            if not job.reaped.wait(self._state.timeout + 1):
                self.close()
                raise RuntimeError('HTTP request cancellation did not finish')
            if completed_before_cancel:
                # Preserve a complete response that won the race with stop intent.
                return job.result.result()
            raise

    def close(self) -> None:
        """Close the session on its loop and wait for owned resources to disappear."""
        try:
            _close(self._state)
        finally:
            if not self.is_alive:
                self._finalizer.detach()
