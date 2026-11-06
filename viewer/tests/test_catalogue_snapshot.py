"""Catalogue construction is read-only and installation replaces one revision."""

from pathlib import Path

import pytest
from test_service import publish, service
from viewer.catalogue import CatalogueBuilder
from viewer.errors import ViewerError
from viewer.util import json_bytes


def test_builder_does_not_write_or_use_live_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A builder can validate stored runs after SQLite is closed, with writes forbidden."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    svc.db.close()

    def forbidden(*args: object, **kwargs: object) -> None:
        """Fail if catalogue construction attempts a storage write."""
        raise AssertionError('builder writes storage')

    monkeypatch.setattr(svc.store, 'put', forbidden)
    snapshot = CatalogueBuilder(svc.config, svc.store, {}).build()
    assert list(snapshot.runs) == [report['run']['id']]
    assert snapshot.order == svc.order


def test_failed_build_preserves_exact_snapshot(tmp_path: Path) -> None:
    """Invalid settings cannot partially replace any catalogue field."""
    svc = service(tmp_path)
    publish(svc, tmp_path)
    previous = svc.snapshot
    svc.store.put('settings/basket-display.json', json_bytes({'version': 1, 'items': {'bad': {}}}))
    with pytest.raises(ViewerError):
        svc.refresh()
    assert svc.snapshot is previous
    svc.db.close()
