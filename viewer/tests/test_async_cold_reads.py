"""Cold HTTP readers wait without occupying the shared AnyIO worker limiter."""

import asyncio
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

import anyio.to_thread
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_local_api import get_local_service, local_upload
from test_local_api import local_client as local_client
from viewer.local_repository import LocalRecord
from viewer.local_restores import RestoreTicket
from viewer.local_service import LocalViewerService, ReadLease

# Re-export the public fixture for pytest collection in this module.
__all__ = ['local_client']

type ColdRun = tuple[Starlette, LocalViewerService, str]


@pytest.fixture
def cold_run(local_client: TestClient, tmp_path: Path) -> ColdRun:
    """Publish and back up a real archive, then remove its unpinned local copy."""
    response, report, _, _ = local_upload(local_client, tmp_path)
    assert response.status_code == 201, response.text
    app = local_client.app
    assert isinstance(app, Starlette)
    service = get_local_service(local_client)
    run_id = report['run']['id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = service.repository.get(run_id)
        assert record is not None
        if record.backup_state == 'backed_up':
            break
        time.sleep(0.01)
    record = service.repository.get(run_id)
    assert record is not None and record.backup_state == 'backed_up'
    assert record.local_path is not None
    assert service.files.evict(Path(record.local_path))
    assert service.repository.update_local_state(
        run_id,
        generation=record.generation,
        local_path=None,
        cache_state='cold',
        cache_bytes=0,
    )
    return app, service, run_id


async def wait_for_event(event: threading.Event) -> None:
    """Wait for a worker milestone without borrowing another AnyIO thread."""
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.01)


def track_preparations(
    service: LocalViewerService,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> threading.Event:
    """Observe run selection on both the old and the asynchronous HTTP paths."""
    prepared = threading.Event()
    original = service._read_record
    observed = 0

    def read_record(run_id: str) -> LocalRecord:
        """Count selections before readers begin waiting for their shared restore."""
        nonlocal observed
        result = original(run_id)
        observed += 1
        if observed >= count:
            prepared.set()
        return result

    monkeypatch.setattr(service, '_read_record', read_record)
    return prepared


def test_forty_cold_http_readers_leave_catalogue_responsive(
    cold_run: ColdRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forty cold GETs cannot exhaust the worker tokens needed by the catalogue."""
    app, service, run_id = cold_run
    entered, release = threading.Event(), threading.Event()
    prepared = track_preparations(service, monkeypatch, 40)
    original = service._restore
    restore_calls = 0

    def restore(record: LocalRecord) -> None:
        """Hold the single shared restore until the catalogue has answered."""
        nonlocal restore_calls
        restore_calls += 1
        entered.set()
        assert release.wait(15)
        original(record)

    monkeypatch.setattr(service, '_restore', restore)

    async def exercise() -> None:
        """Drive actual ASGI endpoints and always release blocked workers on failure."""
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == 40
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            readers = [asyncio.create_task(client.get('/api/v1/runs/' + run_id)) for _ in range(40)]
            catalogue: asyncio.Task[httpx.Response] | None = None
            try:
                await wait_for_event(entered)
                await wait_for_event(prepared)
                catalogue = asyncio.create_task(client.get('/api/v1/baskets'))
                completed, _ = await asyncio.wait({catalogue}, timeout=1)
                assert catalogue in completed, f'Catalogue blocked by {limiter.borrowed_tokens} borrowed worker tokens'
                assert catalogue.result().status_code == 200
                assert all(not reader.done() for reader in readers)
                assert restore_calls == 1
            finally:
                release.set()
                pending = [*readers, *([catalogue] if catalogue is not None else [])]
                results = await asyncio.wait_for(asyncio.gather(*pending), timeout=10)
            assert all(response.status_code == 200 for response in results)
            assert limiter.borrowed_tokens == 0
        assert service.files.readers == {}

    asyncio.run(exercise())


def stall_restore(
    service: LocalViewerService,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Delay real running work while HTTP waiters cancel or reach their deadlines."""
    entered, release = threading.Event(), threading.Event()
    original = service._restore

    def restore(record: LocalRecord) -> None:
        """Keep an active worker occupied until the test drains all reader outcomes."""
        entered.set()
        assert release.wait(15)
        original(record)

    monkeypatch.setattr(service, '_restore', restore)
    return entered, release


def test_cancelled_reader_does_not_cancel_shared_restore(
    cold_run: ColdRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling one HTTP reader leaves another reader's shared running job intact."""
    app, service, run_id = cold_run
    entered, release = stall_restore(service, monkeypatch)

    async def exercise() -> None:
        """Cancel one registered reader while another still depends on the same job."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            first = asyncio.create_task(client.get('/api/v1/runs/' + run_id))
            second = asyncio.create_task(client.get('/api/v1/runs/' + run_id))
            try:
                await wait_for_event(entered)
                async with asyncio.timeout(5):
                    while service._restore_queue.snapshot()['waiters'] != 2:
                        await asyncio.sleep(0.01)
                future = service._restore_queue.futures()[0]
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                assert not future.cancelled()
                assert not second.done()
                assert service._restore_queue.snapshot()['waiters'] == 1
                assert service.files.readers == {}
            finally:
                release.set()
                results = await asyncio.wait_for(
                    asyncio.gather(first, second, return_exceptions=True),
                    timeout=10,
                )
            assert isinstance(results[0], asyncio.CancelledError)
            assert isinstance(results[1], httpx.Response) and results[1].status_code == 200
            assert service.files.readers == {}
            assert service._restore_queue.futures() == ()

    asyncio.run(exercise())


def test_cold_timeout_keeps_running_restore_available_for_later_readers(
    cold_run: ColdRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP 504 releases its waiter while already running storage I/O remains counted."""
    app, service, run_id = cold_run
    service.config = replace(service.config, cold_read_timeout_seconds=1)
    entered, release = stall_restore(service, monkeypatch)

    async def exercise() -> None:
        """Finish active restoration after the timed-out response and read the warm version."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            request = asyncio.create_task(client.get('/api/v1/runs/' + run_id))
            try:
                await wait_for_event(entered)
                future = service._restore_queue.futures()[0]
                response = await asyncio.wait_for(asyncio.shield(request), timeout=5)
                assert response.status_code == 504
                assert response.json()['error']['code'] == 'cold_read_timeout'
                assert not future.cancelled()
                assert service._restore_queue.snapshot()['active'] == 1
                assert service._restore_queue.snapshot()['waiters'] == 0
                assert service.files.readers == {}
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), timeout=10)
                await asyncio.wait_for(
                    asyncio.gather(*(asyncio.wrap_future(item) for item in service._restore_queue.futures())),
                    timeout=10,
                )
            response = await client.get('/api/v1/runs/' + run_id)
            assert response.status_code == 200
            assert service.files.readers == {}
            assert service._restore_queue.futures() == ()

    asyncio.run(exercise())


def test_cancelled_warm_preparation_cannot_leak_a_late_pin(
    cold_run: ColdRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preparation thread finishing after HTTP cancellation never owns a file lease."""
    app, service, run_id = cold_run
    lease = service.acquire_read(run_id)
    lease.close(False)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = service._prepare_read

    def prepare(
        rid: str,
        expected: tuple[int, str] | None,
        ticket: RestoreTicket[LocalRecord],
    ) -> tuple[LocalRecord, Future[None] | None]:
        """Pause the short worker before returning its warm version selection."""
        selected = original(rid, expected, ticket)
        entered.set()
        try:
            assert release.wait(15)
            return selected
        finally:
            finished.set()

    monkeypatch.setattr(service, '_prepare_read', prepare)

    async def exercise() -> None:
        """Cancel before preparation returns, then let the worker finish without a pin."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            request = asyncio.create_task(client.get('/api/v1/runs/' + run_id))
            try:
                await wait_for_event(entered)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            finally:
                release.set()
                await wait_for_event(finished)
                await asyncio.gather(request, return_exceptions=True)
            assert service.files.readers == {}

    asyncio.run(exercise())


@pytest.mark.parametrize('update_catalogue', [False, True])
def test_warm_version_evicted_during_handoff_is_restored_in_same_request(
    cold_run: ColdRun,
    monkeypatch: pytest.MonkeyPatch,
    update_catalogue: bool,
) -> None:
    """Eviction after warm preparation must resume cold loading instead of returning 503."""
    app, service, run_id = cold_run
    lease = service.acquire_read(run_id)
    lease.close(False)
    original = service._prepare_read
    preparations = 0

    def prepare(
        rid: str,
        expected: tuple[int, str] | None,
        ticket: RestoreTicket[LocalRecord],
    ) -> tuple[LocalRecord, Future[None] | None]:
        """Remove the first warm selection before the async caller can pin it."""
        nonlocal preparations
        selected, future = original(rid, expected, ticket)
        preparations += 1
        if preparations == 1:
            assert future is None and selected.local_path is not None
            assert service.files.evict(Path(selected.local_path))
            if update_catalogue:
                assert service.repository.update_local_state(
                    rid,
                    generation=selected.generation,
                    local_path=None,
                    cache_state='cold',
                    cache_bytes=0,
                )
        return selected, future

    monkeypatch.setattr(service, '_prepare_read', prepare)

    async def exercise() -> None:
        """Keep the original GET open through the raced eviction and recovery."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v1/runs/' + run_id)
        assert response.status_code == 200, response.text
        assert preparations == 2
        assert service.files.readers == {}

    asyncio.run(exercise())


def test_eviction_retry_cannot_switch_to_an_overwritten_generation(
    cold_run: ColdRun,
    local_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication replacing an evicted selection still requires a new HTTP request."""
    app, service, run_id = cold_run
    # Only HTTP reader leases belong to this assertion; backup pins the new generation independently.
    assert service.backups.close(5)
    lease = service.acquire_read(run_id)
    lease.close(False)
    original_prepare, original_pin = service._prepare_read, service._pin_read
    preparations = 0
    pin_attempts = 0

    def prepare(
        rid: str,
        expected: tuple[int, str] | None,
        ticket: RestoreTicket[LocalRecord],
    ) -> tuple[LocalRecord, Future[None] | None]:
        """Commit a replacement after the first failed pin and before retry selection."""
        nonlocal preparations
        preparations += 1
        if preparations == 2:
            response, _, _, _ = local_upload(
                local_client,
                tmp_path,
                run_id=run_id,
                overwrite=True,
            )
            assert response.status_code == 200, response.text
        return original_prepare(rid, expected, ticket)

    def pin(selected: LocalRecord) -> ReadLease:
        """Evict the first selection before its atomic pin check."""
        nonlocal pin_attempts
        pin_attempts += 1
        if pin_attempts == 1:
            assert selected.local_path is not None
            assert service.files.evict(Path(selected.local_path))
            assert service.repository.update_local_state(
                run_id,
                generation=selected.generation,
                local_path=None,
                cache_state='cold',
                cache_bytes=0,
            )
        return original_pin(selected)

    monkeypatch.setattr(service, '_prepare_read', prepare)
    monkeypatch.setattr(service, '_pin_read', pin)

    async def exercise() -> None:
        """Reject the stale generation instead of returning the replacement run."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v1/runs/' + run_id)
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'run_changed'
        assert service.files.readers == {}

    asyncio.run(exercise())
