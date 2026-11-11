"""Byte-bound validators for the two public catalogue representations."""

import re

from starlette.requests import Request
from starlette.responses import Response
from viewer.util import digest, json_bytes

_CATALOGUE_PATH = re.compile(r'/api/v1/baskets(?:/[0-9a-f]{64}/runs)?')


def public_catalogue_request(method: str, path: str) -> bool:
    """Permit cache revalidation only for exact public catalogue GET routes."""
    return method == 'GET' and _CATALOGUE_PATH.fullmatch(path) is not None


def conditional_json(request: Request, value: object) -> Response:
    """Return current JSON bytes or an empty 304 for a matching GET validator."""
    body = json_bytes(value)
    if not public_catalogue_request(request.method, request.url.path):
        return Response(body, media_type='application/json', headers={'Cache-Control': 'no-store'})
    etag = '"' + digest(body) + '"'
    headers = {'Cache-Control': 'public, no-cache', 'ETag': etag}
    validators = ','.join(request.headers.getlist('if-none-match'))
    for candidate in validators.split(','):
        candidate = candidate.strip()
        if candidate == '*' or candidate.removeprefix('W/') == etag:
            return Response(status_code=304, headers=headers)
    return Response(body, media_type='application/json', headers=headers)
