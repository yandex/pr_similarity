"""Checkpoint creation preserves authoritative registry bootstrap provenance."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_service import publish
from viewer.bundles import REGISTRY_HEAD, BundleRegistrySnapshot
from viewer.config import Config
from viewer.local_checkpoint import CHECKPOINT_PREFIX, restore
from viewer.local_service import LocalViewerService
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore


def empty_service(root: Path, store: MemoryStore) -> LocalViewerService:
    """Create an account-only local viewer in a previously unused namespace."""
    config = Config(runtime=root / 'runtime', storage='mock', publication_storage='local', disk_reserve_bytes=0)
    database = Database(config.runtime)
    database.create_user('alice', 'secret')
    return LocalViewerService(config, store, database)


def complete_checkpoint(service: LocalViewerService) -> str:
    """Wait for the service-owned worker and return its closed checkpoint key."""
    service.request_checkpoint()
    assert service._checkpoint_thread is not None
    service._checkpoint_thread.join(10)
    assert not service._checkpoint_thread.is_alive()
    assert service.checkpoint_job['status'] == 'succeeded'
    key = service.checkpoint_job['manifest_key']
    assert isinstance(key, str)
    return key


def test_account_only_checkpoint_restores_through_empty_registry(tmp_path: Path) -> None:
    """A pre-publication checkpoint restores and reconciles against a closed empty registry."""
    store = MemoryStore()
    original = empty_service(tmp_path / 'original', store)
    key = complete_checkpoint(original)
    config = original.config
    assert REGISTRY_HEAD in store.objects
    assert original.repository.registry_initialized()
    assert original.close()
    original.db.close()

    result = restore(config, store, key, tmp_path / 'restored')
    restored_config = replace(config, runtime=result.runtime)
    restored_db = Database(result.runtime)
    restored = LocalViewerService(restored_config, store, restored_db)
    try:
        assert restored.repository.registry_initialized()
        refresh = restored.refresh()
        assert refresh['status'] == 'succeeded'
        assert refresh['checked'] == 0
        assert restored.repository.restore_reconciliation()['status'] == 'reconciled'
        restored.check_write_admission()
    finally:
        assert restored.close()
        restored_db.close()


def test_checkpoint_refuses_lost_previously_initialized_empty_registry(tmp_path: Path) -> None:
    """A lost initialized head is not recreated from an otherwise empty namespace."""
    store = MemoryStore()
    service = empty_service(tmp_path, store)
    first = complete_checkpoint(service)
    before = set(store.list(CHECKPOINT_PREFIX))
    del store.objects[REGISTRY_HEAD]

    service.request_checkpoint()
    assert service._checkpoint_thread is not None
    service._checkpoint_thread.join(10)
    assert not service._checkpoint_thread.is_alive()
    assert service.checkpoint_job['status'] == 'failed'
    assert service.checkpoint_job['error_code'] == 'registry_missing'
    assert REGISTRY_HEAD not in store.objects
    assert set(store.list(CHECKPOINT_PREFIX)) == before == {first}
    assert service.close()
    service.db.close()


def test_first_backup_and_checkpoint_share_registry_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent first backup and checkpoint perform one serialized registry initialization."""
    store = MemoryStore()
    service = empty_service(tmp_path, store)
    publication = tmp_path / 'publication'
    publication.mkdir()
    report, _, _ = publish(service, publication)
    service.activate_backup(report['run']['id'])
    entered, second, release = threading.Event(), threading.Event(), threading.Event()
    original = service.bundles.initialize_registry
    calls = 0

    def initialize_registry() -> BundleRegistrySnapshot:
        """Hold the first bootstrap so a concurrent checkpoint attempts the same boundary."""
        nonlocal calls
        calls += 1
        if calls > 1:
            second.set()
        entered.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(service.bundles, 'initialize_registry', initialize_registry)
    with ThreadPoolExecutor(max_workers=1) as pool:
        backup = pool.submit(service.backups.run_once)
        assert entered.wait(5)
        service.request_checkpoint()
        deadline = time.monotonic() + 5
        while service.checkpoint_job['status'] != 'running' and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.checkpoint_job['status'] == 'running'
        assert not second.wait(0.2)
        assert service._checkpoint_thread is not None and service._checkpoint_thread.is_alive()
        release.set()
        assert backup.result(timeout=10)

    service._checkpoint_thread.join(10)
    assert not service._checkpoint_thread.is_alive()
    assert service.checkpoint_job['status'] == 'succeeded'
    assert calls == 1
    assert service.repository.registry_initialized()
    assert len(service.bundles.registry_snapshot().entries) == 1
    assert service.close()
    service.db.close()
