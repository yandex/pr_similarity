"""Hold local run leases through response construction and streamed delivery."""

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager
from functools import wraps
from typing import Protocol

import anyio
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import ContentStream, Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send


class ReadLease(Protocol):
    """A version pin released once the HTTP read completes."""

    def close(self, success: bool = True) -> None:
        """Release the pin and record successful user access when applicable."""
        ...


class ReadService[L: ReadLease](Protocol):
    """Local version selection and request-context binding."""

    async def acquire_read_async(self, run_id: str, /) -> L:
        """Await a readable version without retaining an HTTP worker thread."""
        ...

    def bind_read(self, lease: L) -> AbstractContextManager[None]:
        """Bind the selected immutable version to the current request task."""
        ...


async def close_lease(lease: ReadLease, success: bool) -> None:
    """Release pins even when the surrounding request has been cancelled."""
    with anyio.CancelScope(shield=True):
        await run_in_threadpool(lease.close, success)


class LeaseResponse[L: ReadLease](Response):
    """Delegate delivery while keeping the selected local version pinned."""

    def __init__(self, response: Response, service: ReadService[L], lease: L) -> None:
        """Retain the existing response without buffering or copying its body."""
        self.response, self.service, self.lease = response, service, lease
        self.status_code = response.status_code
        self.raw_headers = response.raw_headers
        self.background = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Close the lease after final delivery, failure or client disconnect."""
        completed = False

        async def track(message: Message) -> None:
            """Mark success only after the transport accepts the final body message."""
            nonlocal completed
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                completed = True

        try:
            with self.service.bind_read(self.lease):
                await self.response(scope, receive, track)
        finally:
            await close_lease(self.lease, completed and self.status_code < 400)


def local_read[L: ReadLease](
    service: ReadService[L],
    handler: Callable[[Request], Awaitable[Response]],
    before: Callable[[Request], Awaitable[None]] | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """Acquire a version before a run-specific handler enters its short lock."""

    @wraps(handler)
    async def read(request: Request) -> Response:
        """Bind the version during construction and transfer ownership to the response."""
        if before is not None:
            await before(request)
        lease = await service.acquire_read_async(request.path_params['run_id'])
        transferred = False
        try:
            with service.bind_read(lease):
                response = await handler(request)
            delivery = LeaseResponse(response, service, lease)
            transferred = True
            return delivery
        finally:
            if not transferred:
                await close_lease(lease, False)

    return read


class ClosingStreamResponse(StreamingResponse):
    """Close an admitted archive body even if send fails before iteration starts."""

    def __init__(
        self,
        content: ContentStream,
        close: Callable[[], None],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        """Keep the stream closer separate from lazy body iteration."""
        super().__init__(
            content, status_code=status_code, headers=headers, media_type=media_type, background=background
        )
        self.close_stream = close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Release the source on success, cancellation and transport errors."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.close_stream)
