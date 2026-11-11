"""Conditional public catalogue reads and private response cache boundaries."""

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient
from test_api import admin, create_user, upload
from test_api import client as client
from viewer.util import digest, json_bytes
from viewer.web.cache import conditional_json
from viewer.web.middleware import SecurityHeaders

# Re-export the public fixture for pytest collection in this module.
__all__ = ['client']


@pytest.mark.parametrize('path', ['/api/v1/baskets', '/api/v1/baskets/' + 'a' * 64 + '/runs'])
def test_catalogue_etag_revalidates_exact_response(path: str) -> None:
    """The validator follows current response bytes, including query projections."""
    state = {'revision': 1}

    async def catalogue(request: Request) -> Response:
        """Return the current fixture projection through conditional JSON handling."""
        return conditional_json(request, {**state, 'filter': request.query_params.get('q')})

    app = Starlette(routes=[Route(path, catalogue)], middleware=[Middleware(SecurityHeaders)])
    with TestClient(app) as client:
        first = client.get(path)
        assert first.content == json_bytes({'revision': 1, 'filter': None})
        etag = '"' + digest(first.content) + '"'
        assert first.headers['etag'] == etag
        assert first.headers['cache-control'] == 'public, no-cache'
        for validator in [etag, 'W/' + etag, '"other", W/' + etag, '*']:
            unchanged = client.get(path, headers={'If-None-Match': validator})
            assert unchanged.status_code == 304
            assert unchanged.content == b''
            assert unchanged.headers['etag'] == etag
            assert unchanged.headers['cache-control'] == 'public, no-cache'
        filtered = client.get(path + '?q=changed', headers={'If-None-Match': etag})
        assert filtered.status_code == 200
        assert filtered.headers['etag'] != etag
        state['revision'] = 2
        changed = client.get(path, headers={'If-None-Match': etag})
        assert changed.status_code == 200
        assert changed.headers['etag'] != etag
        head = client.head(path, headers={'If-None-Match': '*'})
        assert head.status_code == 200
        assert head.headers['cache-control'] == 'no-store'
        assert 'etag' not in head.headers


@pytest.mark.parametrize(
    'path',
    [
        '/api/v1/auth/me',
        '/api/v1/admin/stats',
        '/api/v1/runs/run/archive',
        '/api/v1/baskets/not-a-hash/runs',
        '/api/v1/baskets/' + 'A' * 64 + '/runs',
    ],
)
def test_other_routes_cannot_opt_into_public_cache(path: str) -> None:
    """Sensitive routes stay no-store even if a handler sets public cache headers."""

    async def unsafe(request: Request) -> Response:
        """Attach public cache headers to test the middleware privacy boundary."""
        return Response(b'private', headers={'Cache-Control': 'public, no-cache', 'ETag': '"private"'})

    app = Starlette(routes=[Route(path, unsafe)], middleware=[Middleware(SecurityHeaders)])
    with TestClient(app) as client:
        response = client.get(path)
        assert response.headers['cache-control'] == 'no-store'
        assert 'etag' not in response.headers


@pytest.mark.parametrize('status', [400, 401, 404, 409, 500, 503])
def test_catalogue_errors_are_never_cacheable(status: int) -> None:
    """Errors cannot reuse the success response caching policy."""

    async def error(request: Request) -> Response:
        """Attach success cache headers to an HTTP error for rejection by middleware."""
        return Response(status_code=status, headers={'Cache-Control': 'public, no-cache', 'ETag': '"error"'})

    app = Starlette(routes=[Route('/api/v1/baskets', error)], middleware=[Middleware(SecurityHeaders)])
    with TestClient(app) as client:
        response = client.get('/api/v1/baskets')
        assert response.status_code == status
        assert response.headers['cache-control'] == 'no-store'
        assert 'etag' not in response.headers


def test_live_catalogue_validators_follow_publication_and_visibility(client: TestClient, tmp_path: Path) -> None:
    """Actual handlers revalidate after publish, pin and basket visibility edits."""
    empty = client.get('/api/v1/baskets')
    assert empty.headers['cache-control'] == 'public, no-cache'
    create_user(client)
    report, _, _, _ = upload(client, tmp_path)
    published = client.get('/api/v1/baskets', headers={'If-None-Match': empty.headers['etag']})
    assert published.status_code == 200
    assert published.headers['etag'] != empty.headers['etag']
    basket_hash = published.json()['items'][0]['hash']
    runs_path = '/api/v1/baskets/' + basket_hash + '/runs'
    runs = client.get(runs_path)
    assert client.get(runs_path, headers={'If-None-Match': runs.headers['etag']}).status_code == 304
    pin = client.put('/api/v1/runs/' + report['run']['id'] + '/pinned', json={'pinned': True}, headers=admin())
    assert pin.status_code == 200
    assert pin.headers['cache-control'] == 'no-store'
    pinned = client.get(runs_path, headers={'If-None-Match': runs.headers['etag']})
    assert pinned.status_code == 200
    assert pinned.headers['etag'] != runs.headers['etag']
    layout = client.get('/api/v1/admin/baskets/layout', headers=admin())
    assert layout.headers['cache-control'] == 'no-store'
    value = layout.json()
    response = client.put(
        '/api/v1/admin/baskets/layout',
        headers=admin(),
        json={
            'expected_revision': value['revision'],
            'items': [{'hash': basket_hash, 'hidden': True, 'collapsed': False}],
        },
    )
    assert response.status_code == 200
    hidden = client.get('/api/v1/baskets', headers={'If-None-Match': published.headers['etag']})
    assert hidden.status_code == 200
    assert hidden.json()['items'] == []
