"""Autonomous journal recovery and read-only request boundaries."""

import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_service import FaultStore, publish, service
from viewer.config import Config
from viewer.errors import StorageError, ViewerError
from viewer.maintenance import Maintenance
from viewer.web.api import create_app


def test_recovery_finishes_without_another_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduled recovery completes the receipt after the original request fails."""
    store = FaultStore()
    svc = service(tmp_path, store)
    svc.start_background()
    completed = threading.Event()
    original = svc.db.finish_operation

    def finish(
        op_id: str,
        login: str | None = None,
        key: str | None = None,
        fingerprint: str | None = None,
        receipt: dict | None = None,
    ) -> None:
        """Signal that autonomous recovery durably completed the operation."""
        result = original(op_id, login, key, fingerprint, receipt)
        completed.set()
        return result

    monkeypatch.setattr(svc.db, 'finish_operation', finish)
    try:
        store.armed = 'settings/basket-display.json'
        with pytest.raises(StorageError):
            publish(svc, tmp_path, key='autonomous')
        assert completed.wait(5)
        with svc.lock:
            assert not svc.db.operations()
            assert svc.db.receipt('alice', 'autonomous') is not None
    finally:
        svc.close()
        svc.db.close()


def test_pending_operation_rejects_new_write_and_affected_evidence(tmp_path: Path) -> None:
    """A new mutation does not secretly recover, and mixed evidence is unavailable."""
    store = FaultStore()
    svc = service(tmp_path, store)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    store.armed = 'settings/basket-display.json'
    with pytest.raises(StorageError):
        publish(svc, tmp_path, run_id=rid, score=0.3)
    with pytest.raises(ViewerError, match='recovery') as error:
        svc.patch_metadata(rid, 'alice', {'display_name': 'later'})
    assert error.value.status == 503
    with pytest.raises(ViewerError) as error:
        svc.full_report(rid)
    assert error.value.code == 'run_updating'
    assert len(svc.db.operations()) == 1
    svc.recover()
    svc.db.close()


def test_recovery_retries_are_bounded_and_can_be_requested_again() -> None:
    """A persistent outage stops at the configured limit, then permits explicit retry."""
    calls = []

    def recover() -> None:
        """Count unsuccessful recovery attempts."""
        calls.append(1)
        raise StorageError()

    worker = Maintenance(recover, retry_seconds=0, attempts=2)
    worker.start()
    try:
        worker.request_recovery()
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.state['status'] == 'failed', 5)
        assert len(calls) == 2
        worker.request_recovery()
        with worker.condition:
            assert worker.condition.wait_for(lambda: worker.state['status'] == 'failed', 5)
        assert len(calls) == 4
    finally:
        worker.close()


def test_http_download_rejects_pending_overwrite_before_opening_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP download must not send new bytes with the previous catalogue digest."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    store = FaultStore()
    app = create_app(Config(runtime=tmp_path / 'runtime', storage='mock', admin_token_file=token), store)
    svc = app.state.service
    svc.db.create_user('alice', 'secret')
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    store.armed = 'settings/basket-display.json'
    with pytest.raises(StorageError):
        publish(svc, tmp_path, run_id=rid, score=0.3)

    def forbidden(*args: object, **kwargs: object) -> None:
        """Fail if an uncommitted archive is opened for download."""
        raise AssertionError('uncommitted archive opened')

    monkeypatch.setattr(store, 'open', forbidden)
    client = TestClient(app)
    try:
        result = client.get('/api/v1/runs/' + rid + '/archive', headers={'Authorization': 'Bearer ' + 'A' * 32})
        assert result.status_code == 503
        assert result.json()['error']['code'] == 'run_updating'
    finally:
        client.close()
        svc.db.close()
