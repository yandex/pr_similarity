"""Durable full-backup audits with per-run merge and resumable verification."""

import copy
import logging
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from viewer.bundles import (
    BundleDescriptor,
    BundleIdentity,
    BundleRepository,
    DownloadedBundle,
)
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_repository import LocalRecord, LocalRepository
from viewer.util import digest, utc_now

ApplyBundle = Callable[[DownloadedBundle, LocalRecord | None], str]
ApplyTombstone = Callable[[BundleDescriptor, LocalRecord | None], str]


class RefreshAudit:
    """Discover first, verify exact closed identities, then ask the service to CAS."""

    def __init__(
        self,
        config: Config,
        repository: LocalRepository,
        bundles: BundleRepository,
        apply_bundle: ApplyBundle,
        *,
        busy: Callable[[str], bool],
        on_progress: Callable[[dict], None] | None = None,
        apply_tombstone: ApplyTombstone | None = None,
        start_worker: bool = True,
        run_lock: Callable[[str], AbstractContextManager] | None = None,
        reserve_download: Callable[[BundleDescriptor], AbstractContextManager[object]] | None = None,
    ) -> None:
        """Bind durable audit state, per-run merge callbacks and optional worker startup."""
        self.config, self.repository, self.bundles = config, repository, bundles
        self.apply_bundle, self.apply_tombstone = apply_bundle, apply_tombstone
        self.busy, self.on_progress = busy, on_progress
        self.run_lock = run_lock or (lambda rid: nullcontext())
        self.reserve_download = reserve_download or (lambda _: nullcontext())
        self._lock = repository.lock
        self._runner = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._state = repository.load_refresh('active') or self._empty()
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(target=self._worker, name='viewer-refresh', daemon=True)
            self._thread.start()
            if self._state['status'] in ('pending', 'running'):
                self._wake.set()

    def _empty(self) -> dict:
        """Build an idle progress envelope with no captured remote discovery boundary."""
        return dict(
            status='idle',
            started_at=None,
            request_id=None,
            completed_id=None,
            phase='idle',
            boundary=None,
            discovered=0,
            total=0,
            checked=0,
            updated=0,
            protected=0,
            deferred=0,
            errors=0,
            failures=[],
            bytes=0,
            active=[],
            last_success_at=None,
            plan=[],
            results={},
            discovery_complete=False,
            discovery_failed=False,
        )

    def status(self) -> dict:
        """Return progress without exposing serialized internal projections."""
        with self._lock:
            return copy.deepcopy(
                {
                    key: value
                    for key, value in self._state.items()
                    if key not in ('plan', 'results', 'discovery_complete', 'discovery_failed')
                }
            )

    def _save(self) -> None:
        """Persist the active audit and notify observers without letting them interrupt verification."""
        with self._lock:
            self.repository.save_refresh('active', self._state)
            if self._state['request_id']:
                self.repository.save_refresh(self._state['request_id'], self._state)
        if self.on_progress is not None:
            try:
                self.on_progress(self.status())
            except Exception as error:
                # Optional observers cannot change durable verification outcomes.
                logging.getLogger('viewer.refresh').warning(
                    'refresh_progress_callback_failed exception_type=%s', type(error).__name__
                )

    def request(self) -> dict:
        """Persist a fresh audit, or return the already queued/running request."""
        with self._lock:
            if self._stop.is_set():
                raise ViewerError('refresh_closed', 'Refresh worker is closed.', 503)
            if self._state['status'] not in ('pending', 'running'):
                previous = self._state
                self._state = self._empty()
                self._state.update(
                    status='pending',
                    request_id=str(uuid.uuid4()),
                    phase='discovery',
                    completed_id=previous['completed_id'],
                    last_success_at=previous['last_success_at'],
                )
                self._save()
            self._wake.set()
            return self.status()

    def resume(self, refresh_id: str) -> dict:
        """Retry a saved partial audit against its original registry boundary."""
        with self._lock:
            if self._stop.is_set():
                raise ViewerError('refresh_closed', 'Refresh worker is closed.', 503)
            if self._state['status'] in ('pending', 'running'):
                if self._state['request_id'] != refresh_id:
                    raise ViewerError('refresh_busy', 'Another refresh is already running.', 409)
                return self.status()
            saved = self.repository.load_refresh(refresh_id)
            if saved is None:
                raise ViewerError('not_found', 'Refresh request was not found.', 404)
            if saved['status'] not in ('partial', 'failed', 'cancelled'):
                raise ViewerError('refresh_complete', 'Only an incomplete refresh can be resumed.', 409)
            saved['last_success_at'] = self._state['last_success_at'] or saved['last_success_at']
            self._state = saved
            self._state['status'] = 'pending'
            if not self._state['discovery_complete']:
                failures = self._state['failures']
                retained = [failure for failure in failures if failure['key'] != 'discovery']
                self._state['errors'] -= len(failures) - len(retained)
                self._state['failures'] = retained
            self._save()
            self._wake.set()
            return self.status()

    def _failure(self, key: str, code: str, run_id: str | None = None) -> None:
        """Record a distinct per-object failure and persist the resulting progress counters."""
        with self._lock:
            failure = dict(key=key, code=code, run_id=run_id)
            if failure not in self._state['failures']:
                self._state['failures'].append(failure)
                self._state['errors'] += 1
        self._save()

    def _discover(self) -> None:
        """Capture one authoritative immutable registry before reading its bundles."""
        with self._lock:
            self._state.update(
                phase='discovery', plan=[], discovered=0, discovery_complete=False, discovery_failed=False
            )
        try:
            snapshot = self.bundles.registry_snapshot()
            plan = []
            for reference in snapshot.entries:
                identity = BundleIdentity.from_wire(reference['identity'])
                current = self.repository.get(identity.run_id, include_deleted=True)
                expected = (
                    None
                    if current is None
                    else dict(
                        generation=current.generation,
                        metadata_revision=current.metadata_revision,
                        archive_sha256=current.archive_sha256,
                        owner=current.owner,
                        deleted=current.deleted,
                    )
                )
                plan.append(dict(reference, expected=expected))
            with self._lock:
                self._state.update(
                    boundary=dict(
                        at=utc_now(),
                        registry_revision=snapshot.revision,
                        snapshot_sha256=snapshot.snapshot_sha256,
                        head_sha256=digest(snapshot.head_bytes),
                    ),
                    plan=plan,
                    discovered=len(plan),
                    total=len(plan),
                    discovery_complete=True,
                )
        except Exception as error:
            # An incomplete boundary must remain a recorded failure, never an empty catalogue.
            with self._lock:
                self._state['discovery_failed'] = True
            self._failure('discovery', self._code(error))
        self._save()

    @staticmethod
    def _code(error: Exception) -> str:
        """Expose a known viewer error code or a stable generic audit failure code."""
        return error.code if isinstance(error, ViewerError) else 'refresh_failed'

    @staticmethod
    def _matches(current: LocalRecord | None, expected: dict | None, *, allow_metadata_advance: bool = False) -> bool:
        """Fence content changes while optionally accepting newer labels on identical bytes."""
        if current is None or expected is None:
            return current is None and expected is None
        return (
            current.generation,
            current.archive_sha256,
            current.owner,
            current.deleted,
        ) == (
            expected['generation'],
            expected['archive_sha256'],
            expected['owner'],
            expected['deleted'],
        ) and (
            current.metadata_revision == expected['metadata_revision']
            or (allow_metadata_advance and current.metadata_revision > expected['metadata_revision'])
        )

    def _protect_corruption(self, descriptor: BundleDescriptor, error: Exception) -> None:
        """Protect a matching local copy when its referenced remote evidence is corrupt."""
        if not isinstance(error, ViewerError) or error.code not in {
            'not_found',
            'stored_digest_mismatch',
            'invalid_bundle',
            'bundle_conflict',
            'invalid_json',
            'invalid_contract',
            'invalid_publication',
            'unsafe_archive',
            'invalid_archive',
        }:
            return
        identity = descriptor.identity
        with self.run_lock(identity.run_id):
            current = self.repository.get(identity.run_id, include_deleted=True)
            if (
                current is not None
                and current.generation == identity.generation
                and current.owner == identity.owner
                and current.archive_sha256 == identity.archive_sha256
                and (
                    getattr(error, 'component', None) == 'archive'
                    or current.backup_manifest_key == descriptor.manifest_key
                )
            ):
                self.repository.invalidate_backup(identity.run_id, identity.generation, error.code)

    def _apply(self, descriptor: BundleDescriptor, expected: dict | None, downloaded: DownloadedBundle | None) -> str:
        """Merge verified remote evidence only if the discovered local head is still eligible."""
        identity = descriptor.identity
        current = self.repository.get(identity.run_id, include_deleted=True)
        if not self._matches(current, expected, allow_metadata_advance=not descriptor.tombstone):
            return 'protected'
        if current is not None:
            if current.owner != identity.owner:
                raise ViewerError('owner_conflict', 'Remote publication owner conflicts.', 409)
            if current.generation > identity.generation or (
                descriptor.tombstone
                and current.generation == identity.generation
                and current.metadata_revision > identity.metadata_revision
            ):
                return 'protected'
            if current.generation == identity.generation and (
                current.archive_sha256 != identity.archive_sha256 or current.deleted != descriptor.tombstone
            ):
                raise ViewerError('bundle_conflict', 'Remote publication generation conflicts.', 409)
        if self.busy(identity.run_id):
            return 'deferred'
        if descriptor.tombstone:
            if self.apply_tombstone is None:
                return 'deferred'
            result = self.apply_tombstone(descriptor, current)
        else:
            assert downloaded is not None
            result = self.apply_bundle(downloaded, current)
        if result not in ('updated', 'protected', 'deferred'):
            raise ViewerError('refresh_result', 'Invalid refresh merge result.', 500)
        return result

    def _verify_item(self, item: dict) -> None:
        """Verify one frozen bundle identity and persist its merge result or failure."""
        identity = BundleIdentity.from_wire(item['identity'])
        key = item['manifest_key']
        descriptor = BundleDescriptor(identity, {}, {}, item['archive_size'], item['tombstone'])
        previous = self._state['results'].get(key)
        if previous is not None:
            if previous['result'] in ('updated', 'protected'):
                return
            with self._lock:
                if previous['result'] == 'deferred':
                    self._state['deferred'] -= 1
                    self._state['checked'] -= 1
                    self._state['bytes'] -= item['archive_size']
                else:
                    failures = self._state['failures']
                    retained = [failure for failure in failures if failure['key'] != key]
                    self._state['errors'] -= len(failures) - len(retained)
                    self._state['failures'] = retained
                del self._state['results'][key]
        with self._lock:
            self._state['active'] = [dict(run_id=identity.run_id, object=key, phase='verification', bytes=0)]
        self._save()
        remote_verified = False
        try:
            descriptor = self.bundles.read_bundle(identity, expected_sha256=item['closure_sha256'])
            if (
                descriptor.manifest_key != key
                or descriptor.archive_size != item['archive_size']
                or descriptor.tombstone != item['tombstone']
            ):
                raise ViewerError('bundle_conflict', 'Remote bundle changed after discovery.', 409)
            if descriptor.tombstone:
                self.bundles.verify_bundle(descriptor)
                remote_verified = True
                result = self._apply(descriptor, item['expected'], None)
            else:
                # The callback installs/moves its files before this staging owner exits.
                with self.reserve_download(descriptor):
                    staging = self.config.runtime / 'refresh-staging'
                    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix='audit-', dir=staging) as temporary:
                        downloaded = self.bundles.download_bundle(descriptor, Path(temporary) / 'bundle')
                        remote_verified = True
                        result = self._apply(descriptor, item['expected'], downloaded)
            with self._lock:
                self._state['checked'] += 1
                self._state[result] += 1
                self._state['bytes'] += descriptor.archive_size
                self._state['results'][key] = dict(identity=identity.to_wire(), checked_at=utc_now(), result=result)
        except Exception as error:
            # Preserve the per-item outcome and protect local evidence before continuing the audit.
            if not remote_verified:
                self._protect_corruption(descriptor, error)
            self._failure(key, self._code(error), identity.run_id)
            with self._lock:
                self._state['results'][key] = dict(identity=identity.to_wire(), checked_at=utc_now(), result='error')
        finally:
            with self._lock:
                self._state['active'] = []
            self._save()

    def run_once(self) -> dict:
        """Execute the pending durable request synchronously; concurrent calls coalesce."""
        if not self._runner.acquire(blocking=False):
            return self.status()
        try:
            with self._lock:
                if self._state['status'] not in ('pending', 'running'):
                    return self.status()
                self._state['status'] = 'running'
                self._state['started_at'] = utc_now()
            self._save()
            if not self._state['discovery_complete']:
                self._discover()
            with self._lock:
                self._state['phase'] = 'verification'
            self._save()
            for item in self._state['plan']:
                if self._stop.is_set():
                    break
                self._verify_item(item)
            with self._lock:
                if self._stop.is_set():
                    self._state['status'] = 'cancelled'
                elif self._state['discovery_failed']:
                    self._state['status'] = 'failed'
                elif self._state['errors'] or self._state['deferred']:
                    self._state['status'] = 'partial'
                else:
                    self._state['status'] = 'succeeded'
                    self._state['last_success_at'] = utc_now()
                self._state.update(phase='completed', completed_id=self._state['request_id'], active=[])
            self._save()
            return self.status()
        finally:
            self._runner.release()

    def _worker(self) -> None:
        """Resume queued audits between stop checks and record worker-level failures."""
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if not self._stop.is_set():
                try:
                    self.run_once()
                except Exception as error:
                    # Persist a worker failure without discarding the resumable discovery plan.
                    self._failure('worker', self._code(error))
                    with self._lock:
                        self._state['status'] = 'failed'
                    self._save()

    def close(self, timeout: float | None = None) -> None:
        """Stop between items and preserve the remaining plan for explicit resume."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=self.config.shutdown_seconds if timeout is None else max(0, timeout))
            if self._thread.is_alive():
                raise ViewerError('refresh_busy', 'Refresh is still finishing an active transfer.', 503)
