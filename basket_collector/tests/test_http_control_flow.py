"""Preserve process control when HTTP completion races with caller interruption."""

import asyncio
import gc
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context

import httpx
import pytest
from basket_collector.providers.remote import RemoteInterrupted
from basket_collector.runtime import http_runtime


@pytest.mark.parametrize(
    ('interruption', 'completed_response'),
    [
        (KeyboardInterrupt(), False),
        (SystemExit(7), False),
        (ValueError('unexpected callback failure'), False),
        (InterruptedError('stopped'), True),
        (RemoteInterrupted('stopped'), True),
        (TimeoutError('deadline'), True),
    ],
)
def test_completed_response_only_wins_expected_stop_races(
    monkeypatch: pytest.MonkeyPatch, interruption: BaseException, completed_response: bool
) -> None:
    """Preserve completed responses only for supported stop signals, never unrelated failures."""
    finish_allowed = threading.Event()
    jobs: list[http_runtime._Job] = []
    original_job = http_runtime._Job

    def create_job() -> http_runtime._Job:
        """Expose the real job's completion barrier to the caller-side race fixture."""
        job = original_job()
        jobs.append(job)
        return job

    async def handler(request: httpx.Request) -> httpx.Response:
        """Finish the real transport only after the caller starts its stop check."""
        while not finish_allowed.is_set():
            await asyncio.sleep(0.001)
        return httpx.Response(200)

    def check_stop() -> None:
        """Deliver process control after response cleanup and result delivery finish."""
        if threading.current_thread() is threading.main_thread() and jobs:
            finish_allowed.set()
            assert jobs[0].reaped.wait(2)
            raise interruption

    monkeypatch.setattr(http_runtime, '_Job', create_job)
    runtime = http_runtime.HTTPRuntime(transport=httpx.MockTransport(handler))
    try:
        if completed_response:
            response = runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=3, check_stop=check_stop)
            assert response.status_code == 200
        else:
            with pytest.raises(type(interruption)) as caught:
                runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=3, check_stop=check_stop)
            assert caught.value is interruption
    finally:
        finish_allowed.set()
        runtime.close()
    assert not runtime.is_alive


class ProcessControl(BaseException):
    """Represent a caller-defined process-control failure outside Exception."""


@pytest.mark.parametrize('failure_type', [KeyboardInterrupt, SystemExit, ProcessControl])
def test_http_startup_delivers_original_control_failure(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[BaseException]
) -> None:
    """A loop-startup failure reaches the caller unchanged and the loop is reaped."""
    failure = failure_type('fixture startup failed')

    async def fail_startup(state: http_runtime._State) -> None:
        """Raise the control failure in the HTTP runtime's owned thread."""
        raise failure

    monkeypatch.setattr(http_runtime, '_open', fail_startup)
    runtime = http_runtime.HTTPRuntime()
    try:
        with pytest.raises(failure_type) as caught:
            runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=3)
        assert caught.value is failure
    finally:
        runtime.close()
    assert not runtime.is_alive


def test_runtime_finalizer_reclaims_executor_thread() -> None:
    """Dropping an idle runtime eventually closes its loop and releases the owned worker."""
    runtime = http_runtime.HTTPRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=3)
    thread = runtime._state.thread
    reference = weakref.ref(runtime)
    del runtime
    gc.collect()
    assert reference() is None
    assert thread is not None
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_cleanup_failure_preserves_cause_and_reclaims_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The joining caller receives a cleanup failure after the owned thread exits."""
    failure = ProcessControl('fixture cleanup failed')

    async def fail_cleanup(state: http_runtime._State) -> None:
        """Close the actual HTTP client before simulating a teardown failure."""
        assert state.client is not None
        await state.client.aclose()
        raise failure

    monkeypatch.setattr(http_runtime, '_cleanup', fail_cleanup)
    runtime = http_runtime.HTTPRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=3)
    with pytest.raises(RuntimeError, match='resource cleanup failed') as caught:
        runtime.close()
    assert caught.value.__cause__ is failure
    assert not runtime.is_alive


def test_failed_startup_terminates_worker_before_explicit_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed runtime startup leaves no idle pool thread waiting for explicit close."""

    async def fail_startup(state: http_runtime._State) -> None:
        """Fail before the HTTP client acquires any resources."""
        raise RuntimeError('fixture startup failure')

    monkeypatch.setattr(http_runtime, '_open', fail_startup)
    runtime = http_runtime.HTTPRuntime()
    try:
        with pytest.raises(RuntimeError, match='fixture startup failure'):
            runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=3)
        thread = runtime._state.thread
        assert thread is not None
        thread.join(2)
        assert not runtime.is_alive
    finally:
        runtime.close()


@pytest.mark.parametrize('failure', [ValueError('callback failed'), KeyboardInterrupt('interrupted')])
def test_concurrent_owner_close_preserves_request_failure(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """Closing the loop during cancellation scheduling cannot replace the caller's failure."""
    entered = threading.Event()
    closing = threading.Event()
    closed_body = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """Keep the second real request pending until owner shutdown reaps it."""
        if request.url.path == '/slow':
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                closed_body.set()
        return httpx.Response(200)

    runtime = http_runtime.HTTPRuntime(transport=httpx.MockTransport(handler))
    runtime.request('GET', 'https://fixture.invalid/ready', timeout_seconds=3)
    loop = runtime._state.loop
    assert loop is not None
    original_schedule = loop.call_soon_threadsafe
    calls = 0

    def check_stop() -> None:
        """Fail the caller after the request starts on the owned loop."""
        nonlocal calls
        if threading.current_thread() is threading.main_thread():
            calls += 1
            if calls > 1:
                assert entered.wait(2)
                raise failure

    def close_owner() -> None:
        """Complete actual shutdown inside the cancellation scheduler's race window."""
        assert closing.wait(3)
        runtime.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(close_owner)

        def schedule(callback: Callable[..., object], *args: object, context: Context | None = None) -> asyncio.Handle:
            """Let close finish after is_closed was checked but before scheduling cancel."""
            if getattr(callback, '__name__', '') == 'cancel':
                closing.set()
                owner.result(timeout=3)
            return original_schedule(callback, *args, context=context)

        monkeypatch.setattr(loop, 'call_soon_threadsafe', schedule)
        try:
            with pytest.raises(type(failure)) as caught:
                runtime.request('GET', 'https://fixture.invalid/slow', timeout_seconds=3, check_stop=check_stop)
            assert caught.value is failure
            assert closed_body.is_set()
        finally:
            closing.set()
            owner.result(timeout=3)
            runtime.close()
    assert not runtime.is_alive


def test_close_tolerates_loop_finishing_before_stop_is_scheduled(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner close remains idempotent when the loop finishes during stop scheduling."""
    runtime = http_runtime.HTTPRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    runtime.request('GET', 'https://fixture.invalid/ready', timeout_seconds=3)
    loop = runtime._state.loop
    assert loop is not None
    original_schedule = loop.call_soon_threadsafe

    def schedule(callback: Callable[..., object], *args: object, context: Context | None = None) -> asyncio.Handle:
        """Finish the real loop after the caller checked its open state."""
        if callback == loop.stop:
            original_schedule(loop.stop)
            assert runtime._state.stopped.wait(3)
        return original_schedule(callback, *args, context=context)

    monkeypatch.setattr(loop, 'call_soon_threadsafe', schedule)
    try:
        runtime.close()
        runtime.close()
        assert not runtime.is_alive
    finally:
        runtime.close()


@pytest.mark.parametrize('operation', ['cancel', 'close'])
def test_open_loop_scheduler_failures_remain_visible(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Unexpected scheduler failures are not mistaken for concurrent loop closure."""
    runtime = http_runtime.HTTPRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    runtime.request('GET', 'https://fixture.invalid/ready', timeout_seconds=3)
    loop = runtime._state.loop
    assert loop is not None
    failure = RuntimeError('fixture scheduler failure')

    def fail_schedule(callback: Callable[..., object], *args: object, context: Context | None = None) -> asyncio.Handle:
        """Fail scheduling without closing the owned event loop."""
        raise failure

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(loop, 'call_soon_threadsafe', fail_schedule)
            with pytest.raises(RuntimeError) as caught:
                if operation == 'cancel':
                    runtime._cancel_job(http_runtime._Job(), loop)
                else:
                    runtime.close()
            assert caught.value is failure
            assert not loop.is_closed()
    finally:
        runtime.close()
    assert not runtime.is_alive
