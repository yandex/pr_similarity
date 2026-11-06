"""Background refresh keeps reads available and retains invalidated requests."""

import threading
from pathlib import Path

import pytest
from test_service import publish, service
from viewer.catalogue import CatalogueBuilder, CatalogueSnapshot
from viewer.publication import CatalogueEntry


def test_refresh_during_write_retries_and_preserves_new_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale scan cannot overwrite a name change or consume its refresh request."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    entered, release = threading.Event(), threading.Event()
    original = CatalogueBuilder.build
    calls = []

    def slow(builder: CatalogueBuilder) -> CatalogueSnapshot:
        """Hold a scan at the boundary where a concurrent mutation invalidates it."""
        result = original(builder)
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(CatalogueBuilder, 'build', slow)
    svc.start_background()
    try:
        svc.request_refresh()
        assert entered.wait(5)
        assert svc.run(rid)['run_id'] == rid
        svc.patch_metadata(rid, 'alice', {'display_name': 'updated'})
        second = svc.request_refresh()
        release.set()
        worker = svc.maintenance
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.refresh_state['completed_id'] == second['request_id'], 12)
        assert len(calls) >= 2
        assert svc.run(rid)['display_name'] == 'updated'
    finally:
        release.set()
        svc.close()
        svc.db.close()


def test_refresh_builder_has_no_access_to_live_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan uses copied owner bindings and cannot commit to storage."""
    svc = service(tmp_path)
    publish(svc, tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        """Fail if background catalogue construction writes storage."""
        raise AssertionError('background scan wrote storage')

    monkeypatch.setattr(svc.store, 'put', forbidden)
    assert svc.refresh_once()
    svc.db.close()


def test_delete_during_scan_does_not_turn_stale_read_into_terminal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed marker invalidates the scan and keeps its request outstanding."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    entered, release = threading.Event(), threading.Event()
    original = CatalogueBuilder._marker

    def slow(builder: CatalogueBuilder, key: str) -> CatalogueEntry:
        """Hold a scan at the boundary where a concurrent mutation invalidates it."""
        entered.set()
        assert release.wait(10)
        return original(builder, key)

    monkeypatch.setattr(CatalogueBuilder, '_marker', slow)
    svc.start_background()
    try:
        job = svc.request_refresh()
        assert entered.wait(5)
        svc.delete(rid)
        release.set()
        worker = svc.maintenance
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.refresh_state['completed_id'] == job['request_id'], 12)
        assert rid not in svc.runs
    finally:
        release.set()
        svc.close()
        svc.db.close()


def test_unexpected_refresh_failure_survives_concurrent_metadata_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalidated scan still exposes programming errors to maintenance diagnostics."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']

    def fail_after_write(builder: CatalogueBuilder) -> CatalogueSnapshot:
        """Invalidate the captured revision before an unexpected builder failure."""
        svc.patch_metadata(rid, 'alice', {'display_name': 'updated'})
        raise RuntimeError('unexpected builder failure')

    monkeypatch.setattr(CatalogueBuilder, 'build', fail_after_write)
    try:
        with pytest.raises(RuntimeError, match='unexpected builder failure'):
            svc.refresh_once()
        assert svc.run(rid)['display_name'] == 'updated'
    finally:
        svc.close()
        svc.db.close()
