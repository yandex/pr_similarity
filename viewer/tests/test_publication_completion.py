"""Publication visibility follows receipt persistence, without a global scan."""

from pathlib import Path

import pytest
from test_service import FaultStore, publish, service
from viewer.catalogue import CatalogueBuilder
from viewer.errors import StorageError, ViewerError
from viewer.persistence.database import Database
from viewer.service import ViewerService


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('corrupt_storage', [False, True])
def test_marker_failure_recovers_without_transferred_report(
    tmp_path: Path, restart: bool, corrupt_storage: bool
) -> None:
    """Recover durable files after losing only the staged report before the marker."""
    store = FaultStore()
    svc = service(tmp_path, store)
    try:
        report, _, _ = publish(svc, tmp_path)
        rid = report['run']['id']
        store.armed = 'runs/alice/' + rid + '/publication.json'
        with pytest.raises(StorageError):
            publish(svc, tmp_path, run_id=rid, score=0.3, key='lost-report')
        op_id, op = next(iter(svc.db.operations().items()))
        assert op['phase'] == 'installing'
        assert svc.db.progress(op_id, 'files') == len(op['marker']['inventory'])
        staged_report = Path(op['staging']) / 'data' / 'report.json'
        report_key = op['prefix'] + 'data/report.json'
        report_bytes = staged_report.read_bytes()
        assert store.get(report_key) == report_bytes
        staged_report.unlink()
        assert (Path(op['staging']) / 'archive.tar.gz').is_file()
        assert svc.db.receipt('alice', 'lost-report') is None
        if restart:
            svc.close()
            svc.db.close()
            svc = ViewerService(svc.config, store, Database(svc.config.runtime), initialize=False)
        if corrupt_storage:
            store.put(report_key, b'{}')
            with pytest.raises(ViewerError) as invalid:
                svc.recover()
            assert invalid.value.code == 'stored_digest_mismatch'
            assert svc.db.receipt('alice', 'lost-report') is None
            assert svc.db.operations()
            assert op['prefix'] + 'publication.json' not in store.list(op['prefix'])
            store.put(report_key, report_bytes)
        if restart:
            svc.initialize()
        else:
            svc.recover()
        assert svc.run(rid)['score'] == 0.3
        assert svc.db.receipt('alice', 'lost-report') is not None
        assert not svc.db.operations()
        publish(svc, tmp_path, run_id=rid, score=0.4)
        assert svc.run(rid)['score'] == 0.4
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('phase', ['published', 'installing'])
def test_installed_publication_recovers_without_staged_report(tmp_path: Path, phase: str) -> None:
    """Finish installed evidence even if staging or the last phase update was lost."""
    store = FaultStore()
    svc = service(tmp_path, store)
    try:
        report, _, _ = publish(svc, tmp_path)
        rid = report['run']['id']
        store.armed = 'settings/basket-display.json'
        with pytest.raises(StorageError):
            publish(svc, tmp_path, run_id=rid, score=0.3, key='lost-staging')
        op_id, op = next(iter(svc.db.operations().items()))
        assert op['phase'] == 'published'
        (Path(op['staging']) / 'data' / 'report.json').unlink()
        op['phase'] = phase
        svc.db.save_operation(op_id, op)
        svc.recover()
        assert svc.run(rid)['score'] == 0.3
        assert svc.db.receipt('alice', 'lost-staging') is not None
        assert not svc.db.operations()
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('failure', ['settings/basket-display.json', 'receipt'])
def test_failed_completion_keeps_previous_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A failure after installing files cannot expose an uncommitted score."""
    store = FaultStore()
    svc = service(tmp_path, store)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    previous = svc.snapshot.runs[rid]
    original = svc.db.finish_operation
    if failure == 'receipt':

        def fail(*args: object, **kwargs: object) -> None:
            """Simulate receipt persistence failure after files are installed."""
            raise StorageError()

        monkeypatch.setattr(svc.db, 'finish_operation', fail)
    else:
        store.armed = failure
    with pytest.raises(StorageError):
        publish(svc, tmp_path, run_id=rid, score=0.3, key='retry')
    assert svc.snapshot.runs[rid] is previous
    with pytest.raises(ViewerError) as unavailable:
        svc.run(rid)
    assert unavailable.value.code == 'run_updating'
    assert svc.db.receipt('alice', 'retry') is None
    assert len(svc.db.operations()) == 1
    monkeypatch.setattr(svc.db, 'finish_operation', original)
    svc.recover()
    assert svc.run(rid)['score'] == 0.3
    assert svc.db.receipt('alice', 'retry') is not None
    assert not svc.db.operations()
    svc.db.close()


def test_mutations_do_not_scan_other_publications(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish, layout and delete complete without scanning unrelated evidence."""
    svc = service(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        """Fail if a local mutation starts a global catalogue scan."""
        raise AssertionError('global catalogue scan')

    monkeypatch.setattr(CatalogueBuilder, 'build', forbidden)
    report, receipt, _ = publish(svc, tmp_path)
    basket_hash = report['basket']['sha256']
    svc.set_settings(svc.settings()['revision'], [basket_hash], {basket_hash: {'hidden': True, 'collapsed': True}})
    svc.delete(receipt['run_id'])
    assert not svc.runs and not svc.db.operations()
    svc.db.close()


def test_background_publish_does_not_request_global_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running worker stays idle after local mutations until an explicit refresh."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    original = svc.store.get
    reads: list[str] = []

    def observe(key: str, limit: int = 64 << 20) -> bytes:
        """Record reads of the unrelated first run's evidence."""
        if rid in key:
            reads.append(key)
        return original(key, limit)

    monkeypatch.setattr(svc.store, 'get', observe)
    svc.start_background()
    try:
        second, _, _ = publish(svc, tmp_path, run_id='00000000-0000-4000-8000-000000000002')
        publish(svc, tmp_path, run_id=second['run']['id'], score=0.5)
        with svc.maintenance.condition:
            assert svc.maintenance.requested == 0
        assert not reads
        job = svc.request_refresh()
        with svc.maintenance.condition:
            assert svc.maintenance.condition.wait_for(
                lambda: svc.maintenance.refresh_state['completed_id'] == job['request_id'], 5
            )
        assert reads
    finally:
        svc.close()
        svc.db.close()
