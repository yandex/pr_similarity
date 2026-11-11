"""Restart cleanup discards interrupted HTTP staging while preserving durable recovery bytes."""

import tempfile
from pathlib import Path

import pytest
from helpers import archive_fixture
from test_local_service import require_operation, service
from viewer.persistence.storage import MemoryStore
from viewer.util import digest
from viewer.validation.archive import validate_archive


@pytest.mark.parametrize('phase', ['receiving', 'extracting'])
def test_restart_cleans_http_staging_and_recovers_durable_archive(tmp_path: Path, phase: str) -> None:
    """An interrupted transfer leaves no unmanaged HTTP staging after restart."""
    remote = MemoryStore()
    original = service(tmp_path, remote)
    stage = Path(tempfile.mkdtemp(prefix='upload-', dir=original.staging))
    archive = stage / 'archive.tar.gz'
    report, manifest = archive_fixture(archive)
    rid = report['run']['id']
    archive_hash = digest(archive.read_bytes())
    original.admit_publication(
        'alice',
        'interrupted',
        rid,
        archive_hash,
        manifest['report_sha256'],
        overwrite=False,
    )
    if phase == 'extracting':
        original.publication_phase('alice', 'interrupted', phase, archive_path=str(archive))
        validate_archive(archive, stage / 'data', original.config.limits)
    original.close()
    original.db.close()
    restarted = service(tmp_path, remote)
    try:
        restarted._recover_uploads()
        restarted._cleanup_terminal_uploads()
        restarted.cleanup_cache()
        assert not stage.exists()
        operation = require_operation(restarted, 'alice', 'interrupted')
        assert operation['phase'] == ('available' if phase == 'extracting' else 'rejected')
        if phase == 'extracting':
            assert restarted.run(rid)['publication_sha256'] == archive_hash
            assert not list((restarted.config.runtime / 'local-uploads').iterdir())
    finally:
        restarted.close()
        restarted.db.close()


def test_restart_cleanup_preserves_unowned_paths_and_later_active_staging(
    tmp_path: Path,
) -> None:
    """Startup cleanup does not follow links or run again over live HTTP staging."""
    runtime = tmp_path / 'runtime'
    staging = runtime / 'staging'
    staging.mkdir(parents=True)
    private = staging / 'operator-notes'
    private.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('preserve')
    link = staging / 'upload-abcdefgh'
    link.symlink_to(outside, target_is_directory=True)
    svc = service(tmp_path)
    try:
        assert private.is_dir() and link.is_symlink()
        assert (outside / 'keep').read_text() == 'preserve'
        active = Path(tempfile.mkdtemp(prefix='upload-', dir=svc.staging))
        (active / 'partial').write_bytes(b'in progress')
        svc.initialize()
        assert (active / 'partial').read_bytes() == b'in progress'
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize(
    'relative',
    [
        'local-staging/version-abcdefgh',
        'local-staging/restore-abcdefgh',
        'local-staging/recover-abcdefgh',
        'local-staging/evicted-12345678-1234-1234-1234-123456789abc',
        'refresh-staging/audit-abcdefgh',
    ],
)
def test_restart_cleans_local_temporary_trees(tmp_path: Path, relative: str) -> None:
    """Crash leftovers are removed once; durable and unowned paths survive."""
    runtime = tmp_path / 'runtime'
    stale = runtime / relative
    stale.mkdir(parents=True)
    (stale / 'archive').write_bytes(b'partial')
    durable = runtime / 'local-uploads' / 'accepted'
    durable.mkdir(parents=True)
    (durable / 'archive').write_bytes(b'durable')
    unknown = stale.parent / 'operator-notes'
    unknown.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = stale.parent / 'restore-ijklmnop'
    link.symlink_to(outside, target_is_directory=True)
    svc = service(tmp_path)
    try:
        assert not stale.exists()
        assert (durable / 'archive').read_bytes() == b'durable'
        assert unknown.is_dir() and link.is_symlink() and outside.is_dir()
        stale.mkdir()
        svc.initialize()
        assert stale.is_dir()
    finally:
        svc.close()
        svc.db.close()
