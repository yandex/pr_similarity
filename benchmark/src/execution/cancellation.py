"""Cancellation-aware await boundaries for startup network operations."""

import asyncio
import threading
from collections.abc import Awaitable
from contextlib import suppress

from benchmark.errors import BenchmarkError


def check_cancelled(cancel: threading.Event | None) -> None:
    """Reject cancelled preparation without reclassifying it as an infrastructure failure."""
    if cancel is not None and cancel.is_set():
        raise BenchmarkError(
            'operation_cancelled', 'Preparation was interrupted; use RESUME to continue.', exit_code=130
        )


async def cancellable[T](operation: Awaitable[T], cancel: threading.Event | None) -> T:
    """Await startup I/O while propagating cancellation into the owned asynchronous task.

    Args:
        operation: Download or other startup request owned by this invocation.
        cancel: Optional signal intent, polled without blocking the event loop.

    Returns:
        The operation's result if it completes before cancellation.

    Raises:
        BenchmarkError: If cancellation is requested before completion.

    """
    if cancel is None:
        return await operation
    task = asyncio.ensure_future(operation)
    try:
        while True:
            check_cancelled(cancel)
            done, _pending = await asyncio.wait([task], timeout=0.05)
            if done:
                check_cancelled(cancel)
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        elif not task.cancelled():
            # Retrieve a raced failure even when cancellation wins over its completed result.
            task.exception()
