"""Basket display writes recover as a single logical setting, with revision conflicts."""

import uuid
from pathlib import Path

import pytest
from helpers import archive_fixture, inference, service_for
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient
from test_api import admin, create_user, upload
from test_api import client as _api_client
from test_service import FaultStore, publish, service

from viewer.errors import StorageError, ViewerError
from viewer.persistence.database import Database
from viewer.presentation.queries import Query, basket_runs, catalogue
from viewer.service import ViewerService
from viewer.util import digest, json_bytes
from viewer.validation.archive import validate_archive

client = _api_client


def test_display_revision_hidden_direct_and_publish_preserves_flags(tmp_path: Path) -> None:
    """Verify display revision hidden direct and publish preserves flags."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    h = report['basket']['sha256']
    before = svc.settings()
    flags = {h: {'hidden': True, 'collapsed': True}}
    after = svc.set_settings(before['revision'], [h], flags)
    assert after['revision'] != before['revision']
    assert catalogue(svc, Query(QueryParams('')))['items'] == []
    assert basket_runs(svc, h, Query(QueryParams('')))['total'] == 1
    with pytest.raises(ViewerError) as stale:
        svc.set_settings(before['revision'], [h], flags)
    assert stale.value.status == 409
    publish(svc, tmp_path, run_id=report['run']['id'], score=0.2)
    assert svc.settings()['display'] == flags
    svc.set_order([h])
    svc.refresh()
    assert svc.settings()['display'] == flags
    svc.delete(report['run']['id'])
    assert svc.settings()['display'] == {}
    svc.db.close()


@pytest.mark.parametrize('failure', ['settings/basket-order.json', 'settings/basket-display.json'])
def test_partial_settings_commit_recovers_before_public_read(tmp_path: Path, failure: str) -> None:
    """Verify partial settings commit recovers before public read."""
    store = FaultStore()
    svc = service(tmp_path, store)
    report, _, _ = publish(svc, tmp_path)
    h = report['basket']['sha256']
    before = svc.settings()
    store.armed = failure
    with pytest.raises(StorageError):
        svc.set_settings(before['revision'], [h], {h: {'hidden': False, 'collapsed': True}})
    assert svc.db.operations()
    svc.db.close()
    db = Database(svc.config.runtime)
    restarted = ViewerService(svc.config, store, db)
    assert restarted.settings()['display'][h]['collapsed'] is True
    assert db.operations() == {}
    assert catalogue(restarted, Query(QueryParams('')))['items'][0]['collapsed'] is True
    db.close()


def test_bad_flags_rejected_and_cache_stats_do_not_scan(tmp_path: Path) -> None:
    """Verify bad flags rejected and cache stats do not scan."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    h = report['basket']['sha256']
    before = svc.settings()
    for flags in (
        {h: {'hidden': 1, 'collapsed': False}},
        {'bad': {'hidden': False, 'collapsed': False}},
        {h: {'hidden': False}},
    ):
        with pytest.raises(ViewerError):
            svc.set_settings(before['revision'], [h], flags)
    svc.store.list = lambda prefix: (_ for _ in ()).throw(AssertionError('stats should not scan storage'))
    assert svc.cache_stats()['runs'] == 1
    svc.db.close()


def test_layout_api_authorization_revision_and_old_order_compatibility(client: TestClient, tmp_path: Path) -> None:
    """Verify layout api authorization revision and old order compatibility."""
    create_user(client)
    report, _, _, _ = upload(client, tmp_path)
    h = report['basket']['sha256']
    endpoint = '/api/v1/admin/baskets/layout'
    assert client.get(endpoint).status_code == 401
    snapshot = client.get(endpoint, headers=admin()).json()
    value = {'expected_revision': snapshot['revision'], 'items': [{'hash': h, 'hidden': True, 'collapsed': True}]}
    assert client.put(endpoint, headers=admin(), json=value).status_code == 200
    assert client.put(endpoint, headers=admin(), json=value).status_code == 409
    assert client.put('/api/v1/admin/baskets/order', headers=admin(), json={'items': [h]}).json() == {'items': [h]}
    assert client.get(endpoint, headers=admin()).json()['display'][h]['hidden'] is True
    home = client.get('/api/v1/baskets').json()
    assert home['items'] == [] and home['total_runs'] == home['shown_runs'] == home['total'] == 0
    assert all(v == [] for v in home['filter_options'].values())
    assert client.get('/api/v1/baskets/' + h + '/runs').json()['total'] == 1
    rid = report['run']['id']
    base = '/api/v1/runs/' + rid
    assert client.get(base).status_code == client.get(base + '/config').status_code == 200
    tasks = client.get(base + '/tasks').json()['items']
    task = client.get(base + '/tasks/' + tasks[0]['task_id']).json()
    assert (
        client.get(base + '/tasks/' + tasks[0]['task_id'] + '/files/' + task['files'][0]['file_id']).status_code == 200
    )
    assert client.get('/api/v1/admin/stats', headers=admin()).json()['runs'] == 1


def test_hidden_runs_excluded_before_home_facets_filters_and_pagination(client: TestClient, tmp_path: Path) -> None:
    """Verify hidden runs excluded before home facets filters and pagination."""
    create_user(client, 'alice')
    create_user(client, 'bob')
    svc = service_for(client)
    reports = []
    for name, owner in (('Visible', 'alice'), ('Hidden', 'bob')):
        effort = 'medium' if name == 'Visible' else 'low'

        def prepare(report: dict, basket: dict) -> None:
            """Assign basket-specific model, effort and setup facets."""
            basket['meta']['name'] = name
            report['inference'] = inference('openai/' + name, effort)
            report['run']['tags'] = ['setup: ' + name]

        path = tmp_path / (name + '.tar.gz')

        def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
            """Bind the selected model and effort to the archived configuration identity."""
            identity = {
                'identity_version': 1,
                'native_config': {
                    'model': 'openai/' + name,
                    'provider': {'openai': {'models': {name: {'options': {'reasoningEffort': effort}}}}},
                },
            }
            files['config_identity.json'] = json_bytes(identity)
            report['config']['identity_sha256'] = digest(json_bytes(identity))

        report, _ = archive_fixture(path, run_id=str(uuid.uuid4()), prepare=prepare, configure=change)
        svc.publish(validate_archive(path, tmp_path / name, svc.config.limits), owner, name)
        reports.append(report)
    visible, hidden = reports
    h = hidden['basket']['sha256']
    v = visible['basket']['sha256']
    flags = {k: {'hidden': k == h, 'collapsed': False} for k in svc.order}
    svc.set_settings(svc.settings()['revision'], svc.order, flags)
    svc.set_pinned(hidden['run']['id'], True)
    for query, shown in (
        ('', 1),
        ('?model=openai/Hidden%20%5Blow%5D', 0),
        ('?user=bob', 0),
        ('?basket=' + h, 0),
        ('?pinned=1', 0),
        ('?q=Hidden', 0),
        ('?offset=1&limit=1', 1),
    ):
        data = client.get('/api/v1/baskets' + query).json()
        assert data['total_runs'] == 1 and data['shown_runs'] == shown and data['total'] == shown
        assert data['filter_options'] == {
            'user': ['alice'],
            'basket': [{'hash': v, 'name': 'Visible'}],
            'model': ['openai/Visible [medium]'],
            'effort': ['medium'],
            'setup': ['setup: Visible'],
        }
        assert all(item['hash'] == v for item in data['items'])
    assert len(client.get('/api/v1/admin/baskets/layout', headers=admin()).json()['items']) == 2
    svc.set_settings(
        svc.settings()['revision'], svc.order, {k: {'hidden': True, 'collapsed': False} for k in svc.order}
    )
    data = client.get('/api/v1/baskets').json()
    assert data['total_runs'] == data['shown_runs'] == data['total'] == 0 and data['items'] == []
    assert all(values == [] for values in data['filter_options'].values())
    svc.set_settings(
        svc.settings()['revision'], svc.order, {k: {'hidden': k != h, 'collapsed': False} for k in svc.order}
    )
    data = client.get('/api/v1/baskets').json()
    assert (
        data['total_runs'] == 1
        and data['filter_options']['user'] == ['bob']
        and data['filter_options']['model'] == ['openai/Hidden [low]']
    )
