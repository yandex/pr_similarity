"""Response-only security headers without buffering request or transfer bodies."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeaders:
    """Add browser security headers to outgoing HTTP responses."""

    def __init__(self, app: ASGIApp) -> None:
        """Initialize the resources and limits owned by this instance."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward the ASGI request while adding security headers."""

        async def headers(message: Message) -> None:
            """Add response security headers without changing the ASGI message flow."""
            if message['type'] == 'http.response.start':
                target = MutableHeaders(scope=message)
                target.update(
                    {
                        'X-Content-Type-Options': 'nosniff',
                        'Referrer-Policy': 'same-origin',
                        'Content-Security-Policy': (
                            'default-src \'self\'; script-src \'self\'; style-src \'self\'; object-s'
                            'rc \'none\'; base-uri \'none\'; frame-ancestors \'none\''
                        ),
                    }
                )
                if scope.get('path', '').startswith('/api/'):
                    target['Cache-Control'] = 'no-store'
            await send(message)

        await self.app(scope, receive, headers)
