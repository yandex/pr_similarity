"""Response-only security headers without buffering request or transfer bodies."""

from starlette.datastructures import MutableHeaders


class SecurityHeaders:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        async def headers(message):
            if message['type']=='http.response.start':
                target=MutableHeaders(scope=message)
                target.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
                if scope.get('path','').startswith('/api/'):target['Cache-Control']='no-store'
            await send(message)
        await self.app(scope,receive,headers)
