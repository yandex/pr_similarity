"""Local publication, durable catalogue and on-demand archive restoration."""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import cast

from anyio.to_thread import run_sync
from viewer.bundles import (
    BundleDescriptor,
    BundleIdentity,
    BundleRepository,
    DownloadedBundle,
)
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_checkpoint import CheckpointRepository, ensure_registry
from viewer.local_files import (
    LocalFiles,
    VersionStore,
    atomic_json,
    fsync_ancestors,
    fsync_directory,
)
from viewer.local_jobs import BackupWorkers
from viewer.local_refresh import RefreshAudit
from viewer.local_repository import BackupJob, LocalRecord, LocalRepository
from viewer.local_restores import RestoreQueue, RestoreTicket
from viewer.local_retention import LocalRetention
from viewer.models import Report
from viewer.persistence.database import Actor, Database
from viewer.persistence.storage import Store, StoredBody
from viewer.publication import CatalogueEntry, PublicationReader
from viewer.service import ViewerService
from viewer.util import digest, json_bytes, parse_json, safe_path, utc_now
from viewer.validation.archive import ImportedRun, validate_archive
from viewer.validation.contracts import require, validate_report

PROTOCOL = 'local-durable-v1'
_MESSAGES = {
    'receiving': 'Uploading the archive to the server…',
    'extracting': 'Unpacking the run into local storage…',
    'validating': 'Verifying run integrity and data contracts…',
    'committing': 'Saving the publication on the server…',
    'available': 'Run published: data unpacked, verified and saved locally on the server.',
    'rejected': 'The server could not publish this run.',
}


class ReadLease:
    """Pin exactly one publication for a complete HTTP response lifetime."""

    def __init__(self, service: 'LocalViewerService', record: LocalRecord) -> None:
        """Pin the selected immutable local version for the lifetime of one request."""
        self.service, self.record = service, record
        if record.local_path is None:
            raise ViewerError('run_cache_miss', 'Run data is not available locally.', 503)
        self.path = Path(record.local_path)
        self.closed = False
        self.service.files.pin(self.path)

    def close(self, success: bool = True) -> None:
        """Release the reader pin once and record access only after a successful response."""
        if not self.closed:
            self.closed = True
            try:
                if success:
                    self.service.repository.touch_access(
                        self.record.run_id,
                        self.record.generation,
                        self.service.db.clock(),
                    )
            finally:
                self.service.files.unpin(self.path)


class LocalReadStore(Store):
    """Resolve existing logical publication keys inside the request's version."""

    def __init__(self, service: 'LocalViewerService') -> None:
        """Resolve publication reads through the service's current or request-pinned record."""
        self.service = service

    def open(self, key: str) -> StoredBody:
        """Open a logical publication object from the selected owner-bound local version."""
        safe_path(key)
        parts = key.split('/')
        require(len(parts) >= 4 and parts[0] == 'runs', 'invalid_local_key')
        owner, rid = parts[1:3]
        record = self.service._read_record(rid)
        require(record.owner == owner, 'owner_conflict')
        if not record.local_path:
            raise ViewerError('run_cache_miss', 'Run data is not available locally.', 503)
        return VersionStore(self.service.files, Path(record.local_path), f'runs/{owner}/{rid}/').open(key)


class LocalViewerService(ViewerService):
    """Keep user requests independent of remote backup and global audit work."""

    def __init__(
        self, config: Config, store: Store, db: Database, *, initialize: bool = True, allow_legacy_import: bool = False
    ) -> None:
        """Bind durable local state, bounded workers and request-scoped immutable reads."""
        if db.operations():
            raise ViewerError('legacy_recovery_pending', 'Complete legacy recovery before enabling local storage.', 503)
        existed = db.connection.execute('SELECT 1 FROM sqlite_master WHERE name=\'local_heads\'').fetchone()
        if (
            not existed
            and db.connection.execute('SELECT count(*) FROM owners').fetchone()[0]
            and not allow_legacy_import
        ):
            raise ViewerError(
                'local_migration_required', 'Import existing publications before enabling local storage.', 503
            )
        self.remote = store
        self.allow_legacy_import = allow_legacy_import
        self._registry_lock = threading.RLock()
        self._reports: OrderedDict[tuple[str, int, str, str], tuple[Report, int]] = OrderedDict()
        self._report_bytes = 0
        self._catalogue_responses: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
        self._catalogue_response_bytes = 0
        self.files = LocalFiles(config)
        self._read_context: ContextVar[ReadLease | None] = ContextVar('viewer_local_read', default=None)
        super().__init__(config, store, db, initialize=False)
        self.repository = LocalRepository(db)
        self.store = LocalReadStore(self)
        self.bundles = BundleRepository(config, store)
        self._active: dict[str, tuple[str, str]] = {}
        self._orphan_paths: set[tuple[str, Path]] = set()
        self._upload_reservations: dict[tuple[str, str], AbstractContextManager] = {}
        self._run_locks: dict[str, threading.RLock] = {}
        self._restore_queue = RestoreQueue[LocalRecord](
            config.restore_workers,
            config.restore_queue_limit,
            lambda record: self._restore(record),
        )
        self.backups = BackupWorkers(config, self.repository, self._backup)
        self.audit = RefreshAudit(
            config,
            self.repository,
            self.bundles,
            self._apply_bundle,
            busy=lambda rid: rid in self.busy_runs(),
            apply_tombstone=self._apply_tombstone,
            start_worker=False,
            run_lock=self._run_lock,
            reserve_download=lambda descriptor: self.files.reserve(
                2 * (descriptor.archive_size + self.config.limits.unpacked_bytes), pending_limit=False
            ),
        )
        self._checkpoint_thread: threading.Thread | None = None
        self.checkpoint_job: dict = self.repository.load_refresh('checkpoint-active') or {
            'status': 'idle',
            'request_id': None,
        }
        checkpoint_start = self.checkpoint_job.get('started_at')
        schedule_origin = self.checkpoint_job.get('schedule_origin')
        if schedule_origin is None:
            schedule_origin = (
                datetime.fromisoformat(checkpoint_start).timestamp() if checkpoint_start else self.db.clock()
            )
            self.checkpoint_job['schedule_origin'] = schedule_origin
            self.repository.save_refresh('checkpoint-active', self.checkpoint_job)
        self._last_checkpoint: float = float(schedule_origin)
        self._stop = threading.Event()
        self._housekeeping: threading.Thread | None = None
        self._audit_thread: threading.Thread | None = None
        self.recovery = {'status': 'idle', 'error_code': None}
        self._staging_cleaned = False
        if initialize:
            self.initialize()

    def _run_lock(self, rid: str) -> threading.RLock:
        """Return the shared per-run lock used to serialize its mutations and restoration."""
        with self.lock:
            return self._run_locks.setdefault(rid, threading.RLock())

    def initialize(self) -> None:
        """Recover local intent state and publish a catalogue snapshot without contacting S3."""
        migration = self.repository.load_refresh('legacy-import')
        if migration and migration.get('status') != 'completed' and not self.allow_legacy_import:
            raise ViewerError(
                'migration_incomplete', 'Complete the local storage import before starting the server.', 503
            )
        self.repository.recover_jobs()
        with self.lock:
            self._recovery_operations = [
                dict(row)
                for row in self.db.connection.execute('SELECT actor,key,data FROM local_operations')
                if json.loads(row['data']).get('phase') not in ('available', 'rejected')
            ]
            for row in self._recovery_operations:
                self._active[json.loads(row['data'])['run_id']] = (row['actor'], row['key'])
        self._sync_snapshot()
        if not self._staging_cleaned:
            self.cleanup_staging()
            self._cleanup_local_staging()
            self._staging_cleaned = True
        self.startup = {'status': 'ready', 'phase': 'ready', 'error_code': None}

    def _cleanup_local_staging(self) -> None:
        """Discard owned crash leftovers before workers or requests can create new ones."""
        roots = (
            (
                self.files.temporary,
                r'(?:version|restore|recover)-[a-z0-9_]{8}|evicted-[0-9a-f-]{36}',
            ),
            (self.config.runtime / 'refresh-staging', r'audit-[a-z0-9_]{8}'),
        )
        for root, pattern in roots:
            if root.is_symlink() or not root.is_dir():
                continue
            for child in root.iterdir():
                if re.fullmatch(pattern, child.name) and not child.is_symlink() and child.is_dir():
                    shutil.rmtree(child)

    def _sync_snapshot(self) -> None:
        """Replace the in-memory catalogue with the current durable local heads."""
        with self.lock:
            self.snapshot = self.repository.load_snapshot()

    def start_background(self) -> None:
        """Start workers once, preserving restored-runtime write barriers and pending audits."""
        if self._background_started:
            return
        self._background_started = True
        if self.startup['status'] != 'ready':
            self.initialize()
        self.backups.start()
        self._housekeeping = threading.Thread(target=self._maintain_local, daemon=True, name='viewer-local-maintenance')
        self._housekeeping.start()
        if (
            self.checkpoint_job.get('status') in ('pending', 'running')
            and self.repository.restore_reconciliation()['status'] != 'required'
        ):
            self._start_checkpoint()
        if self.audit.status()['status'] in ('pending', 'running'):
            self._start_audit()

    def close(self, timeout: float | None = None) -> bool:
        """Stop maintenance and workers within budget while respecting active readers and restores."""
        seconds = self.config.shutdown_seconds if timeout is None else timeout
        deadline = time.monotonic() + seconds
        self.maintenance.stopping = True
        self.maintenance.stop.set()
        self._stop.set()
        self._restore_queue.close()
        backups_closed = self.backups.close(max(0, deadline - time.monotonic()))
        if self._housekeeping:
            self._housekeeping.join(max(0, deadline - time.monotonic()))
        try:
            self.audit.close(timeout=max(0, deadline - time.monotonic()))
        except ViewerError:
            return False
        if self._audit_thread:
            self._audit_thread.join(max(0, deadline - time.monotonic()))
        if self._checkpoint_thread:
            self._checkpoint_thread.join(max(0, deadline - time.monotonic()))
        with self.lock:
            restoring = bool(self._restore_queue.snapshot()['active'])
            readers = bool(self.files.readers)
        return (
            backups_closed
            and not restoring
            and not readers
            and not (self._housekeeping and self._housekeeping.is_alive())
            and not (self._audit_thread and self._audit_thread.is_alive())
            and not (self._checkpoint_thread and self._checkpoint_thread.is_alive())
        )

    def catalogue_response(self, key: tuple, build: Callable[[], dict]) -> dict:
        """Cache bounded public projections by catalogue revision and admission state."""
        with self.lock:
            identity = (self.snapshot.revision, tuple(sorted(self._active)), *key)
            previous = self._catalogue_responses.get(identity)
            if previous:
                self._catalogue_responses.move_to_end(identity)
                return previous[0]
            result = build()
            size = len(json_bytes(result))
            if size <= self.config.catalogue_cache_bytes:
                self._catalogue_responses[identity] = (result, size)
                self._catalogue_response_bytes += size
                while (
                    self._catalogue_response_bytes > self.config.catalogue_cache_bytes
                    or len(self._catalogue_responses) > self.config.catalogue_cache_entries
                ):
                    _, (_, removed) = self._catalogue_responses.popitem(last=False)
                    self._catalogue_response_bytes -= removed
            return result

    def read_is_pinned(self) -> bool:
        """Report whether the current request already owns an immutable read lease."""
        lease = self._read_context.get()
        return lease is not None and not lease.closed

    def busy_runs(self) -> set[str]:
        """Snapshot run identities currently reserved by admitted publication operations."""
        with self.lock:
            return set(self._active)

    def check_write_admission(self, *, allow_restore_reconciliation: bool = False) -> None:
        """Allow recovery reads and audits while fencing restored mutation identities."""
        if self.maintenance.stopping:
            raise ViewerError('service_stopping', 'Service is stopping.', 503)
        if not allow_restore_reconciliation:
            self.repository.require_reconciled()

    def _require_available(self, rid: str) -> None:
        """Allow pinned reads and reject unpinned reads of busy or quarantined runs."""
        lease = self._read_context.get()
        if lease and not lease.closed and lease.record.run_id == rid:
            return
        if rid in self.busy_runs():
            raise ViewerError('run_updating', 'Run is being updated; retry later.', 503)
        if rid in self.snapshot.quarantined:
            raise ViewerError('local_version_unavailable', 'This publication has no verified available copy.', 503)

    def _require_run(self, rid: str) -> CatalogueEntry:
        """Return the request-pinned catalogue entry before consulting the current head."""
        lease = self._read_context.get()
        if lease and not lease.closed and lease.record.run_id == rid:
            return lease.record.entry
        return super()._require_run(rid)

    def _read_record(self, rid: str) -> LocalRecord:
        """Select the pinned generation or require an available current local record."""
        lease = self._read_context.get()
        if lease and not lease.closed and lease.record.run_id == rid:
            return lease.record
        self._require_available(rid)
        record = self.repository.get(rid)
        if record is None:
            raise ViewerError('not_found', 'Run not found.', 404)
        return record

    @property
    def metadata(self) -> dict:
        """Preserve the selected version's metadata while serving a pinned request."""
        lease = self._read_context.get()
        if lease and not lease.closed:
            return {**self.snapshot.metadata, lease.record.run_id: lease.record.metadata}
        return self.snapshot.metadata

    @contextmanager
    def bind_read(self, lease: ReadLease) -> Iterator[None]:
        """Propagate one immutable lease through the request context and restore the prior context."""
        token = self._read_context.set(lease)
        try:
            yield
        finally:
            self._read_context.reset(token)

    def _identity(self, record: LocalRecord, *, confirmed: bool = False) -> BundleIdentity:
        """Build the requested or confirmed backup identity for one local generation."""
        revision = record.backup_metadata_revision if confirmed else record.metadata_revision
        return BundleIdentity(record.owner, record.run_id, record.generation, record.archive_sha256, revision)

    def _prepare_read(
        self,
        rid: str,
        expected: tuple[int, str] | None,
        ticket: RestoreTicket[LocalRecord],
    ) -> tuple[LocalRecord, Future[None] | None]:
        """Select an immutable version and schedule its shared restore without pinning it."""
        with self.lock:
            self.check_write_admission(allow_restore_reconciliation=True)
            record = self._read_record(rid)
            if expected is not None and (record.generation, record.archive_sha256) != expected:
                raise ViewerError(
                    'run_changed',
                    'Run changed while loading; retry the request.',
                    409,
                )
            if record.local_path and Path(record.local_path).is_dir():
                return record, None
            if not record.backup_valid or not record.backup_metadata_revision:
                raise ViewerError(
                    'run_unavailable',
                    'No verified archive is available for this run.',
                    503,
                )
            identity = (rid, record.generation, record.archive_sha256)
        return record, self._restore_queue.join(ticket, identity, record)

    def _pin_read(self, selected: LocalRecord) -> ReadLease:
        """Recheck the selected generation and pin it without an asynchronous handoff."""
        with self.lock:
            self.check_write_admission(allow_restore_reconciliation=True)
            self._require_available(selected.run_id)
            current = self.repository.get(selected.run_id)
            if current is None:
                raise ViewerError('not_found', 'Run not found.', 404)
            if (current.generation, current.archive_sha256) != (
                selected.generation,
                selected.archive_sha256,
            ):
                raise ViewerError(
                    'run_changed',
                    'Run changed while loading; retry the request.',
                    409,
                )
            return ReadLease(self, current)

    def acquire_read(self, rid: str) -> ReadLease:
        """Wait synchronously through raced eviction within one original deadline."""
        deadline = time.monotonic() + self.config.cold_read_timeout_seconds
        expected: tuple[int, str] | None = None
        while True:
            ticket = self._restore_queue.ticket()
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                selected, future = self._prepare_read(rid, expected, ticket)
                if expected is None:
                    expected = (selected.generation, selected.archive_sha256)
                if future is not None:
                    future.result(timeout=max(0, deadline - time.monotonic()))
            except TimeoutError:
                raise ViewerError(
                    'cold_read_timeout',
                    'Run loading exceeded the request deadline.',
                    504,
                ) from None
            finally:
                self._restore_queue.release(ticket)
            try:
                return self._pin_read(selected)
            except ViewerError as error:
                if error.code != 'run_cache_miss':
                    raise

    async def acquire_read_async(self, rid: str) -> ReadLease:
        """Await shared restoration without retaining an HTTP worker thread or file pin."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.cold_read_timeout_seconds
        expected: tuple[int, str] | None = None
        while True:
            ticket = self._restore_queue.ticket()
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    selected, future = await run_sync(self._prepare_read, rid, expected, ticket)
                    if expected is None:
                        expected = (selected.generation, selected.archive_sha256)
                    if future is not None:
                        wrapped = asyncio.wrap_future(future)

                        def consume_result(completed: asyncio.Future[None]) -> None:
                            """Retrieve late errors even when this request stopped waiting."""
                            if not completed.cancelled():
                                completed.exception()

                        wrapped.add_done_callback(consume_result)
                        await asyncio.shield(wrapped)
            except TimeoutError:
                raise ViewerError(
                    'cold_read_timeout',
                    'Run loading exceeded the request deadline.',
                    504,
                ) from None
            finally:
                self._restore_queue.release(ticket)
            try:
                return self._pin_read(selected)
            except ViewerError as error:
                if error.code != 'run_cache_miss':
                    raise

    def _restore(self, record: LocalRecord) -> None:
        """Verify and install one cold generation without replacing a newer local head."""
        identity = self._identity(record, confirmed=True)
        with self._run_lock(record.run_id):
            current = self.repository.get(record.run_id)
            if not current or current.generation != record.generation:
                raise ViewerError('run_changed', 'Run changed while loading.', 409)
            if current.local_path and Path(current.local_path).is_dir():
                return
            with self.files.reserve(
                2 * (record.entry['marker']['archive_size'] + self.config.limits.unpacked_bytes), pending_limit=False
            ):
                with tempfile.TemporaryDirectory(dir=self.files.temporary, prefix='restore-') as temporary:
                    descriptor = self.bundles.read_bundle(identity)
                    downloaded = self.bundles.download_bundle(descriptor, Path(temporary) / 'bundle')
                    path = self.files.version_path(identity)
                    if path.exists() and not self.files.evict(path):
                        raise ViewerError('run_updating', 'An earlier reader is still using this version.', 503)
                    path = self.files.install(downloaded.imported, identity, downloaded.marker)
                    self.repository.update_local_state(
                        record.run_id,
                        generation=record.generation,
                        local_path=str(path),
                        cache_state='ready',
                        cache_bytes=self.files.size(path),
                    )

    def full_report(self, rid: str) -> Report:
        """Reuse validated reports within a bounded cache of immutable local versions."""
        self._require_available(rid)
        record = self._read_record(rid)
        expected = record.entry['marker']['report_sha256']
        identity = (rid, record.generation, record.archive_sha256, str(record.local_path))
        with self.lock:
            cached = self._reports.get(identity)
            if cached is not None:
                self._reports.move_to_end(identity)
                return cached[0]
        content = self.store.get(record.entry['prefix'] + 'data/report.json', self.config.limits.json_bytes)
        require(digest(content) == expected, 'stored_digest_mismatch')
        report = validate_report(parse_json(content))
        size = len(content)
        if size <= self.config.report_cache_bytes:
            with self.lock:
                previous = self._reports.pop(identity, None)
                if previous:
                    self._report_bytes -= previous[1]
                self._reports[identity] = (report, size)
                self._report_bytes += size
                while (
                    self._report_bytes > self.config.report_cache_bytes
                    or len(self._reports) > self.config.report_cache_entries
                ):
                    _, (_, removed) = self._reports.popitem(last=False)
                    self._report_bytes -= removed
        return report

    def _fingerprint(
        self, actor: str, rid: str, archive: str, report: str, overwrite: bool, admin: bool, expected: str | None
    ) -> str:
        """Bind publication idempotency to actor, bytes, operation intent and service origin."""
        return digest(
            json_bytes(
                dict(
                    actor=actor,
                    run_id=rid,
                    archive_sha256=archive,
                    report_sha256=report,
                    overwrite=overwrite,
                    admin_overwrite=admin,
                    expected_publication_sha256=expected,
                    protocol=PROTOCOL,
                    server=self.config.public_origin,
                )
            )
        )

    def _public_status(self, operation: dict) -> dict:
        """Project client-visible publication progress without exposing internal storage state."""
        receipt = operation.get('receipt')
        return {
            key: operation.get(key, receipt.get(key) if receipt else None)
            for key in ('operation_id', 'run_id', 'archive_sha256', 'report_sha256', 'phase', 'message', 'receipt')
        }

    def publication_status(self, actor: str, key: str, *, run_id: str, archive_sha256: str, report_sha256: str) -> dict:
        """Return saved progress only for the actor and exact expected publication hashes."""
        operation = self.repository.operation(actor, key)
        if operation is None:
            raise ViewerError('not_found', 'Publication operation not found.', 404)
        for field, expected in (
            ('run_id', run_id),
            ('archive_sha256', archive_sha256),
            ('report_sha256', report_sha256),
        ):
            actual = operation.get(field) or (operation.get('receipt') or {}).get(field)
            if actual != expected:
                raise ViewerError('idempotency_conflict', 'Operation key identifies different content.', 409)
        return self._public_status(operation)

    def admit_publication(
        self,
        actor: str,
        key: str,
        rid: str,
        archive_sha256: str,
        report_sha256: str,
        *,
        overwrite: bool = False,
        admin: bool = False,
        expected_publication_sha256: str | None = None,
        grant_token: str | None = None,
        reserve_upload: bool = False,
    ) -> dict:
        """Authorize and reserve a publication before durable progress or uploaded bytes are accepted."""
        fingerprint = self._fingerprint(
            actor, rid, archive_sha256, report_sha256, overwrite, admin, expected_publication_sha256
        )
        with self._run_lock(rid), self.lock:
            self.check_write_admission()
            person = self.db.actor(actor)
            if person is None or (admin and person['role'] != 'admin'):
                raise ViewerError('admin_denied' if admin else 'auth_required', 'Publication is not authorized.', 403)
            previous = self.repository.operation(actor, key)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ViewerError('idempotency_conflict', 'Operation key identifies different content.', 409)
                if previous.get('phase') == 'available':
                    if self.repository.get(rid) is None:
                        raise ViewerError('publication_removed', 'Original publication was removed.', 404)
                    self._require_available(rid)
                    return self._public_status(previous)
                if rid in self._active:
                    raise ViewerError('publication_in_progress', 'Publication is already in progress.', 503)
            if rid in self._active:
                raise ViewerError('run_updating', 'Another operation is updating this run.', 503)
            current = self.repository.get(rid, include_deleted=True)
            owner = self.db.owner(rid)
            if admin:
                if not overwrite or not current or current.deleted:
                    raise ViewerError(
                        'invalid_admin_overwrite', 'Administrative overwrite requires an existing run.', 409
                    )
                if current.archive_sha256 != expected_publication_sha256:
                    raise ViewerError('run_changed', 'Run changed; obtain its current publication hash.', 409)
            elif owner and owner['login'] != actor:
                raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
            if current and not current.deleted and not overwrite:
                raise ViewerError('run_exists', 'Run exists; use explicit overwrite.', 409)
            operation = dict(
                operation_id=(previous or {}).get('operation_id') or str(uuid.uuid4()),
                actor_login=actor,
                run_id=rid,
                archive_sha256=archive_sha256,
                report_sha256=report_sha256,
                overwrite=overwrite,
                admin=admin,
                expected_publication_sha256=expected_publication_sha256,
                owner=current.owner if admin and current is not None else actor,
                phase='receiving',
                message=_MESSAGES['receiving'],
                grant_hash=digest(grant_token.encode()) if grant_token else None,
                receipt=None,
            )
            reservation = None
            if reserve_upload:
                peak = 3 * self.config.limits.archive_bytes + 2 * self.config.limits.unpacked_bytes
                reservation = self.files.reserve(peak, pending_limit=False)
                reservation.__enter__()
            persisted = False
            try:
                with self.db.connection:
                    self.db.connection.execute('BEGIN IMMEDIATE')
                    if grant_token:
                        purpose = 'admin-overwrite' if admin else 'publish'
                        if self.db.authorize(grant_token, purpose) != actor:
                            raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
                        consumed = self.db.connection.execute(
                            'UPDATE grants SET consumed=1 WHERE token_hash=? AND consumed=0',
                            (operation['grant_hash'],),
                        )
                        if consumed.rowcount != 1:
                            raise ViewerError('invalid_grant', 'Grant is already consumed.', 401)
                    self.db.connection.execute(
                        'INSERT OR REPLACE INTO local_operations VALUES (?,?,?,?)',
                        (actor, key, fingerprint, json.dumps(operation)),
                    )
                persisted = True
            finally:
                if not persisted and reservation is not None:
                    reservation.__exit__(None, None, None)
            if reservation is not None:
                self._upload_reservations[(actor, key)] = reservation
            self._active[rid] = (actor, key)
            return self._public_status(operation)

    def release_upload(self, actor: str, key: str) -> None:
        """Release the admission-time disk reservation once its request no longer owns it."""
        with self.lock:
            reservation = self._upload_reservations.pop((actor, key), None)
        if reservation is not None:
            reservation.__exit__(None, None, None)

    def publication_phase(self, actor: str, key: str, phase: str, *, archive_path: str | None = None) -> None:
        """Persist publication progress and durably retain accepted upload bytes when supplied."""
        require(phase in _MESSAGES, 'invalid_phase')
        operation = self.repository.operation(actor, key)
        if operation is None:
            raise ViewerError('invalid_operation', 'Publication operation is missing.', 409)
        if archive_path is not None:
            target = self.config.runtime / 'local-uploads' / operation['operation_id'] / 'archive.tar.gz'
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(archive_path, target)
            os.chmod(target, 0o600)
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
            fsync_ancestors(target.parent, self.config.runtime)
            operation['archive_path'] = str(target)
        operation.update(phase=phase, message=_MESSAGES[phase])
        self.repository.set_operation(actor, key, operation['fingerprint'], operation)

    def _cleanup_upload(self, operation: dict) -> None:
        """Remove terminal staging bytes only within the server-owned upload root."""
        target = self.config.runtime / 'local-uploads' / operation['operation_id']
        try:
            shutil.rmtree(target)
            fsync_directory(target.parent)
        except FileNotFoundError:
            pass
        except OSError:
            # A terminal operation stays discoverable for the next maintenance pass.
            logging.getLogger('viewer.local').warning(
                'upload_cleanup_failed operation_id=%s', operation['operation_id']
            )

    def publication_failed(self, actor: str, key: str, error: BaseException) -> None:
        """Record rejection only before commit and release terminal upload ownership."""
        with self.lock:
            operation = self.repository.operation(actor, key)
            if operation and operation.get('phase') != 'available':
                operation.update(
                    phase='rejected',
                    message=getattr(error, 'message', _MESSAGES['rejected']),
                    error_code=getattr(error, 'code', 'publication_failed'),
                )
                self.repository.set_operation(actor, key, operation['fingerprint'], operation)
        if operation:
            self._cleanup_upload(operation)
            with self.lock:
                if self._active.get(operation['run_id']) == (actor, key):
                    self._active.pop(operation['run_id'], None)

    def _marker_for(self, imported: ImportedRun, owner: str, operation_id: str) -> dict:
        """Bind the validated archive and complete inventory into a publication marker."""
        manifest = imported.directory / 'manifest.json'
        return dict(
            version=1,
            owner=owner,
            run_id=imported.report.run.id,
            basket_sha256=imported.report.basket.sha256,
            report_sha256=imported.manifest.report_sha256,
            archive_sha256=imported.archive_sha256,
            archive_size=imported.archive.stat().st_size,
            published_at=utc_now(self.db.clock()),
            operation_id=operation_id,
            inventory=[
                *(entry.to_wire() for entry in imported.manifest.files),
                dict(path='manifest.json', size_bytes=manifest.stat().st_size, sha256=digest(manifest.read_bytes())),
            ],
        )

    def _publication_operation(
        self,
        imported: ImportedRun,
        login: str,
        key: str,
        *,
        overwrite: bool,
        admin: bool,
        expected_publication_sha256: str | None,
        grant_token: str | None,
    ) -> dict:
        """Resolve admission or replay while enforcing the exact frozen publication identity."""
        rid = imported.report.run.id
        operation = self.repository.operation(login, key)
        if operation is None or operation.get('phase') == 'rejected':
            self.admit_publication(
                login,
                key,
                rid,
                imported.archive_sha256,
                imported.manifest.report_sha256,
                overwrite=overwrite,
                admin=admin,
                expected_publication_sha256=expected_publication_sha256,
                grant_token=grant_token,
            )
            operation = self.repository.operation(login, key)
        if operation is None:
            raise ViewerError('invalid_operation', 'Publication operation is missing.', 409)
        fingerprint = self._fingerprint(
            login,
            rid,
            imported.archive_sha256,
            imported.manifest.report_sha256,
            overwrite,
            admin,
            expected_publication_sha256,
        )
        if operation['fingerprint'] != fingerprint:
            raise ViewerError('idempotency_conflict', 'Operation key identifies different content.', 409)
        if operation.get('phase') == 'available':
            if self.repository.get(rid) is None:
                raise ViewerError('publication_removed', 'Original publication was removed.', 404)
            self._require_available(rid)
            return operation
        require(operation['run_id'] == rid, 'publish_identity_mismatch')
        require(
            operation['archive_sha256'] == imported.archive_sha256
            and operation['report_sha256'] == imported.manifest.report_sha256,
            'publish_identity_mismatch',
        )
        return operation

    def _publication_receipt(self, imported: ImportedRun, login: str, key: str, owner: str, operation: dict) -> dict:
        """Describe the local durability guarantee committed with this exact publication."""
        return dict(
            run_id=imported.report.run.id,
            archive_sha256=imported.archive_sha256,
            report_sha256=imported.manifest.report_sha256,
            idempotency_key=key,
            author_login=owner,
            actor_login=login,
            protocol=PROTOCOL,
            durability='local_durable',
            operation_id=operation['operation_id'],
            phase='available',
            message=_MESSAGES['available'],
        )

    def _failed_publication_install(
        self,
        login: str,
        key: str,
        rid: str,
        path: Path | None,
        error: BaseException,
    ) -> None:
        """Retain committed bytes and record cleanup work only for an uncommitted install."""
        committed = self.repository.operation(login, key)
        self.publication_failed(login, key, error)
        if path is not None and (not committed or committed.get('phase') != 'available'):
            with self._run_lock(rid):
                try:
                    self.files.evict(path)
                except OSError:
                    self._orphan_paths.add((rid, path))

    def _publication_owner(
        self,
        imported: ImportedRun,
        login: str,
        operation: dict,
        current: LocalRecord | None,
        admin: bool,
        expected_publication_sha256: str | None,
    ) -> str:
        """Recheck actor, owner, frozen basket and overwrite CAS under the caller's run lock."""
        rid = imported.report.run.id
        if self.db.actor(login) is None:
            raise ViewerError('auth_required', 'Publishing account is no longer active.', 403)
        owner = operation['owner']
        binding = self.db.owner(rid)
        if binding and binding['basket_hash'] != imported.report.basket.sha256:
            raise ViewerError('basket_conflict', 'Overwrite must retain the frozen basket.', 409)
        if admin:
            person = self.db.actor(login)
            if person is None or person['role'] != 'admin':
                raise ViewerError('admin_denied', 'Administrative overwrite is not authorized.', 403)
            if not current or current.deleted or current.archive_sha256 != expected_publication_sha256:
                raise ViewerError('run_changed', 'Run changed before overwrite.', 409)
        return owner

    def _commit_installed_publication(
        self,
        imported: ImportedRun,
        login: str,
        key: str,
        operation: dict,
        identity: BundleIdentity,
        metadata: dict,
        path: Path,
        expected_generation: int | None,
    ) -> dict:
        """Validate installed evidence and atomically expose its head, receipt and backup intent."""
        rid, owner, generation = identity.run_id, identity.owner, identity.generation
        entry = PublicationReader(self.config, VersionStore(self.files, path, f'runs/{owner}/{rid}/'))._marker(
            f'runs/{owner}/{rid}/publication.json'
        )
        receipt = self._publication_receipt(imported, login, key, owner, operation)
        record = LocalRecord(
            rid,
            owner,
            generation,
            1,
            entry,
            metadata,
            str(path),
            last_access=self.db.clock(),
            cache_bytes=self.files.size(path),
        )
        self.repository.commit(
            record,
            expected_generation=expected_generation,
            actor=login,
            idempotency_key=key,
            fingerprint=operation['fingerprint'],
            receipt=receipt,
        )
        self._sync_snapshot()
        return receipt

    def _publication_metadata(self, imported: ImportedRun, current: LocalRecord | None) -> dict:
        """Preserve owner display edits on overwrite and validate defaults for a new run."""
        metadata = (
            deepcopy(current.metadata)
            if current and not current.deleted
            else dict(
                display_name=imported.report.agent.name,
                tags=imported.report.run.tags,
                pinned=False,
                created_at=imported.report.run.created_at,
            )
        )
        self.validate_patch({'display_name': metadata['display_name'], 'tags': metadata['tags']})
        return metadata

    def publish(
        self,
        imported: ImportedRun,
        login: str,
        key: str,
        *,
        overwrite: bool = False,
        grant_token: str | None = None,
        deadline: float | None = None,
        admin: bool = False,
        expected_publication_sha256: str | None = None,
    ) -> dict:
        """Install validated files and commit the local publication before scheduling backup.

        Args:
            imported: Validated original archive and its extracted evidence.
            login: Authenticated actor; administrative overwrite preserves the original owner.
            key: Actor-scoped idempotency key bound to the admitted publication request.
            overwrite: Permit replacement of an existing publication.
            grant_token: One-use grant consumed during admission, if not already admitted.
            deadline: Compatibility input; the local commit does not transfer remote objects.
            admin: Apply the administrative overwrite authorization checks.
            expected_publication_sha256: Expected active archive hash for administrative overwrite.

        Returns:
            The durable receipt, including the original receipt on an exact replay.

        Raises:
            ViewerError: Admission, ownership, identity, validation or local commit fails.

        """
        rid = imported.report.run.id
        operation = self._publication_operation(
            imported,
            login,
            key,
            overwrite=overwrite,
            admin=admin,
            expected_publication_sha256=expected_publication_sha256,
            grant_token=grant_token,
        )
        if operation.get('phase') == 'available':
            return operation['receipt']
        path = None
        completed = False
        try:
            with self._run_lock(rid):
                current = self.repository.get(rid, include_deleted=True)
                owner = self._publication_owner(imported, login, operation, current, admin, expected_publication_sha256)
                generation = self.repository.next_generation(rid)
                identity = BundleIdentity(owner, rid, generation, imported.archive_sha256)
                marker = self._marker_for(imported, owner, operation['operation_id'])
                metadata = self._publication_metadata(imported, current)
                self.publication_phase(login, key, 'committing')
                size = imported.archive.stat().st_size + sum(
                    item.stat().st_size for item in imported.directory.rglob('*') if item.is_file()
                )
                pending = sum(
                    row.cache_bytes
                    for row in self.repository.versions()
                    if row.backup_state != 'backed_up' or not row.backup_valid
                )
                with self.files.reserve(
                    size, pending_bytes=pending, account_disk=(login, key) not in self._upload_reservations
                ):
                    target = self.files.version_path(identity)
                    if target.exists() and not self.files.evict(target):
                        raise ViewerError('run_updating', 'Publication files are still in use.', 503)
                    operation = self.repository.operation(login, key)
                    if operation is None:
                        raise ViewerError('invalid_operation', 'Publication operation is missing.', 409)
                    operation['installed_path'] = str(target)
                    self.repository.set_operation(login, key, operation['fingerprint'], operation)
                    path = self.files.install(imported, identity, marker)
                    receipt = self._commit_installed_publication(
                        imported,
                        login,
                        key,
                        operation,
                        identity,
                        metadata,
                        path,
                        current.generation if current else None,
                    )
                self._cleanup_upload(operation)
                completed = True
                return receipt
        finally:
            try:
                if not completed:
                    error = sys.exception()
                    if error is not None:
                        self._failed_publication_install(login, key, rid, path, error)
            finally:
                with self.lock:
                    if self._active.get(rid) == (login, key):
                        self._active.pop(rid, None)

    def activate_backup(self, rid: str) -> None:
        """Wake backup workers only after the successful publication response is sent."""
        self.backups.activate(rid)

    def _backup(self, job: BackupJob) -> str:
        """Publish the leased version's archive or metadata without bypassing restore reconciliation."""
        # Losing a previously initialized registry must not turn this backup into a reset.
        ensure_registry(self.repository, self.bundles, self._registry_lock)
        record = job.record
        identity = self._identity(record)
        if record.deleted:
            return self.bundles.publish_tombstone(identity, record.metadata, record.entry['marker']).manifest_key
        path = Path(record.local_path) if record.local_path else None
        try:
            marker = self.files.marker(identity)
        except FileNotFoundError:
            if not record.backup_metadata_revision:
                raise ViewerError('backup_source_missing', 'The local backup source is missing.', 503) from None
            marker = self.bundles.read_bundle(self._identity(record, confirmed=True)).marker
        if path is not None:
            try:
                self.files.pin(path)
            except ViewerError as error:
                if error.code != 'run_cache_miss':
                    raise
                path = None
        if path is not None:
            try:
                return self.bundles.publish_bundle(
                    identity, path / 'archive.tar.gz', marker, record.metadata
                ).manifest_key
            finally:
                self.files.unpin(path)
        return self.bundles.publish_bundle(identity, None, marker, record.metadata).manifest_key

    def cleanup_cache(self) -> int:
        """Evict only confirmed, inactive cache versions; user access owns TTL."""
        removed = 0
        rows = self.repository.versions()
        paths = {row.local_path: row.cache_bytes for row in rows if row.local_path and Path(row.local_path).is_dir()}
        total = sum(paths.values())
        now = self.db.clock()
        for record in sorted(rows, key=lambda row: row.last_access):
            if not record.local_path or record.deleted or record.run_id in self.busy_runs():
                continue
            if record.backup_state != 'backed_up' or not record.backup_valid:
                continue
            expired = now - record.last_access >= self.config.cache_idle_ttl_seconds
            if not expired and total <= self.config.cache_max_bytes:
                continue
            with self._run_lock(record.run_id):
                current = next(
                    (
                        row
                        for row in self.repository.versions()
                        if row.run_id == record.run_id and row.generation == record.generation
                    ),
                    None,
                )
                if not current or current.local_path != record.local_path or current.last_access != record.last_access:
                    continue
                if not current.local_path or not current.backup_valid or current.backup_state != 'backed_up':
                    continue
                if self.files.evict(current.local_path):
                    self.repository.update_local_state(
                        current.run_id,
                        generation=current.generation,
                        local_path=None,
                        cache_state='absent',
                        cache_bytes=0,
                    )
                    total -= current.cache_bytes
                    removed += 1
        for path in tuple(getattr(self, '_retired', set())):
            if self.files.evict(path):
                self._retired.discard(path)
        return removed

    def _maintain_local(self) -> None:
        """Recover accepted work and periodically clean local storage and request checkpoints."""
        while not self._stop.is_set():
            try:
                self._recover_uploads()
                self.cleanup_cache()
                self._cleanup_terminal_uploads()
                self._cleanup_orphan_paths()
                LocalRetention(self.config, self.repository, self.files, busy=lambda rid: rid in self.busy_runs()).run()
                interval = (
                    self.config.checkpoint_retry_seconds
                    if self.checkpoint_job.get('status') == 'failed'
                    else self.config.checkpoint_interval_seconds
                )
                if self.db.clock() - self._last_checkpoint >= interval:
                    self.request_checkpoint()
            except Exception as error:
                # Pending cleanup remains discoverable for the next maintenance pass.
                logging.getLogger('viewer.local').warning('cleanup_failed exception_type=%s', type(error).__name__)
            if self._stop.wait(min(self.config.cache_cleanup_interval_seconds, self.config.backup_retry_seconds)):
                break

    def _cleanup_orphan_paths(self) -> None:
        """Retry removal of uncommitted installed trees without deleting active publications."""
        for journal in (self.config.runtime / 'refresh-installs').glob('*.json'):
            if journal.is_symlink():
                continue
            intent = parse_json(journal.read_bytes())
            if not isinstance(intent, dict):
                raise ViewerError('invalid_refresh_install', 'Invalid refresh cleanup journal.')
            rid, raw_paths = intent.get('run_id'), intent.get('paths')
            if not isinstance(rid, str) or not isinstance(raw_paths, list):
                raise ViewerError('invalid_refresh_install', 'Invalid refresh cleanup journal.')
            paths: list[str] = []
            for path in raw_paths:
                if not isinstance(path, str):
                    raise ViewerError('invalid_refresh_install', 'Invalid refresh cleanup path.')
                paths.append(path)
            self._cleanup_refresh_install(journal, rid, paths)
        for rid, path in tuple(self._orphan_paths):
            with self._run_lock(rid):
                if any(record.local_path == str(path) for record in self.repository.versions()):
                    self._orphan_paths.discard((rid, path))
                elif self.files.evict(path):
                    self._orphan_paths.discard((rid, path))

    def _cleanup_terminal_uploads(self) -> None:
        """Find terminal operations and retry cleanup of their private upload directories."""
        with self.lock:
            operations = [json.loads(row[0]) for row in self.db.connection.execute('SELECT data FROM local_operations')]
        for operation in operations:
            with self._run_lock(operation['run_id']):
                if operation['run_id'] in self.busy_runs():
                    continue
                if operation.get('phase') in ('available', 'rejected'):
                    self._cleanup_upload(operation)
                installed = operation.get('installed_path')
                if operation.get('phase') == 'rejected' and installed:
                    path = Path(installed)
                    if path.is_relative_to(self.files.root) and not any(
                        record.local_path == installed for record in self.repository.versions()
                    ):
                        self.files.evict(path)

    def _recover_uploads(self) -> None:
        """Continue accepted exact archive bytes after a process interrupted local work."""
        if self.repository.restore_reconciliation()['status'] == 'required':
            return
        with self.lock:
            pending = list(self._recovery_operations)
        for row in pending:
            try:
                self._recover_upload(row)
            except Exception as error:
                # Keep durable work queued until its terminal transition succeeds.
                logging.getLogger('viewer.local').warning(
                    'upload_recovery_failed exception_type=%s', type(error).__name__
                )
            else:
                with self.lock:
                    self._recovery_operations.remove(row)

    def _recover_upload(self, row: dict) -> None:
        """Recover one startup operation, re-reading any previously committed transition."""
        operation = self.repository.operation(row['actor'], row['key'])
        if operation is None:
            return
        if operation.get('phase') in ('available', 'rejected'):
            self._cleanup_upload(operation)
            with self.lock:
                if self._active.get(operation['run_id']) == (row['actor'], row['key']):
                    self._active.pop(operation['run_id'], None)
            if operation['phase'] == 'available':
                self.activate_backup(operation['run_id'])
            return
        path = operation.get('archive_path')
        if not path or not Path(path).is_file():
            self.publication_failed(
                row['actor'],
                row['key'],
                ViewerError('upload_interrupted', 'Upload was interrupted; retry the archive.'),
            )
            return
        try:
            self.recovery = {'status': 'running', 'run_id': operation['run_id'], 'error_code': None}
            with tempfile.TemporaryDirectory(dir=self.files.temporary, prefix='recover-') as temporary:
                imported = validate_archive(Path(path), Path(temporary) / 'data', self.config.limits)
                with self.lock:
                    self._active[operation['run_id']] = (row['actor'], row['key'])
                self.publish(
                    imported,
                    row['actor'],
                    row['key'],
                    overwrite=operation['overwrite'],
                    admin=operation.get('admin', False),
                    expected_publication_sha256=operation.get('expected_publication_sha256'),
                )
                self.activate_backup(operation['run_id'])
            self.recovery = {'status': 'succeeded', 'error_code': None}
        except Exception as error:
            # Persist rejection for this accepted upload without abandoning other recovery work.
            self.publication_failed(row['actor'], row['key'], error)
            self.recovery = {'status': 'failed', 'error_code': getattr(error, 'code', 'local_recovery_failed')}

    def patch_metadata(self, rid: str, login: str, patch: dict, *, admin: bool = False) -> dict:
        """Persist owner or admitted administrator edits as a backed-up metadata revision."""
        with self._run_lock(rid), self.lock:
            self.check_write_admission()
            self._require_available(rid)
            current = self._read_record(rid)
            if admin:
                actor = self.db.actor(login)
                if actor is None or actor['role'] != 'admin':
                    raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
            elif current.owner != login:
                raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
            self.validate_patch(patch)
            metadata = {**current.metadata, **patch}
            self.repository.mutate_metadata(
                rid,
                metadata,
                expected_generation=current.generation,
                expected_metadata_revision=current.metadata_revision,
            )
            self._sync_snapshot()
            self.backups.wake.set()
            return {'run_id': rid, **metadata}

    def set_pinned(self, rid: str, pinned: bool) -> dict:
        """Persist the administrator's pin choice as a new metadata revision."""
        require(type(pinned) is bool, 'invalid_pinned')
        with self._run_lock(rid):
            self.check_write_admission()
            self._require_available(rid)
            current = self._read_record(rid)
            self.repository.mutate_metadata(
                rid,
                {**current.metadata, 'pinned': pinned},
                expected_generation=current.generation,
                expected_metadata_revision=current.metadata_revision,
            )
            self._sync_snapshot()
            self.backups.wake.set()
            return {'run_id': rid, 'pinned': pinned}

    def delete(self, rid: str, *, actor: Actor | None = None, publication: str | None = None) -> dict:
        """Check the expected head and actor before committing a new tombstone generation."""
        with self._run_lock(rid), self.lock:
            self.check_write_admission()
            self._require_available(rid)
            current = self._read_record(rid)
            if actor and actor['role'] != 'admin' and actor['login'] != current.owner:
                raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
            if publication is not None and publication != current.archive_sha256:
                raise ViewerError('run_changed', 'Run changed before deletion.', 409)
            tombstone = self.repository.tombstone(
                rid,
                expected_generation=current.generation,
                actor=(actor['login'] or current.owner) if actor else current.owner,
            )
            self.repository.update_local_state(
                rid, generation=tombstone.generation, local_path=None, cache_state='absent', cache_bytes=0
            )
            self._sync_snapshot()
            self.backups.wake.set()
            return {'run_id': rid, 'deleted': True}

    def set_settings(self, revision: str, order: list[str], display: dict) -> dict:
        """CAS basket presentation settings while preserving quarantined basket preferences."""
        with self.lock:
            self.check_write_admission()
            if revision != self.settings()['revision']:
                raise ViewerError('settings_changed', 'Settings changed; reload before saving.', 409)
            require(
                isinstance(order, list) and len(order) == len(set(order)) and set(order) == set(self.order),
                'invalid_basket_order',
            )
            self._validate_display(display)
            require(set(display) == set(order), 'invalid_basket_display')
            self.repository.save_settings(order=self._preserved_order(order), display=self._preserved_display(display))
            self._sync_snapshot()
            return self.settings()

    def request_refresh(self) -> dict:
        """Queue an explicit full audit and bind it to pending restore reconciliation."""
        self.check_write_admission(allow_restore_reconciliation=True)
        result = self.audit.request()
        self.repository.bind_restore_refresh(result['request_id'])
        self._start_audit()
        return result

    def resume_refresh(self, request_id: str) -> dict:
        """Resume only an eligible saved audit, preserving the restored-runtime boundary."""
        self.check_write_admission(allow_restore_reconciliation=True)
        recovery = self.repository.restore_reconciliation()
        if recovery['status'] == 'required' and recovery.get('request_id') != request_id:
            raise ViewerError('restore_reconciliation_required', 'Start a fresh refresh after restore.', 503)
        result = self.audit.resume(request_id)
        self._start_audit()
        return result

    def _start_audit(self) -> None:
        """Start at most one local audit coordinator thread."""
        with self.lock:
            if self._audit_thread and self._audit_thread.is_alive():
                return
            self._audit_thread = threading.Thread(target=self._run_audit, daemon=True, name='viewer-local-audit')
            self._audit_thread.start()

    def _run_audit(self) -> None:
        """Complete the audit and restore barrier checks before refreshing local catalogue views."""
        try:
            self._finish_restore_refresh(self.audit.run_once())
        finally:
            self._sync_snapshot()

    def refresh(self, *, persist: bool = True, _recover: bool = True) -> dict:
        """Run a full audit synchronously and retain the write barrier until reconciliation succeeds."""
        requested = self.audit.request()
        self.repository.bind_restore_refresh(requested['request_id'])
        result = self._finish_restore_refresh(self.audit.run_once())
        self._sync_snapshot()
        return result

    def _finish_restore_refresh(self, result: dict) -> dict:
        """Fence local writes until the audited remote registry is still current."""
        if self.repository.restore_reconciliation()['status'] != 'required' or result['status'] != 'succeeded':
            return result
        try:
            reservation_bytes, floors = self.bundles.reservation_floors()
            registry = self.bundles.registry_snapshot()
            if self.bundles.reservation_floors()[0] != reservation_bytes:
                raise ViewerError('restore_registry_changed', 'Backup reservations changed; run a fresh refresh.', 503)
            complete = self.repository.complete_restore_reconciliation(
                result['request_id'], digest(registry.head_bytes), len(registry.entries), floors
            )
            if not complete:
                raise ViewerError('restore_registry_changed', 'The remote registry changed; run a fresh refresh.', 503)
        except ViewerError as error:
            self.repository.restore_reconciliation_failed(error.code)
            return {**result, 'status': 'partial', 'error_code': error.code}
        self.backups.wake.set()
        return result

    def refresh_once(self) -> bool:
        """Report whether the synchronous full audit and any restore reconciliation succeeded."""
        return self.refresh()['status'] == 'succeeded'

    @staticmethod
    def _same_repair_identity(current: LocalRecord | None, expected: LocalRecord | None) -> bool:
        """Accept newer metadata only when the discovered publication bytes remain identical."""
        if current is None or expected is None:
            return current is None and expected is None
        return (current.run_id, current.owner, current.generation, current.archive_sha256, current.deleted) == (
            expected.run_id,
            expected.owner,
            expected.generation,
            expected.archive_sha256,
            expected.deleted,
        ) and current.metadata_revision >= expected.metadata_revision

    def _apply_bundle(self, downloaded: DownloadedBundle, expected: LocalRecord | None) -> str:
        """Install verified remote evidence only while its discovered local head still matches."""
        descriptor = downloaded.descriptor
        identity = descriptor.identity
        rid = identity.run_id
        with self._run_lock(rid):
            if rid in self.busy_runs():
                return 'deferred'
            current = self.repository.get(rid, include_deleted=True)
            if not self._same_repair_identity(current, expected):
                return 'deferred'
            if current and current.generation > identity.generation:
                return 'protected'
            owner_exists = self.db.connection.execute('SELECT 1 FROM users WHERE login=?', (identity.owner,)).fetchone()
            if not owner_exists:
                raise ViewerError('owner_missing', 'Create the original owner account before importing this run.', 409)
            if (
                current
                and current.generation == identity.generation
                and (
                    current.owner != identity.owner
                    or current.archive_sha256 != identity.archive_sha256
                    or current.deleted
                )
            ):
                raise ViewerError('generation_conflict', 'Remote publication conflicts with local identity.', 409)
            # Keep the staged-file stat checks; the audit owns the space reservation.
            _ = downloaded.imported.archive.stat().st_size + sum(
                path.stat().st_size for path in downloaded.imported.directory.rglob('*') if path.is_file()
            )
            with self._refresh_install(downloaded, current) as path:
                entry = PublicationReader(
                    self.config, VersionStore(self.files, path, f'runs/{identity.owner}/{rid}/')
                )._marker(f'runs/{identity.owner}/{rid}/publication.json')
                record = LocalRecord(
                    rid,
                    identity.owner,
                    identity.generation,
                    identity.metadata_revision,
                    entry,
                    downloaded.metadata,
                    str(path),
                    backup_state='backed_up',
                    last_access=current.last_access if current else 0,
                    cache_bytes=self.files.size(path),
                    backup_metadata_revision=identity.metadata_revision,
                    backup_manifest_key=descriptor.manifest_key,
                    backup_valid=True,
                )
                if rid in self.busy_runs():
                    self.files.evict(path)
                    return 'deferred'
                applied = self._merge_refreshed_files(record, expected)
                if not applied:
                    self.files.evict(path)
                    return 'deferred'
                if current and current.local_path and current.local_path != str(path):
                    if not self.files.evict(current.local_path):
                        if not hasattr(self, '_retired'):
                            self._retired = set()
                        self._retired.add(Path(current.local_path))
                self._sync_snapshot()
            return 'updated'

    @contextmanager
    def _refresh_install(self, downloaded: DownloadedBundle, current: LocalRecord | None) -> Iterator[Path]:
        """Journal installed copies until their actual database bindings can be checked."""
        identity = downloaded.descriptor.identity
        installation_id = uuid.uuid4()
        path = self.files.version_path(identity, installation_id)
        rid = identity.run_id
        journal = self.config.runtime / 'refresh-installs' / f'{installation_id}.json'
        paths = [str(path)]
        if current and current.local_path and current.local_path != str(path):
            paths.append(current.local_path)
        try:
            atomic_json(journal, {'run_id': rid, 'paths': paths})
            fsync_ancestors(journal.parent, self.config.runtime)
            self.files.install(downloaded.imported, identity, downloaded.marker, installation_id=installation_id)
            yield path
        finally:
            try:
                self._cleanup_refresh_install(journal, rid, paths)
            except Exception as error:
                logging.getLogger('viewer.local').warning(
                    'refresh_cleanup_deferred exception_type=%s', type(error).__name__
                )

    def _cleanup_refresh_install(self, journal: Path, rid: str, paths: list[str]) -> None:
        """Remove only proven unbound paths and retain the journal while cleanup is blocked."""
        with self._run_lock(rid):
            referenced = {record.local_path for record in self.repository.versions()}
            complete = True
            for path in paths:
                if path not in referenced and not self.files.evict(path):
                    complete = False
            if complete and journal.exists():
                journal.unlink()
                fsync_directory(journal.parent)

    def _merge_refreshed_files(self, verified: LocalRecord, expected: LocalRecord | None) -> bool:
        """CAS repaired files while retaining pending metadata and concurrent backup completion."""
        # File installation and validation happen before this short database critical section.
        # Backup workers use the database lock rather than the service's per-run lock.
        with self.db.lock:
            current = self.repository.get(verified.run_id, include_deleted=True)
            if not self._same_repair_identity(current, expected):
                return False
            if current and current.generation == verified.generation:
                if (
                    current.owner != verified.owner
                    or current.archive_sha256 != verified.archive_sha256
                    or current.deleted != verified.deleted
                ):
                    return False
                if current.metadata_revision > verified.metadata_revision:
                    verified = replace(
                        current,
                        entry=verified.entry,
                        local_path=verified.local_path,
                        cache_state=verified.cache_state,
                        cache_bytes=verified.cache_bytes,
                        backup_state=current.backup_state if current.backup_state != 'backed_up' else 'pending',
                        backup_metadata_revision=verified.backup_metadata_revision,
                        backup_manifest_key=verified.backup_manifest_key,
                        backup_valid=True,
                    )
                if current.backup_valid and current.backup_metadata_revision > verified.backup_metadata_revision:
                    verified = replace(
                        verified,
                        backup_state=current.backup_state,
                        backup_metadata_revision=current.backup_metadata_revision,
                        backup_manifest_key=current.backup_manifest_key,
                        backup_valid=True,
                    )
            return self.repository.merge_remote(
                verified,
                expected_generation=current.generation if current else None,
                expected_metadata_revision=current.metadata_revision if current else None,
            )

    def _apply_tombstone(self, descriptor: BundleDescriptor, expected: LocalRecord | None) -> str:
        """Apply a verified remote deletion without superseding a newer local generation."""
        rid = descriptor.identity.run_id
        with self._run_lock(rid):
            if rid in self.busy_runs():
                return 'deferred'
            current = self.repository.get(rid, include_deleted=True)
            if current is None:
                require(bool(descriptor.marker), 'tombstone_owner_binding_missing')
                record = LocalRecord(
                    rid,
                    descriptor.identity.owner,
                    descriptor.identity.generation,
                    descriptor.identity.metadata_revision,
                    cast(CatalogueEntry, {'marker': descriptor.marker}),
                    descriptor.metadata,
                    None,
                    deleted=True,
                    backup_state='backed_up',
                    cache_state='absent',
                    backup_metadata_revision=descriptor.identity.metadata_revision,
                    backup_manifest_key=descriptor.manifest_key,
                )
                if not self.repository.merge_remote(record, expected_generation=None, expected_metadata_revision=None):
                    return 'deferred'
                self._sync_snapshot()
                return 'updated'
            if (current.generation, current.metadata_revision) > (
                descriptor.identity.generation,
                descriptor.identity.metadata_revision,
            ):
                return 'protected'
            if expected is None:
                return 'deferred'
            record = replace(
                current,
                generation=descriptor.identity.generation,
                entry={'marker': descriptor.marker or current.entry['marker']},
                metadata=descriptor.metadata,
                metadata_revision=descriptor.identity.metadata_revision,
                deleted=True,
                backup_state='backed_up',
                local_path=None,
                cache_state='absent',
                cache_bytes=0,
                backup_metadata_revision=descriptor.identity.metadata_revision,
                backup_manifest_key=descriptor.manifest_key,
            )
            if not self.repository.merge_remote(
                record, expected_generation=expected.generation, expected_metadata_revision=expected.metadata_revision
            ):
                return 'deferred'
            self._sync_snapshot()
            return 'updated'

    def cache_stats(self) -> dict:
        """Snapshot catalogue, worker and recovery diagnostics under the shared state lock."""
        with self.lock:
            jobs = dict(self.db.connection.execute('SELECT state,count(*) FROM local_backup_jobs GROUP BY state'))
            result = {
                **self.snapshot.counts(),
                'users': len(self.db.list_users()),
                'last_refresh_at': self.audit.status().get('last_success_at'),
                'refresh': 'running' if self.audit.status()['status'] in ('pending', 'running') else 'idle',
                'refresh_job': self.audit.status(),
                'recovery': dict(self.recovery),
                'startup': dict(self.startup),
                'backup_jobs': jobs,
                'restore_jobs': self._restore_queue.snapshot(),
                'checkpoint_job': deepcopy(self.checkpoint_job),
                'publication_storage': 'local',
                'restore_reconciliation': self.repository.restore_reconciliation(),
            }
            return result

    def import_legacy(self, imported: ImportedRun, marker: dict, metadata: dict) -> LocalRecord:
        """Import exact legacy evidence locally without issuing a new user receipt."""
        require(self.allow_legacy_import, 'migration_not_enabled')
        rid, owner = imported.report.run.id, marker['owner']
        require(
            marker['run_id'] == rid
            and marker['archive_sha256'] == imported.archive_sha256
            and marker['archive_size'] == imported.archive.stat().st_size
            and marker['report_sha256'] == imported.manifest.report_sha256
            and marker['basket_sha256'] == imported.report.basket.sha256,
            'legacy_identity_mismatch',
        )
        with self._run_lock(rid):
            current = self.repository.get(rid, include_deleted=True)
            if current:
                if not current.deleted and current.owner == owner and current.archive_sha256 == imported.archive_sha256:
                    return current
                raise ViewerError(
                    'migration_conflict', 'Existing local publication differs from the legacy source.', 409
                )
            with self.db.lock:
                if self.db.connection.execute('SELECT 1 FROM users WHERE login=?', (owner,)).fetchone() is None:
                    raise ViewerError('owner_missing', 'Create the original owner account before importing.', 409)
            self.validate_patch({'display_name': metadata['display_name'], 'tags': metadata['tags']})
            require(
                set(metadata) == {'display_name', 'tags', 'pinned', 'created_at'} and type(metadata['pinned']) is bool,
                'invalid_metadata',
            )
            identity = BundleIdentity(owner, rid, 1, imported.archive_sha256)
            target = self.files.version_path(identity)
            if target.exists() and not self.files.evict(target):
                raise ViewerError('migration_busy', 'Import files are still in use.', 503)
            path = self.files.install(imported, identity, marker)
            entry = PublicationReader(self.config, VersionStore(self.files, path, f'runs/{owner}/{rid}/'))._marker(
                f'runs/{owner}/{rid}/publication.json'
            )
            record = LocalRecord(
                rid,
                owner,
                1,
                1,
                entry,
                deepcopy(metadata),
                str(path),
                last_access=0,
                cache_bytes=self.files.size(path),
            )
            self.repository.commit(record, expected_generation=None, actor=owner)
            self._sync_snapshot()
            return record

    def request_checkpoint(self) -> dict:
        """Queue a private service backup without waiting for remote storage."""
        with self.lock:
            self.check_write_admission()
            if self.checkpoint_job.get('status') not in ('pending', 'running'):
                self.checkpoint_job = dict(
                    status='pending',
                    request_id=str(uuid.uuid4()),
                    error_code=None,
                    manifest_key=None,
                    started_at=None,
                    finished_at=None,
                )
                self.repository.save_refresh('checkpoint-active', self.checkpoint_job)
            self._start_checkpoint()
            return deepcopy(self.checkpoint_job)

    def _start_checkpoint(self) -> None:
        """Start at most one checkpoint worker for the saved administrative request."""
        with self.lock:
            if self._checkpoint_thread and self._checkpoint_thread.is_alive():
                return
            self._checkpoint_thread = threading.Thread(
                target=self._create_checkpoint, daemon=True, name='viewer-checkpoint'
            )
            self._checkpoint_thread.start()

    def _create_checkpoint(self) -> None:
        """Persist checkpoint progress and retain a diagnosable outcome after remote failure."""
        with self.lock:
            self._last_checkpoint = self.db.clock()
            self.checkpoint_job.update(
                status='running', started_at=utc_now(self._last_checkpoint), schedule_origin=self._last_checkpoint
            )
            self.repository.save_refresh('checkpoint-active', self.checkpoint_job)
        try:
            key = CheckpointRepository(
                self.config,
                self.db,
                self.repository,
                self.bundles,
                self._registry_lock,
            ).create()
            result = dict(status='succeeded', manifest_key=key, error_code=None)
        except Exception as error:
            # Keep failure observable; previously closed checkpoints are never removed here.
            result = dict(status='failed', error_code=getattr(error, 'code', 'checkpoint_failed'))
        with self.lock:
            self.checkpoint_job.update(**result, finished_at=utc_now(self.db.clock()))
            self.repository.save_refresh('checkpoint-active', self.checkpoint_job)

    def retry_backup(self, rid: str) -> dict:
        """Explicitly requeue failed jobs without stealing an active worker's claim."""
        with self.lock:
            self.check_write_admission()
            record = self.repository.get(rid, include_deleted=True)
            if record is None:
                raise ViewerError('not_found', 'Run not found.', 404)
            with self.db.connection:
                cursor = self.db.connection.execute(
                    (
                        'UPDATE local_backup_jobs SET state=\'pending\',active=1,retry_at=0,worker=NULL,t'
                        'oken=NULL,lease_until=0 WHERE run_id=? AND (state IN (\'blocked\',\'retry_wait\') '
                        'OR (state=\'backed_up\' AND error IS NOT NULL))'
                    ),
                    (rid,),
                )
                scheduled = cursor.rowcount
            self.backups.wake.set()
            return {'run_id': rid, 'scheduled': scheduled}
