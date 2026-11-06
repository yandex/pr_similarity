"""Slow writes leave unrelated catalogue and evidence reads responsive."""

import sqlite3
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_service import publish, service
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.storage import MemoryStore
from viewer.web.api import create_app


def test_slow_overwrite_does_not_block_other_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated evidence readable while an overwrite is paused."""
    svc = service(tmp_path)
    first, _, _ = publish(svc, tmp_path)
    second, _, _ = publish(svc, tmp_path, run_id='00000000-0000-4000-8000-000000000002')
    first_id, second_id = first['run']['id'], second['run']['id']
    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    original = svc.store.put_file
    errors = []

    def slow(key: str, path: str | Path, *, deadline: float | None = None) -> None:
        """Pause the storage mutation until concurrent reads have been checked."""
        if first_id in key and key.endswith('/archive.tar.gz'):
            entered.set()
            assert release.wait(10)
        return original(key, path, deadline=deadline)

    monkeypatch.setattr(svc.store, 'put_file', slow)

    def overwrite() -> None:
        """Capture any overwrite failure for assertion in the main thread."""
        try:
            publish(svc, tmp_path, run_id=first_id, score=0.3)
        except (AssertionError, OSError, sqlite3.Error, ViewerError) as error:
            errors.append(error)

    def read() -> None:
        """Check read availability and retain thread failures for the test."""
        try:
            with svc.lock:
                assert svc.run(second_id)['score'] == 1
                assert svc.full_report(second_id).run.id == second_id
                with pytest.raises(ViewerError) as caught:
                    svc.full_report(first_id)
                assert caught.value.code == 'run_updating'
            read_done.set()
        except (AssertionError, OSError, sqlite3.Error, ViewerError) as error:
            errors.append(error)

    writer = threading.Thread(target=overwrite)
    reader = threading.Thread(target=read)
    writer.start()
    try:
        assert entered.wait(5)
        reader.start()
        assert read_done.wait(1), 'unrelated reads wait for file upload'
    finally:
        release.set()
        writer.join(10)
        if reader.ident:
            reader.join(10)
        svc.db.close()
    assert not errors
    assert not writer.is_alive() and not reader.is_alive()


def test_http_delete_keeps_other_runs_and_catalogue_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP wrapper must not reintroduce the global lock around storage writes."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    app = create_app(Config(runtime=tmp_path / 'runtime', storage='mock', admin_token_file=token), MemoryStore())
    svc = app.state.service
    svc.db.create_user('alice', 'secret')
    first, _, _ = publish(svc, tmp_path)
    second, _, _ = publish(svc, tmp_path, run_id='00000000-0000-4000-8000-000000000002')
    rid = first['run']['id']
    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    original = svc.store.delete
    errors = []

    def slow(key: str) -> None:
        """Pause the storage mutation until concurrent reads have been checked."""
        if rid in key and key.endswith('/publication.json'):
            entered.set()
            assert release.wait(10)
        return original(key)

    monkeypatch.setattr(svc.store, 'delete', slow)
    client = TestClient(app)

    def remove() -> None:
        """Delete through HTTP and retain any thread failure."""
        try:
            assert (
                client.delete('/api/v1/admin/runs/' + rid, headers={'Authorization': 'Bearer ' + 'A' * 32}).status_code
                == 200
            )
        except (AssertionError, OSError, sqlite3.Error, ViewerError) as error:
            errors.append(error)

    def read() -> None:
        """Check read availability and retain thread failures for the test."""
        try:
            assert client.get('/api/v1/runs/' + second['run']['id'] + '/tasks').status_code == 200
            assert client.get('/api/v1/runs/' + rid).status_code == 503
            assert client.get('/api/v1/baskets').json()['total_runs'] == 1
            read_done.set()
        except (AssertionError, OSError, sqlite3.Error, ViewerError) as error:
            errors.append(error)

    writer, reader = threading.Thread(target=remove), threading.Thread(target=read)
    writer.start()
    try:
        assert entered.wait(5)
        reader.start()
        assert read_done.wait(2), 'HTTP readers waited for another run deletion'
    finally:
        release.set()
        writer.join(10)
        if reader.ident:
            reader.join(10)
        client.close()
        svc.db.close()
    assert not errors


def test_shutdown_waits_for_active_request_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No maintenance worker is needed for an HTTP/storage writer to own SQLite."""
    svc = service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = svc.store.put_file
    errors = []

    def slow(key: str, path: str | Path, *, deadline: float | None = None) -> None:
        """Pause the storage mutation until concurrent reads have been checked."""
        entered.set()
        assert release.wait(5)
        return original(key, path, deadline=deadline)

    monkeypatch.setattr(svc.store, 'put_file', slow)

    def publish_run() -> None:
        """Record cancellation of the active publication during shutdown."""
        try:
            publish(svc, tmp_path)
        except ViewerError as error:
            errors.append(error.code)

    writer = threading.Thread(target=publish_run)
    writer.start()
    try:
        assert entered.wait(5)
        assert svc.close(timeout=0.01) is False
        assert svc.db.operations()
    finally:
        release.set()
        writer.join(5)
        assert svc.close(timeout=5) is True
        svc.db.close()
    assert errors == ['operation_cancelled']
