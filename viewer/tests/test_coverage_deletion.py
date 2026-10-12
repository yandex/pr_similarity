"""Partial measurements remain useful; browser deletion enforces ownership and snapshot."""

import pytest
from starlette.datastructures import QueryParams
from helpers import archive_fixture
import test_auth
from test_auth import login
from test_metrics import task
from viewer.archive import validate_archive
from viewer.metrics import aggregate
from viewer.presentation import Query
from viewer.errors import StorageError, ViewerError

client = test_auth.client


def test_replay_after_deletion_cannot_confirm_an_absent_publication(client, tmp_path):
    rid, imported = publication(client, tmp_path)
    svc = client.app.state.service
    previous = svc.db.receipt("Alice", "original")
    svc.delete(rid)
    with pytest.raises(ViewerError) as caught:
        svc.publish(imported, "Alice", "original")
    assert caught.value.status == 404 and caught.value.code == "publication_removed"
    assert svc.db.receipt("Alice", "original") == previous
    svc.publish(imported, "Alice", "fresh-operation")
    assert rid in svc.runs


def test_interrupted_owner_delete_recovers_without_foreign_trigger(client, tmp_path, monkeypatch):
    rid, imported = publication(client, tmp_path)
    svc = client.app.state.service
    svc.db.create_user("Bob", "secret")
    foreign_headers = login(client, "Bob")
    foreign_cookies = dict(client.cookies)
    headers = login(client)
    owner_cookies = dict(client.cookies)
    url = "/api/v1/runs/" + rid + "?expected_publication_sha256=" + imported.archive_sha256
    original = svc.store.delete
    calls = []

    def fail(key):
        calls.append(key)
        raise StorageError()

    monkeypatch.setattr(svc.store, "delete", fail)
    assert client.delete(url, headers=headers).status_code == 503
    assert svc.db.operations()
    # Reuse sessions so no generic GET/login can trigger storage recovery.
    client.cookies.clear()
    client.cookies.update(foreign_cookies)
    before = len(calls)
    assert client.delete(url, headers=foreign_headers).status_code == 403
    assert len(calls) == before and svc.db.operations()
    client.cookies.clear()
    before = len(calls)
    assert client.delete(url, headers=headers).status_code == 401
    assert len(calls) == before and svc.db.operations()
    monkeypatch.setattr(svc.store, "delete", original)
    client.cookies.update(owner_cookies)
    assert client.delete(url, headers=headers).status_code == 200
    assert rid not in svc.runs and not svc.db.operations()
    assert any(k.endswith("/archive.tar.gz") for k in svc.store.list("trash/"))


@pytest.mark.parametrize("sort", ["cost", "total_tokens", "cache_read", "input", "output", "cache_write", "reasoning"])
def test_known_time_subtotal_and_partial_sort(sort):
    known, missing = task(seconds=12), task(seconds=None)
    result = aggregate([known, missing])
    assert result["time"]["total_seconds"] is None
    assert result["time"]["known_subtotal_seconds"] == 12

    def row(name, amount, subtotal, count):
        value = {"amount": amount, "known_subtotal": subtotal, "known_tasks": count}
        return {
            "run_id": name,
            "created_at": "2026-10-09T00:00:00Z",
            "cost": value,
            "tokens": {key: value for key in ["total", "cache_read", "input", "output", "cache_write", "reasoning"]},
        }

    rows = [row("unknown", None, 0, 0), row("partial", None, 3, 1), row("zero", 0, 0, 1), row("full", 2, 2, 1)]
    assert [r["run_id"] for r in Query(QueryParams("sort=" + sort + "&direction=desc")).sorted(rows)] == [
        "partial",
        "full",
        "zero",
        "unknown",
    ]
    assert [r["run_id"] for r in Query(QueryParams("sort=" + sort + "&direction=asc")).sorted(rows)] == [
        "zero",
        "full",
        "partial",
        "unknown",
    ]


def publication(client, tmp_path):
    path = tmp_path / "run.tar.gz"
    report, _ = archive_fixture(path)
    svc = client.app.state.service
    imported = validate_archive(path, tmp_path / "data", svc.config.limits)
    svc.publish(imported, "Alice", "original")
    return report["run"]["id"], imported


@pytest.mark.parametrize("actor", ["Alice", "operator"])
def test_owner_and_admin_delete_preserve_trash(client, tmp_path, actor):
    rid, imported = publication(client, tmp_path)
    headers = login(client, actor)
    url = "/api/v1/runs/" + rid + "?expected_publication_sha256=" + imported.archive_sha256
    response = client.delete(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert client.get("/api/v1/runs/" + rid).status_code == 404
    svc = client.app.state.service
    assert not svc.db.operations()
    assert any(k.endswith("/archive.tar.gz") for k in svc.store.list("trash/"))
    assert svc.db.owner(rid)["login"] == "Alice"


def test_delete_rejects_foreign_guest_csrf_and_stale_snapshot(client, tmp_path):
    rid, imported = publication(client, tmp_path)
    url = "/api/v1/runs/" + rid + "?expected_publication_sha256=" + imported.archive_sha256
    assert client.delete(url).status_code == 401
    svc = client.app.state.service
    svc.db.create_user("Bob", "secret")
    headers = login(client, "Bob")
    assert client.delete(url, headers=headers).status_code == 403
    headers = login(client)
    assert client.delete(url).status_code == 403
    assert client.delete("/api/v1/runs/" + rid, headers=headers).status_code == 422
    assert (
        client.delete("/api/v1/runs/" + rid + "?expected_publication_sha256=" + "f" * 64, headers=headers).status_code
        == 409
    )
    assert rid in svc.runs
    svc.db.update_user("Alice", enabled=False)
    assert client.delete(url, headers=headers).status_code == 401
