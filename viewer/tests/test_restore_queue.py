"""Cold restoration admission remains bounded after readers leave or time out."""

import asyncio
import json
import threading
import uuid
from concurrent.futures import Future, wait
from pathlib import Path

import pytest
from test_local_service import require_record
from test_service import publish
from viewer.config import Config, Limits
from viewer.errors import ViewerError
from viewer.local_repository import LocalRecord
from viewer.local_restores import RestoreTicket
from viewer.local_service import LocalViewerService
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore


def bounded_service(root: Path) -> LocalViewerService:
    """Bind every fixture component to small archive and extraction budgets before startup."""
    config = Config(
        runtime=root / 'runtime',
        storage='mock',
        publication_storage='local',
        cold_read_timeout_seconds=1,
        disk_reserve_bytes=0,
        limits=Limits(archive_bytes=1 << 20, unpacked_bytes=1 << 20, staging_free_bytes=0),
    )
    database = Database(config.runtime)
    database.create_user('alice', 'secret')
    return LocalViewerService(config, MemoryStore(), database)


def make_cold_records(viewer: LocalViewerService, root: Path, count: int) -> list[LocalRecord]:
    """Publish, back up and evict independent fixture generations for queue admission."""
    records = []
    for number in range(count):
        directory = root / f'publication-{number}'
        directory.mkdir()
        report, _, _ = publish(viewer, directory, run_id=str(uuid.uuid4()))
        rid = report['run']['id']
        viewer.activate_backup(rid)
        assert viewer.backups.run_once()
        record = require_record(viewer, rid)
        assert record.local_path is not None
        assert viewer.files.evict(record.local_path)
        assert viewer.repository.update_local_state(
            rid,
            generation=record.generation,
            local_path=None,
            cache_state='cold',
            cache_bytes=0,
        )
        records.append(require_record(viewer, rid))
    return records


def test_timed_out_readers_remove_work_that_has_not_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two running restores remain counted, but four abandoned queued jobs disappear."""
    viewer = bounded_service(tmp_path)
    run_ids = [record.run_id for record in make_cold_records(viewer, tmp_path, 6)]
    release = threading.Event()
    original = viewer._restore
    started: list[str] = []

    def restore(record: LocalRecord) -> None:
        """Hold both worker slots while queued readers exhaust their deadlines."""
        started.append(record.run_id)
        assert release.wait(15)
        original(record)

    monkeypatch.setattr(viewer, '_restore', restore)

    async def exercise() -> None:
        """Drain every blocked worker even when the bounded-admission assertion fails."""
        readers = [asyncio.create_task(viewer.acquire_read_async(rid)) for rid in run_ids]
        try:
            results = await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 5)
            assert all(isinstance(value, ViewerError) and value.code == 'cold_read_timeout' for value in results)
            assert len(started) == 2
            assert viewer._restore_queue.snapshot() == {'active': 2, 'queued': 0, 'waiters': 0, 'capacity': 6}
        finally:
            release.set()
            await asyncio.gather(*readers, return_exceptions=True)
            _, pending = await asyncio.to_thread(wait, viewer._restore_queue.futures(), timeout=10)
            assert not pending
            assert len(started) == 2
            assert viewer._restore_queue.futures() == ()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        assert viewer.close()
        viewer.db.close()


@pytest.mark.parametrize('phase', ['before', 'after'])
@pytest.mark.parametrize('outcome', ['cancel', 'timeout'])
def test_late_prepare_releases_caller_owned_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    outcome: str,
) -> None:
    """A reader leaving during preparation cannot leak a queued job or admit one later."""
    viewer = bounded_service(tmp_path)
    records = make_cold_records(viewer, tmp_path, 3)
    running, prepare_entered, prepared = threading.Event(), threading.Event(), threading.Event()
    release_running, release_prepare = threading.Event(), threading.Event()
    original_restore, original_prepare = viewer._restore, viewer._prepare_read
    started: list[str] = []

    def restore(record: LocalRecord) -> None:
        """Occupy both worker slots while the third identity remains queued."""
        started.append(record.run_id)
        if len(started) >= 2:
            running.set()
        assert release_running.wait(15)
        original_restore(record)

    def prepare(
        rid: str,
        expected: tuple[int, str] | None,
        ticket: RestoreTicket[LocalRecord],
    ) -> tuple[LocalRecord, Future[None] | None]:
        """Pause before or after registering the caller-owned restore ticket."""
        try:
            result = original_prepare(rid, expected, ticket) if phase == 'after' else None
            prepare_entered.set()
            assert release_prepare.wait(10)
            return result if result is not None else original_prepare(rid, expected, ticket)
        finally:
            prepared.set()

    monkeypatch.setattr(viewer, '_restore', restore)
    tickets = [viewer._restore_queue.ticket() for _ in range(2)]
    for ticket, record in zip(tickets, records[:2], strict=True):
        viewer._restore_queue.join(ticket, (record.run_id, record.generation, record.archive_sha256), record)
    assert running.wait(5)
    monkeypatch.setattr(viewer, '_prepare_read', prepare)

    async def exercise() -> None:
        """Leave while a prepare thread is delayed, then drain every worker in finally."""
        request = asyncio.create_task(viewer.acquire_read_async(records[2].run_id))
        try:
            await wait_for_signal(prepare_entered)
            if outcome == 'cancel':
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                with pytest.raises(ViewerError) as timeout:
                    await asyncio.wait_for(asyncio.shield(request), timeout=5)
                assert timeout.value.code == 'cold_read_timeout'
            assert viewer._restore_queue.snapshot() == {'active': 2, 'queued': 0, 'waiters': 2, 'capacity': 6}
            release_prepare.set()
            await wait_for_signal(prepared)
            assert viewer._restore_queue.snapshot()['queued'] == 0
        finally:
            release_prepare.set()
            release_running.set()
            await wait_for_signal(prepared)
            await asyncio.gather(request, return_exceptions=True)
            for ticket in tickets:
                viewer._restore_queue.release(ticket)
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.wrap_future(item) for item in viewer._restore_queue.futures())),
                timeout=10,
            )
        assert records[2].run_id not in started
        assert viewer._restore_queue.futures() == ()
        assert viewer.files.readers == {}

    try:
        asyncio.run(exercise())
    finally:
        release_prepare.set()
        release_running.set()
        assert viewer.close()
        viewer.db.close()


async def wait_for_signal(signal: threading.Event) -> None:
    """Observe a background milestone without consuming an HTTP worker token."""
    async with asyncio.timeout(5):
        while not signal.is_set():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize('value', [-1, True, 1.5, '4', None])
def test_restore_queue_limit_rejects_invalid_json(tmp_path: Path, value: object) -> None:
    """Restore admission accepts only an explicitly non-negative integer queue limit."""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'storage': 'mock', 'restore_queue_limit': value}))
    with pytest.raises(ViewerError) as error:
        Config.load(path)
    assert error.value.code == 'invalid_config'


@pytest.mark.parametrize('value', [0, 4])
def test_restore_queue_limit_loads_zero_and_default(tmp_path: Path, value: int) -> None:
    """Operators may disable extra queue slots or retain the documented default."""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'storage': 'mock', 'restore_queue_limit': value}))
    assert Config.load(path).restore_queue_limit == value
    assert Config(storage='mock').restore_queue_limit == 4
