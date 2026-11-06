"""Response-only security headers without buffering request or transfer bodies."""

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from viewer.service import ViewerService


class SecurityHeaders:
    """Add browser security headers to outgoing HTTP responses."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream ASGI application to add response headers."""
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


class StartupGate:
    """Refuse unchecked catalogue requests while keeping liveness and readiness visible."""

    def __init__(self, app: ASGIApp, service: ViewerService) -> None:
        """Bind the downstream ASGI app and service startup state."""
        self.app, self.service = app, service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Keep unchecked result APIs unavailable until startup succeeds."""
        state = self.service.startup
        path = scope.get('path', '')
        retry = path == '/api/v1/admin/refresh' and state['status'] == 'failed'
        if scope['type'] == 'http' and path.startswith('/api/') and state['status'] != 'ready' and not retry:
            response = JSONResponse(
                {
                    'error': {
                        'code': 'service_starting',
                        'message': 'Service is preparing its catalogue; retry shortly.',
                    }
                },
                status_code=503,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
