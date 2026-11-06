"""Recover transferred publication bytes without trusting missing local copies."""

import shutil
from pathlib import Path

import pytest
from test_service import FaultStore, publish, service
from viewer.errors import StorageError, ViewerError
from viewer.persistence.database import Database
from viewer.service import ViewerService
from viewer.util import digest


def interrupted_publication(tmp_path: Path, boundary: str = 'marker') -> tuple[ViewerService, str, str, dict]:
    """Stop an overwrite after transfer or before one required file is written."""
    store = FaultStore()
    svc = service(tmp_path, store)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    prefix = 'runs/alice/' + rid + '/'
    store.armed = {
        'marker': prefix + 'publication.json',
        'published': 'settings/basket-display.json',
        'archive': prefix + 'archive.tar.gz',
        'report': prefix + 'data/report.json',
    }[boundary]
    with pytest.raises(StorageError):
        publish(svc, tmp_path, run_id=rid, score=0.3, key='recovered')
    op_id, op = next(iter(svc.db.operations().items()))
    return svc, rid, op_id, op


def reopen(svc: ViewerService, restart: bool) -> ViewerService:
    """Optionally reopen the journal before executing recovery explicitly."""
    if restart:
        svc.close()
        svc.db.close()
        return ViewerService(svc.config, svc.store, Database(svc.config.runtime), initialize=False)
    return svc


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize(
    'boundary,missing', [('marker', 'archive'), ('marker', 'data'), ('marker', 'all'), ('report', 'archive')]
)
def test_transferred_publication_survives_missing_staging(
    tmp_path: Path, restart: bool, boundary: str, missing: str
) -> None:
    """Complete from verified storage after any transferred local copies disappear."""
    svc, rid, op_id, op = interrupted_publication(tmp_path, boundary)
    try:
        assert svc.db.progress(op_id, 'archive') == 1
        if boundary == 'marker':
            assert svc.db.progress(op_id, 'files') == len(op['marker']['inventory'])
        else:
            assert svc.db.progress(op_id, 'files') < len(op['marker']['inventory'])
        assert digest(svc.store.get(op['prefix'] + 'archive.tar.gz')) == op['marker']['archive_sha256']
        root = Path(op['staging'])
        if missing == 'archive':
            (root / 'archive.tar.gz').unlink()
        elif missing == 'data':
            shutil.rmtree(root / 'data')
        else:
            shutil.rmtree(root)
        svc = reopen(svc, restart)
        svc.recover()
        assert svc.run(rid)['score'] == 0.3
        assert svc.db.receipt('alice', 'recovered') is not None
        assert not svc.db.operations()
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('boundary', ['marker', 'published'])
@pytest.mark.parametrize('corrupt', ['archive', 'source'])
@pytest.mark.parametrize('damage', ['corrupt', 'missing'])
def test_recovery_rejects_corrupt_transferred_bytes(
    tmp_path: Path, restart: bool, boundary: str, corrupt: str, damage: str
) -> None:
    """Keep the journal and withhold a receipt until all installed bytes match."""
    svc, rid, _, op = interrupted_publication(tmp_path, boundary)
    try:
        suffix = 'archive.tar.gz'
        if corrupt == 'source':
            source = next(
                entry['path']
                for entry in op['marker']['inventory']
                if '/triplets/' in entry['path'] and not entry['path'].endswith('index.json')
            )
            suffix = 'data/' + source
        key = op['prefix'] + suffix
        original = svc.store.get(key)
        if damage == 'missing':
            svc.store.delete(key)
        else:
            svc.store.put(key, b'corrupt bytes')
        svc = reopen(svc, restart)
        with pytest.raises(ViewerError) as error:
            svc.recover()
        assert error.value.code == ('not_found' if damage == 'missing' else 'stored_digest_mismatch')
        assert svc.db.receipt('alice', 'recovered') is None
        assert svc.db.operations()
        svc.store.put(key, original)
        svc.recover()
        assert svc.run(rid)['score'] == 0.3
        assert svc.db.receipt('alice', 'recovered') is not None
        assert not svc.db.operations()
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('missing', ['archive', 'report'])
def test_missing_untransferred_bytes_restore_previous_publication(tmp_path: Path, missing: str) -> None:
    """Rollback incomplete input instead of adopting bytes from an older publication."""
    svc, rid, op_id, op = interrupted_publication(tmp_path, missing)
    try:
        if missing == 'archive':
            assert svc.db.progress(op_id, 'archive') == 0
            path = Path(op['staging']) / 'archive.tar.gz'
        else:
            pending = op['marker']['inventory'][svc.db.progress(op_id, 'files') :]
            assert any(entry['path'] == 'report.json' for entry in pending)
            path = Path(op['staging']) / 'data/report.json'
        path.unlink()
        svc.recover()
        assert svc.run(rid)['score'] == 1.0
        assert svc.db.receipt('alice', 'recovered') is None
        assert not svc.db.operations()
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('corrupt', ['archive', 'report'])
def test_corrupt_untransferred_bytes_keep_recovery_pending(tmp_path: Path, corrupt: str) -> None:
    """Reject altered pending inputs without deleting the journal or prior backup."""
    svc, rid, _, op = interrupted_publication(tmp_path, corrupt)
    try:
        path = Path(op['staging']) / ('archive.tar.gz' if corrupt == 'archive' else 'data/report.json')
        original = path.read_bytes()
        path.write_bytes(b'corrupt pending bytes')
        with pytest.raises(ViewerError) as error:
            svc.recover()
        assert error.value.code == 'stored_digest_mismatch'
        assert svc.db.receipt('alice', 'recovered') is None
        assert svc.db.operations()
        path.write_bytes(original)
        svc.recover()
        assert svc.run(rid)['score'] == 0.3
        assert svc.db.receipt('alice', 'recovered') is not None
        assert not svc.db.operations()
    finally:
        svc.close()
        svc.db.close()
