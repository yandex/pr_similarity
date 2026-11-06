"""GC cleanup must release HTTP resources without blocking interpreter locks."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize('collection_site', ['thread_start', 'http_loop'])
def test_finalizer_does_not_wait_inside_gc(collection_site: str) -> None:
    """Collect an idle runtime under thread-bootstrap locks or on its own event loop."""
    code = '''
"""Probe runtime collection while interpreter thread locks are held."""

import faulthandler
import gc
import sys
import threading
import weakref
from _thread import LockType
from collections.abc import Callable
from typing import Protocol, cast

import httpx
from basket_collector.runtime.http_runtime import HTTPRuntime


class CyclicRuntime(HTTPRuntime):
    """Keep an explicit self-cycle so the test controls garbage collection."""

    cycle: object


class ThreadingInternals(Protocol):
    """Describe the CPython shutdown-lock hooks exercised by this probe."""

    _maintain_shutdown_locks: Callable[[], None]
    _shutdown_locks_lock: LockType


private_threading = cast(ThreadingInternals, threading)
faulthandler.dump_traceback_later(5, exit=True)
gc.disable()
runtime = CyclicRuntime(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
runtime.request('GET', 'https://fixture.invalid/data', timeout_seconds=2)
state = runtime._state
assert state.loop is not None and state.worker is not None and state.thread is not None
reference = weakref.ref(runtime)
runtime.cycle = runtime
del runtime
if sys.argv[1] == 'thread_start':
    original = private_threading._maintain_shutdown_locks

    def collect_at_bootstrap() -> None:
        """Collect the runtime while the thread bootstrap owns its shutdown lock."""
        private_threading._maintain_shutdown_locks = original
        assert private_threading._shutdown_locks_lock.locked()
        gc.collect()
        original()

    private_threading._maintain_shutdown_locks = collect_at_bootstrap
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
else:
    state.loop.call_soon_threadsafe(gc.collect)
state.worker.result(timeout=2)
state.thread.join(timeout=2)
assert reference() is None
assert not state.thread.is_alive()
assert state.cleanup_error is None
faulthandler.cancel_dump_traceback_later()
'''
    result = subprocess.run(
        [sys.executable, '-c', code, collection_site],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
