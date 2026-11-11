"""Immutable local installations and reader-safe cache cleanup."""

from pathlib import Path

import pytest
from local_test_support import build_publication
from viewer.bundles import BundleIdentity
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_files import LocalFiles, VersionStore
from viewer.validation.archive import ImportedRun

RID = '00000000-0000-4000-8000-000000000001'


def source(tmp_path: Path) -> ImportedRun:
    """Return a real validated archive for installation and reader-pin checks."""
    return build_publication(tmp_path / 'source-fixture', run_id=RID).imported


def test_install_is_independent_and_reader_prevents_eviction(tmp_path: Path) -> None:
    """Installed bytes are independent of staging and pinned readers prevent eviction."""
    files = LocalFiles(Config(runtime=tmp_path / 'runtime', storage='mock', disk_reserve_bytes=0))
    imported = source(tmp_path)
    identity = BundleIdentity('alice', RID, 1, imported.archive_sha256)
    path = files.install(imported, identity, {'run_id': RID})
    original = imported.archive.read_bytes()
    imported.archive.write_bytes(b'changed')
    assert (path / 'archive.tar.gz').read_bytes() == original
    store = VersionStore(files, path, f'runs/alice/{RID}/')
    stream = store.open(f'runs/alice/{RID}/archive.tar.gz')
    assert not files.evict(path)
    stream.close()
    assert files.evict(path)
    assert files.marker(identity) == {'run_id': RID}


def test_reservation_is_bounded_and_released(tmp_path: Path) -> None:
    """Overlapping disk reservations obey capacity and release it on context exit."""
    config = Config(runtime=tmp_path, storage='mock', disk_reserve_bytes=0, pending_backup_max_bytes=100)
    files = LocalFiles(config)
    with files.reserve(80):
        with pytest.raises(ViewerError, match='capacity'):
            with files.reserve(30):
                pass
    with files.reserve(100):
        pass


def test_store_cannot_escape_version_prefix(tmp_path: Path) -> None:
    """Version-store reads cannot traverse outside their bound publication prefix."""
    files = LocalFiles(Config(runtime=tmp_path, storage='mock', disk_reserve_bytes=0))
    imported = source(tmp_path)
    identity = BundleIdentity('alice', RID, 1, imported.archive_sha256)
    path = files.install(imported, identity, {'run_id': RID})
    store = VersionStore(files, path, f'runs/alice/{RID}/')
    with pytest.raises(ViewerError):
        store.open(f'runs/alice/{RID}/../source.tar.gz')
