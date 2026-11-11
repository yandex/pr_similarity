"""Public response cache stays bounded and follows durable and transient catalogue state."""

from dataclasses import replace
from pathlib import Path

import pytest
import viewer.web.api as api
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient
from test_local_api import get_local_service, local_upload
from test_local_api import local_client as local_client
from test_local_service import service
from test_service import publish
from viewer.presentation.queries import Query, basket_runs, catalogue
from viewer.service import ViewerService

# Re-export the public fixture for pytest collection in this module.
__all__ = ['local_client']


def test_cache_reuses_query_and_invalidates_metadata_and_admission(tmp_path: Path) -> None:
    """Invalidate cached projections after metadata edits and transient write admission."""
    svc = service(tmp_path)
    report, _, imported = publish(svc, tmp_path)
    rid = report['run']['id']
    calls = []

    def build() -> dict:
        """Project current fixture metadata and transient write admission state."""
        calls.append(True)
        return {'name': svc.metadata[rid]['display_name'], 'busy': sorted(svc.busy_runs())}

    first = svc.catalogue_response(('query',), build)
    assert svc.catalogue_response(('query',), build) is first and len(calls) == 1
    svc.patch_metadata(rid, 'alice', {'display_name': 'changed'})
    assert svc.catalogue_response(('query',), build)['name'] == 'changed'
    svc.admit_publication(
        'alice', 'replacement', rid, imported.archive_sha256, imported.manifest.report_sha256, overwrite=True
    )
    assert svc.catalogue_response(('query',), build)['busy'] == [rid]
    svc.publication_failed('alice', 'replacement', RuntimeError())
    assert svc.catalogue_response(('query',), build)['busy'] == []
    svc.close()
    svc.db.close()


def test_cache_is_bounded_and_does_not_reuse_another_query(tmp_path: Path) -> None:
    """Keep query projections isolated while enforcing entry and byte budgets."""
    svc = service(tmp_path)
    svc.config = replace(svc.config, catalogue_cache_entries=2, catalogue_cache_bytes=100)
    for number in range(10):
        assert svc.catalogue_response((number,), lambda: {'number': number})['number'] == number
    assert len(svc._catalogue_responses) == 2
    assert svc._catalogue_response_bytes <= 100
    svc.close()
    svc.db.close()


def test_local_http_catalogue_cache_parity_queries_and_visibility(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Match uncached projections across HTTP filters, metadata edits and visibility."""
    client = local_client
    response, report, headers, data = local_upload(client, tmp_path)
    assert response.status_code == 201
    svc = get_local_service(client)
    rid, basket = report['run']['id'], report['basket']['sha256']
    calls = []

    def counted(service: ViewerService, query: Query) -> dict:
        """Count catalogue builds while preserving the real query projection."""
        calls.append(True)
        return catalogue(service, query)

    monkeypatch.setattr(api, 'catalogue', counted)
    monkeypatch.setattr(svc, 'full_report', lambda _: (_ for _ in ()).throw(AssertionError('Catalogue loaded report')))
    expected = catalogue(svc, Query(QueryParams()))
    assert client.get('/api/v1/baskets').json() == expected
    assert client.get('/api/v1/baskets').json() == expected
    assert len(calls) == 1
    assert client.get('/api/v1/baskets?q=missing-title').json()['shown_runs'] == 0
    path = '/api/v1/baskets/' + basket + '/runs'
    assert client.get(path).json() == basket_runs(svc, basket, Query(QueryParams()))
    svc.patch_metadata(rid, 'alice', {'display_name': 'Updated display'})
    assert client.get(path).json()['items'][0]['display_name'] == 'Updated display'
    svc.admit_publication(
        'alice', 'busy-cache', rid, headers['X-Archive-SHA256'], headers['X-Report-SHA256'], overwrite=True
    )
    assert client.get('/api/v1/baskets').json()['shown_runs'] == 0
    assert client.get(path).json()['total'] == 0
    svc.publication_failed('alice', 'busy-cache', RuntimeError())
    assert client.get(path).json()['total'] == 1
    settings = svc.settings()
    svc.set_settings(settings['revision'], svc.order, {basket: {'hidden': True, 'collapsed': False}})
    assert client.get('/api/v1/baskets').json()['total_runs'] == 0
