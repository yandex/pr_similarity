"""Catalogue, UI metadata and durable single-process publication lifecycle."""

import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, Unpack

from viewer.catalogue import CatalogueBuilder, CatalogueSnapshot, ScanStore
from viewer.config import Config
from viewer.errors import StorageError, ViewerError
from viewer.maintenance import Maintenance
from viewer.models import Basket, Report
from viewer.persistence.database import Actor, Database
from viewer.persistence.storage import Store, StoredBody
from viewer.presentation.inference import public_inference
from viewer.presentation.metrics import (
    STATUSES,
    aggregate,
    tag_groups,
)
from viewer.publication import CatalogueEntry, PublicationReader, StagedPublication
from viewer.util import (
    JsonValue,
    digest,
    json_bytes,
    parse_json,
    utc_now,
)
from viewer.validation.archive import ImportedRun
from viewer.validation.contracts import (
    require,
    validate_report,
)


class MetricSelection(TypedDict, total=False):
    """Describe optional public aggregation controls for a run projection."""

    statuses: list[str]
    pr_aggregation: str
    file_aggregation: str


class ViewerService:
    """Own catalogue snapshots and durable publication operations."""

    def __init__(self, config: Config, store: Store, db: Database, *, initialize: bool = True) -> None:
        """Prepare catalogue state, publication locks and maintenance recovery."""
        self.config, self.store, self.db = config, store, db
        self.lock = threading.RLock()
        self.db.lock = self.lock
        self.write_lock = threading.RLock()
        self._write_depth = 0
        self._write_epoch = 0
        self.maintenance = Maintenance(
            self._recover_or_initialize,
            config.recovery_retry_seconds,
            config.recovery_attempts,
            self.refresh_once,
        )
        self._background_started = False
        self.snapshot = CatalogueSnapshot()
        self.staging = config.runtime / 'staging'
        self.staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.startup = {'status': 'starting', 'phase': 'recovery', 'error_code': None}
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        """Recover and validate before exposing a ready catalogue."""
        try:
            with self.lock:
                self.startup = {'status': 'starting', 'phase': 'recovery', 'error_code': None}
                self.recover()
                self.cleanup_staging()
                self.startup = {'status': 'starting', 'phase': 'catalogue', 'error_code': None}
                self.refresh()
                self.startup = {'status': 'ready', 'phase': 'ready', 'error_code': None}
        except Exception as error:
            self.startup = {
                **self.startup,
                'status': 'failed',
                'error_code': getattr(error, 'code', 'startup_failed'),
            }
            raise

    def _recover_or_initialize(self) -> None:
        """Retry startup validation or recover an already initialized service."""
        if self.startup['status'] != 'ready':
            self.initialize()
        else:
            self.recover()

    @property
    def runs(self) -> dict[str, CatalogueEntry]:
        """Return run entries from the current coherent snapshot."""
        return self.snapshot.runs

    @property
    def metadata(self) -> dict[str, dict]:
        """Return display metadata from the current snapshot."""
        return self.snapshot.metadata

    @property
    def order(self) -> list[str]:
        """Return active basket order from the current snapshot."""
        return self.snapshot.order

    @property
    def display(self) -> dict[str, dict[str, bool]]:
        """Return active basket visibility flags from the current snapshot."""
        return self.snapshot.display

    @property
    def quarantined(self) -> dict[str, dict]:
        """Return publications excluded during catalogue validation."""
        return self.snapshot.quarantined

    @property
    def last_refresh(self) -> str | None:
        """Return the timestamp of the last installed full scan."""
        return self.snapshot.last_refresh

    @property
    def _persisted_order(self) -> list[str]:
        """Return stored ordering including quarantined baskets."""
        return self.snapshot.persisted_order

    @property
    def _persisted_display(self) -> dict[str, dict[str, bool]]:
        """Return stored flags including quarantined baskets."""
        return self.snapshot.persisted_display

    def start_background(self) -> None:
        """Start one worker, including deferred startup when used by the CLI."""
        with self.lock:
            self._background_started = True
            if self.startup['status'] != 'ready' or self.db.operations():
                self.maintenance.request_recovery()
            self.maintenance.start()

    def close(self, timeout: float | None = None) -> bool:
        """Report whether workers stopped; callers must retain SQLite if not."""
        budget = self.config.shutdown_seconds if timeout is None else timeout
        deadline = time.monotonic() + budget
        self.maintenance.close(timeout=budget)
        acquired = self.write_lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if acquired:
            self.write_lock.release()
        readers_stopped = self.lock.acquire(timeout=max(0, deadline - time.monotonic()))
        if readers_stopped:
            self.lock.release()
        workers_stopped = self.maintenance.close(timeout=max(0, deadline - time.monotonic()))
        return acquired and readers_stopped and workers_stopped

    @contextmanager
    def _writer(self) -> Iterator[None]:
        """Serialize writers while letting unrelated readers use the last snapshot."""
        with self.write_lock:
            with self.lock:
                self._write_depth += 1
                self._write_epoch += 1
            try:
                yield
            finally:
                with self.lock:
                    self._write_depth -= 1

    def check_write_admission(self) -> None:
        """Reject writes during shutdown or pending journal recovery."""
        with self.lock:
            if self.maintenance.stopping:
                raise ViewerError('service_stopping', 'Service is stopping.', 503)
            if self.db.operations():
                raise ViewerError('recovery_pending', 'Pending recovery; retry after it completes.', 503)

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Admit one storage writer without retaining the shared catalogue lock."""
        with self._writer():
            self.check_write_admission()
            try:
                yield
            finally:
                with self.lock:
                    if self._background_started and self.db.operations():
                        self.maintenance.request_recovery()

    def busy_runs(self) -> set[str]:
        """Return only publication IDs whose files may currently be changing."""
        return {
            op['prefix'].rstrip('/').rsplit('/', 1)[-1]
            for op in self.db.operations().values()
            if op.get('prefix', '').startswith('runs/')
        }

    def _require_available(self, rid: str) -> None:
        """Never mix a cached row with files from an unfinished publication."""
        if rid in self.busy_runs():
            raise ViewerError('run_updating', 'Run is being updated; retry after recovery.', 503)

    def _json(self, key: str, default: JsonValue = None) -> JsonValue:
        """Read a stored JSON document or copy the caller default when absent."""
        try:
            return parse_json(self.store.get(key, self.config.limits.json_bytes))
        except ViewerError as error:
            if error.status == 404:
                return deepcopy(default)
            raise

    def _object(self, key: str, default: JsonValue = None, *, code: str = 'invalid_contract') -> dict:
        """Check a stored object boundary before validating its dynamic metadata."""
        value = self._json(key, default)
        require(isinstance(value, dict), code)
        assert isinstance(value, dict)
        return value

    def _builder(self) -> CatalogueBuilder:
        """Copy SQLite ownership under the service lock; builders never retain it."""
        with self.lock:
            owners = {
                row['run_id']: {'login': row['login'], 'basket_hash': row['basket_hash']}
                for row in self.db.connection.execute('SELECT run_id,login,basket_hash FROM owners')
            }
            return CatalogueBuilder(self.config, ScanStore(self.store, self.maintenance.stop), owners)

    def _marker(self, key: str) -> CatalogueEntry:
        """Validate one stored publication and its bound evidence."""
        return PublicationReader(self.config, self.store)._marker(key)

    def _scan_publications(self) -> tuple[dict[str, CatalogueEntry], dict[str, dict], set[str]]:
        """Scan stored publications using copied ownership bindings."""
        return self._builder()._scan_publications()

    def refresh(self, *, persist: bool = True, _recover: bool = True) -> dict[str, int]:
        """Rebuild a complete catalogue synchronously for startup and offline callers."""
        with self._writer():
            if _recover and self.db.operations():
                self.recover()
            candidate = self._builder().build()
            if persist:
                self.store.put('settings/viewer-metadata.json', json_bytes(candidate.metadata))
                self.store.put('settings/basket-order.json', json_bytes(candidate.persisted_order))
                self.store.put(
                    'settings/basket-display.json',
                    json_bytes({'version': 1, 'items': candidate.persisted_display}),
                )
            self._install_snapshot(candidate)
            return candidate.counts()

    def _install_snapshot(self, candidate: CatalogueSnapshot) -> None:
        """Reserve owners before replacing the only visible catalogue reference."""
        with self.lock:
            for rid, row in candidate.runs.items():
                self.db.reserve_owner(rid, row['marker']['owner'], row['marker']['basket_sha256'])
            self.snapshot = replace(candidate, revision=self.snapshot.revision + 1)

    def request_refresh(self) -> dict:
        """Accept a refresh request, including an explicit retry of failed recovery."""
        with self.lock:
            if self.startup['status'] != 'ready' or self.db.operations():
                self.maintenance.request_recovery()
            return self.maintenance.request_refresh()

    def refresh_once(self) -> bool:
        """Build outside the service lock and reject snapshots invalidated by writes."""
        with self.lock:
            if self._write_depth or self.db.operations() or self.maintenance.stop.is_set():
                return False
            revision = (self.snapshot.revision, self._write_epoch)
            builder = self._builder()
            builder.store = ScanStore(self.store, self.maintenance.stop)
        try:
            candidate = builder.build()
        except (ViewerError, OSError):
            with self.lock:
                if (
                    self.maintenance.stop.is_set()
                    or self._write_depth
                    or self.db.operations()
                    or revision != (self.snapshot.revision, self._write_epoch)
                ):
                    return False
            raise
        with self.lock:
            if (
                self.maintenance.stop.is_set()
                or self._write_depth
                or self.db.operations()
                or revision != (self.snapshot.revision, self._write_epoch)
            ):
                return False
            self._install_snapshot(candidate)
            return True

    def _prepare_operation(self, op: dict, row: CatalogueEntry | None = None) -> dict:
        """Build a local catalogue change without exposing uncommitted state."""
        runs = {rid: entry for rid, entry in self.runs.items() if entry['prefix'] != op['prefix']}
        quarantined = {rid: entry for rid, entry in self.quarantined.items() if entry['prefix'] != op['prefix']}
        if row is not None:
            rid = row['marker']['run_id']
            self.db.reserve_owner(rid, row['marker']['owner'], row['marker']['basket_sha256'])
            runs[rid] = row
        stored = self._object(
            'settings/basket-display.json', {'version': 1, 'items': {}}, code='invalid_basket_display'
        )
        require(
            set(stored) == {'version', 'items'} and type(stored['version']) is int and stored['version'] == 1,
            'invalid_basket_display',
        )
        self._validate_display(stored['items'])
        hashes = {entry['marker']['basket_sha256'] for entry in runs.values()}
        order = [h for h in op['order'] if h in hashes]
        persisted = {h: deepcopy(stored['items'].get(h, {'hidden': False, 'collapsed': False})) for h in op['order']}
        return dict(
            runs=runs,
            metadata=deepcopy(op['metadata']),
            order=order,
            display={h: persisted[h] for h in order},
            quarantined=quarantined,
            _persisted_order=list(op['order']),
            _persisted_display=persisted,
        )

    def _install_operation(self, state: dict) -> None:
        """Expose one coherent prepared change after receipt persistence."""
        with self.lock:
            fields = {
                name.removeprefix('_') if name.startswith('_persisted') else name: value
                for name, value in state.items()
            }
            self.snapshot = replace(self.snapshot, **fields, revision=self.snapshot.revision + 1)

    def _complete_operation(self, op_id: str, state: dict, publication: dict | None = None) -> None:
        """Commit the durable result and visible version within one reader exclusion."""
        op = publication or {}
        with self.lock:
            self.db.finish_operation(op_id, op.get('owner'), op.get('key'), op.get('fingerprint'), op.get('receipt'))
            self._install_operation(state)

    def _preserved_order(self, order: list[str]) -> list[str]:
        """Retain quarantined baskets in durable order during valid owner operations."""
        protected = {row['basket_hash'] for row in self.quarantined.values()}
        result = list(order)
        for index, basket_hash in enumerate(self._persisted_order):
            if basket_hash in protected and basket_hash not in result:
                result.insert(min(index, len(result)), basket_hash)
        return result

    def _preserved_display(self, display: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
        """Keep quarantined basket flags without exposing them as active UI settings."""
        protected = {row['basket_hash'] for row in self.quarantined.values()}
        result = {key: deepcopy(flags) for key, flags in self._persisted_display.items() if key in protected}
        result.update(deepcopy(display))
        return result

    def cleanup_staging(self) -> None:
        # Only our reserved upload/op directory names; never follow symlinks.
        """Remove unreferenced owned staging directories without following symlinks."""
        referenced = {op.get('staging') for op in self.db.operations().values()}
        for child in self.staging.iterdir():
            owned = bool(re.fullmatch(r'upload-[a-z0-9_]{8}', child.name))
            try:
                owned = owned or str(uuid.UUID(child.name)) == child.name
            except ValueError:
                pass
            if owned and child.is_dir() and not child.is_symlink() and str(child) not in referenced:
                shutil.rmtree(child)

    def recover(self) -> None:
        """Replay pending durable operations before exposing the refreshed catalogue."""
        with self._writer():
            deadline = time.monotonic() + self.config.limits.transfer_seconds
            for op_id, value in self.db.operations().items():
                self._execute(op_id, value, deadline=deadline)

    def _execute_settings(self, op_id: str, op: dict, budget: Callable[[], None]) -> None:
        """Persist layout settings before completing the journal and snapshot."""
        budget()
        self.store.put('settings/basket-order.json', json_bytes(op['order']))
        budget()
        self.store.put('settings/basket-display.json', json_bytes({'version': 1, 'items': op['display']}))
        hashes = {entry['marker']['basket_sha256'] for entry in self.runs.values()}
        order = [h for h in op['order'] if h in hashes]
        state = dict(
            order=order,
            display={h: deepcopy(op['display'][h]) for h in order},
            _persisted_order=list(op['order']),
            _persisted_display=deepcopy(op['display']),
        )
        self._complete_operation(op_id, state)

    def _rollback_publication(self, op_id: str, op: dict, budget: Callable[[], None]) -> None:
        """Restore the backed-up publication when an incomplete upload loses staging."""
        prefix = op['prefix']
        for key in list(self.store.list(prefix)):
            budget()
            self.store.delete(key)
        for old in op['old_keys']:
            budget()
            self.store.copy(op['trash'] + old[len(prefix) :], old)
        self.store.put('settings/viewer-metadata.json', json_bytes(op['previous_metadata']))
        self.store.put('settings/basket-order.json', json_bytes(op['previous_order']))
        row = self._marker(prefix + 'publication.json') if self._json(prefix + 'publication.json') else None
        state = self._prepare_operation({**op, 'metadata': op['previous_metadata'], 'order': op['previous_order']}, row)
        self._complete_operation(op_id, state)

    def _install_publication(
        self,
        op_id: str,
        op: dict,
        budget: Callable[[], None],
        deadline: float | None,
    ) -> None:
        """Resume file installation and write the marker after all evidence is durable."""
        prefix = op['prefix']
        root = Path(op['staging'])
        if not self.db.progress(op_id, 'archive'):
            budget()
            self.store.put_file(prefix + 'archive.tar.gz', root / 'archive.tar.gz', deadline=deadline)
            self.db.set_progress(op_id, 'archive', 1)
        start = self.db.progress(op_id, 'files')
        for index, entry in enumerate(op['marker']['inventory'][start:], start + 1):
            budget()
            self.store.put_file(prefix + 'data/' + entry['path'], root / 'data' / entry['path'], deadline=deadline)
            self.db.set_progress(op_id, 'files', index)
        budget()
        self.store.put(prefix + 'publication.json', json_bytes(op['marker']))

    def _publication_source(self, op_id: str, op: dict) -> StagedPublication:
        """Resolve each recovery input from durable transfer progress or the completed marker."""
        published = op['phase'] == 'published'
        return StagedPublication(
            op,
            self.store,
            self.config.limits.path_bytes,
            installed_files=len(op['marker']['inventory']) if published else self.db.progress(op_id, 'files'),
            installed_archive=published or bool(self.db.progress(op_id, 'archive')),
        )

    def _execute(
        self,
        op_id: str,
        op: dict,
        *,
        deadline: float | None = None,
        prepared_row: CatalogueEntry | None = None,
    ) -> None:
        """Complete an operation journal while preserving resumable progress."""

        def budget() -> None:
            """Reject work after the current transfer deadline."""
            if self.maintenance.stop.is_set():
                raise ViewerError('operation_cancelled', 'Operation interrupted by shutdown.', 503)
            if deadline is not None and time.monotonic() > deadline:
                raise StorageError()

        if op['kind'] == 'settings':
            self._execute_settings(op_id, op, budget)
            return
        prefix = op['prefix']
        if op['kind'] == 'publish' and op['phase'] in ('backed_up', 'installing'):
            current = self._json(prefix + 'publication.json')
            if current and not isinstance(current, dict):
                raise AttributeError(f"'{type(current).__name__}' object has no attribute 'get'")
            if isinstance(current, dict) and current.get('operation_id') == op_id:
                op['phase'] = 'published'
                self.db.save_operation(op_id, op)
        staged = self._publication_source(op_id, op) if op['kind'] == 'publish' else None
        if staged is not None and prepared_row is None and staged.available():
            staged.validate(budget)
            prepared_row = PublicationReader(self.config, staged)._marker(prefix + 'publication.json')
        if op['phase'] == 'prepared':
            start = self.db.progress(op_id, 'copied')
            for index, key in enumerate(op['old_keys'][start:], start + 1):
                budget()
                self.store.copy(key, op['trash'] + key[len(prefix) :])
                self.db.set_progress(op_id, 'copied', index)
            op['phase'] = 'backed_up'
            self.db.save_operation(op_id, op)
        if op['phase'] in ('backed_up', 'installing') and staged is not None and not staged.available():
            self._rollback_publication(op_id, op, budget)
            return
        if op['phase'] == 'backed_up':
            # Remove the marker first; partial data cannot become a publication.
            keys = [
                prefix + 'publication.json',
                *[key for key in op['old_keys'] if key != prefix + 'publication.json'],
            ]
            start = self.db.progress(op_id, 'removed')
            for index, key in enumerate(keys[start:], start + 1):
                budget()
                self.store.delete(key)
                self.db.set_progress(op_id, 'removed', index)
            op['phase'] = 'installing'
            self.db.save_operation(op_id, op)
        if op['phase'] == 'installing':
            if op['kind'] == 'publish':
                self._install_publication(op_id, op, budget, deadline)
            op['phase'] = 'published'
            self.db.save_operation(op_id, op)
        if op['phase'] == 'published':
            budget()
            self.store.put('settings/viewer-metadata.json', json_bytes(op['metadata']))
            budget()
            self.store.put('settings/basket-order.json', json_bytes(op['order']))
            if op['kind'] == 'publish':
                require(self._object(prefix + 'publication.json') == op['marker'], 'stored_digest_mismatch')
                if prepared_row is None:
                    prepared_row = self._marker(prefix + 'publication.json')
            state = self._prepare_operation(op, prepared_row)
            # Empty baskets leave the active flags as well as the active order.
            budget()
            self.store.put(
                'settings/basket-display.json',
                json_bytes({'version': 1, 'items': state['_persisted_display']}),
            )
            budget()
            self._complete_operation(op_id, state, op)
            if op.get('staging'):
                shutil.rmtree(op['staging'], ignore_errors=True)

    def _require_run(self, rid: str) -> CatalogueEntry:
        """Return a known run or raise the public not-found error."""
        if rid not in self.runs:
            raise ViewerError('not_found', 'Run not found.', 404)
        return self.runs[rid]

    def require_owner(self, rid: str, login: str) -> CatalogueEntry:
        """Verify that the caller owns the selected run."""
        row = self._require_run(rid)
        if row['marker']['owner'] != login:
            raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
        return row

    def _display_limits(self, report: Report, basket: Basket) -> None:
        """Validate public display sizes before publishing a run."""
        limits = self.config.limits
        if (
            len(basket.meta.name) > limits.basket_name_chars
            or len(basket.meta.description) > limits.basket_description_chars
            or len(report.agent.runtime) > limits.runtime_chars
            or any(len(t.title) > limits.task_title_chars for t in report.tasks)
        ):
            raise ViewerError('metadata_limit', 'Display metadata exceeds configured limits.', 413)

    def _stage_publication(
        self,
        imported: ImportedRun,
        login: str,
        key: str,
        rid: str,
        basket: str,
        fingerprint: str,
        op_id: str,
        root: Path,
    ) -> tuple[dict, dict[str, str]]:
        """Stage immutable files and prepare the operation before the caller journals it."""

        def link_or_copy(source: str | Path, target: str | Path) -> str | Path:
            """Reuse a staged file by hard link or copy when linking is unavailable."""
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
            return target

        try:
            link_or_copy(imported.archive, root / 'archive.tar.gz')
            shutil.copytree(imported.directory, root / 'data', copy_function=link_or_copy)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        for file in root.rglob('*'):
            if file.is_file():
                with file.open('rb') as stream:
                    os.fsync(stream.fileno())
        metadata = deepcopy(self.metadata)
        metadata.setdefault(
            rid,
            {
                'display_name': imported.report.agent.name,
                'tags': imported.report.run.tags,
                'pinned': False,
                'created_at': imported.report.run.created_at,
            },
        )
        order = list(self.order)
        if basket not in order:
            order.append(basket)
        prefix = 'runs/' + login + '/' + rid + '/'
        inventory = [
            *(entry.to_wire() for entry in imported.manifest.files),
            {
                'path': 'manifest.json',
                'size_bytes': (root / 'data/manifest.json').stat().st_size,
                'sha256': digest((root / 'data/manifest.json').read_bytes()),
            },
        ]
        marker = {
            'version': 1,
            'owner': login,
            'run_id': rid,
            'basket_sha256': basket,
            'report_sha256': imported.manifest.report_sha256,
            'archive_sha256': imported.archive_sha256,
            'archive_size': imported.archive.stat().st_size,
            'published_at': utc_now(self.db.clock()),
            'operation_id': op_id,
            'inventory': inventory,
        }
        receipt = {
            'run_id': rid,
            'archive_sha256': imported.archive_sha256,
            'report_sha256': imported.manifest.report_sha256,
            'idempotency_key': key,
            'author_login': login,
        }
        op = {
            'kind': 'publish',
            'phase': 'prepared',
            'prefix': prefix,
            'trash': 'trash/' + login + '/' + rid + '/' + utc_now(self.db.clock()).replace(':', '') + '-' + op_id + '/',
            'old_keys': list(self.store.list(prefix)),
            'staging': str(root),
            'marker': marker,
            'metadata': metadata,
            'order': self._preserved_order(order),
            'previous_metadata': deepcopy(self.metadata),
            'previous_order': self._preserved_order(self.order),
            'owner': login,
            'key': key,
            'fingerprint': fingerprint,
            'receipt': receipt,
        }
        return op, receipt

    def publish(
        self,
        imported: ImportedRun,
        login: str,
        key: str,
        *,
        overwrite: bool = False,
        grant_token: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, str]:
        """Validate and publish an uploaded archive under the ownership rules.

        Args:
            imported: Validated archive and extracted evidence to stage durably.
            login: Authenticated publisher login, used for ownership checks.
            key: Idempotency key scoped to login. Repeating the same request
                returns its saved receipt; reuse for different content fails.
            overwrite: Permit replacement of an owned run with the same basket.
            grant_token: Optional one-time publication grant consumed when the
                durable operation is created.
            deadline: Absolute time.monotonic() deadline for executing storage
                transfers, or None for no caller-supplied transfer deadline.

        Returns:
            Publication receipt containing run_id, archive_sha256,
            report_sha256, idempotency_key and author_login.

        Raises:
            ViewerError: If evidence or display fields are invalid, ownership or
                grant checks fail, the key conflicts, or overwrite is forbidden.
                Replaying a receipt for a deleted run also fails.
            StorageError: If storage operations fail or their deadline expires;
                a durable operation remains available for recovery.
            OSError: If local staging files cannot be created or accessed.

        """
        with self.mutation():
            require(imported.report.inference is not None, 'invalid_report')
            try:
                public_inference(imported.report.inference)
            except ValueError:
                raise ViewerError('invalid_report', 'Unsupported or invalid inference contract.') from None
            self._display_limits(imported.report, imported.basket)
            rid = imported.report.run.id
            basket = imported.report.basket.sha256
            fingerprint = digest(
                json_bytes(
                    {
                        'run_id': rid,
                        'archive_sha256': imported.archive_sha256,
                        'report_sha256': imported.manifest.report_sha256,
                        'overwrite': overwrite,
                    }
                )
            )
            previous = self.db.receipt(login, key)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ViewerError('idempotency_conflict', 'Idempotency key belongs to another request.', 409)
                if rid not in self.runs:
                    raise ViewerError(
                        'publication_removed',
                        'Original publication was deleted; submit a fresh publication operation.',
                        404,
                    )
                return previous['receipt']
            reserved = self.db.owner(rid)
            if reserved and reserved['login'] != login:
                raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
            if reserved and reserved['basket_hash'] != basket:
                raise ViewerError('basket_conflict', 'Overwrite requires the same basket.', 409)
            if rid in self.runs and not overwrite:
                raise ViewerError('run_exists', 'Run exists; use explicit overwrite.', 409)
            self.validate_patch({'display_name': imported.report.agent.name, 'tags': imported.report.run.tags})
            op_id = str(uuid.uuid4())
            root = self.staging / op_id
            root.mkdir(mode=0o700)

            op, receipt = self._stage_publication(imported, login, key, rid, basket, fingerprint, op_id, root)
            try:
                prepared_row = PublicationReader(
                    self.config, StagedPublication(op, self.store, self.config.limits.path_bytes)
                )._marker(op['prefix'] + 'publication.json')
                self.db.begin_publication(op_id, op, grant_token)
            except Exception:
                try:
                    durable = op_id in self.db.operations()
                except Exception:
                    durable = True
                if not durable:
                    shutil.rmtree(root, ignore_errors=True)
                raise
            self._execute(op_id, op, deadline=deadline, prepared_row=prepared_row)
            return receipt

    def delete(self, rid: str, *, actor: Actor | None = None, publication: str | None = None) -> dict:
        """Journal and complete deletion while retaining the recoverable trash copy."""
        with self.lock:
            if actor is not None:
                row = self._require_run(rid)
                if actor['role'] != 'admin' and row['marker']['owner'] != actor['login']:
                    raise ViewerError('foreign_run', 'Only the owner or an administrator can delete this run.', 403)
                require(publication is not None, 'invalid_query')
        with self.mutation():
            if actor is not None:
                if rid not in self.runs:
                    return {'run_id': rid, 'deleted': True}
                self.check_report(rid, publication=publication)
            row = self._require_run(rid)
            owner = row['marker']['owner']
            prefix = row['prefix']
            op_id = str(uuid.uuid4())
            metadata = deepcopy(self.metadata)
            metadata.pop(rid)
            hashes = {v['marker']['basket_sha256'] for k, v in self.runs.items() if k != rid}
            op = {
                'kind': 'delete',
                'phase': 'prepared',
                'prefix': prefix,
                'trash': 'trash/'
                + owner
                + '/'
                + rid
                + '/'
                + utc_now(self.db.clock()).replace(':', '')
                + '-'
                + op_id
                + '/',
                'old_keys': list(self.store.list(prefix)),
                'metadata': metadata,
                'order': self._preserved_order([h for h in self.order if h in hashes]),
            }
            self.db.save_operation(op_id, op)
            self._execute(op_id, op)
            return {'run_id': rid, 'deleted': True}

    def validate_patch(self, patch: dict) -> None:
        """Validate owner-editable display metadata."""
        CatalogueBuilder(self.config, self.store, {}).validate_patch(patch)

    def patch_metadata(self, rid: str, login: str, patch: dict) -> dict:
        """Apply an owner-authorized display metadata update."""
        with self.mutation():
            self.require_owner(rid, login)
            self.validate_patch(patch)
            metadata = deepcopy(self.metadata)
            metadata[rid].update(patch)
            self.store.put('settings/viewer-metadata.json', json_bytes(metadata))
            self._install_operation({'metadata': metadata})
            return {'run_id': rid, **metadata[rid]}

    def set_pinned(self, rid: str, pinned: bool) -> dict:
        """Update the administrative pin flag for one run."""
        with self.mutation():
            self._require_run(rid)
            if type(pinned) is not bool:
                raise ViewerError('invalid_pinned', 'pinned must be boolean.')
            metadata = deepcopy(self.metadata)
            metadata[rid]['pinned'] = pinned
            self.store.put('settings/viewer-metadata.json', json_bytes(metadata))
            self._install_operation({'metadata': metadata})
            return {'run_id': rid, 'pinned': pinned}

    def set_order(self, order: list[str]) -> dict:
        """Persist the selected basket order."""
        with self.write_lock:
            self.set_settings(self.settings()['revision'], order, self.display)
            return {'items': list(order)}

    def _validate_display(self, display: dict) -> None:
        """Validate basket visibility settings before persistence."""
        CatalogueBuilder(self.config, self.store, {})._validate_display(display)

    def settings(self) -> dict:
        """Return persisted layout and its optimistic concurrency revision."""
        with self.lock:
            state = {'order': list(self.order), 'display': deepcopy(self.display)}
            items = [
                {
                    'hash': h,
                    'name': next(
                        r['basket']['meta']['name'] for r in self.runs.values() if r['marker']['basket_sha256'] == h
                    ),
                    'description': next(
                        r['basket']['meta']['description']
                        for r in self.runs.values()
                        if r['marker']['basket_sha256'] == h
                    ),
                    **self.display[h],
                }
                for h in self.order
            ]
            return {'revision': digest(json_bytes(state)), **state, 'items': items}

    def set_settings(self, revision: str, order: list[str], display: dict) -> dict:
        """Update layout only when its expected revision still matches."""
        with self.mutation():
            if revision != self.settings()['revision']:
                raise ViewerError('settings_changed', 'Settings changed; reload before saving.', 409)
            require(
                isinstance(order, list)
                and all(isinstance(x, str) for x in order)
                and len(set(order)) == len(order)
                and set(order) == set(self.order),
                'invalid_basket_order',
            )
            self._validate_display(display)
            require(set(display) == set(order), 'invalid_basket_display')
            # The published envelope remains consumable by the pre-layout v1
            # journal executor during rollback. It restores canonical order and
            # metadata; that older UI simply ignores display flags.
            op_id = str(uuid.uuid4())
            op = {
                'kind': 'settings',
                'phase': 'published',
                'prefix': 'settings/',
                'metadata': deepcopy(self.metadata),
                'order': self._preserved_order(order),
                'display': self._preserved_display(display),
            }
            self.db.save_operation(op_id, op)
            self._execute(op_id, op)
            return self.settings()

    def cache_stats(self) -> dict:
        """Describe the current publication cache and storage coverage."""
        return {
            'runs': len(self.runs),
            'baskets': len(self.order),
            'users': self.db.connection.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'last_refresh_at': self.last_refresh,
            'refresh': (
                'running'
                if self.maintenance.refresh_status()['status'] in ('pending', 'running', 'retrying', 'waiting_recovery')
                else 'idle'
            ),
            'refresh_job': self.maintenance.refresh_status(),
            'recovery': self.maintenance.status(),
            'startup': dict(self.startup),
            'quarantined_runs': len(self.quarantined),
        }

    def full_report(self, rid: str) -> Report:
        """Load and validate the complete stored report for one run."""
        self._require_available(rid)
        row = self._require_run(rid)
        content = self.store.get(row['prefix'] + 'data/report.json', self.config.limits.json_bytes)
        require(digest(content) == row['marker']['report_sha256'], 'stored_digest_mismatch')
        report = validate_report(parse_json(content))
        return report

    def check_report(self, rid: str, expected: str | None = None, publication: str | None = None) -> str:
        """Verify requested report and publication identities before reading evidence."""
        self._require_available(rid)
        row = self._require_run(rid)
        for value, key in ((expected, 'report_sha256'), (publication, 'archive_sha256')):
            if value is not None:
                require(
                    isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None,
                    'invalid_query',
                )
                if value != row['marker'][key]:
                    raise ViewerError('run_changed', 'Run changed; reload the run before continuing.', 409)
        return row['marker']['report_sha256']

    def run(self, rid: str, **selection: Unpack[MetricSelection]) -> dict:
        """Build the public run projection using the requested aggregation selection."""
        with self.lock:
            self._require_available(rid)
            row = self._require_run(rid)
            report = row['report']
            metadata = deepcopy(self.metadata[rid])
            checked = aggregate([], **selection)['selection']
            mask = sum(1 << i for i, s in enumerate(STATUSES) if s in checked['statuses'])
            metrics = deepcopy(row['aggregates'][mask])
            scores = metrics.pop('scores')
            metrics['score'] = scores[checked['pr_aggregation'] + '/' + checked['file_aggregation']]
            metrics['selection'] = checked
            return {
                'run_id': rid,
                'report_sha256': row['marker']['report_sha256'],
                'publication_sha256': row['marker']['archive_sha256'],
                'user': row['marker']['owner'],
                'auth_mode': row['auth_mode'],
                'basket_hash': report.basket.sha256,
                'basket': deepcopy(row['basket']['meta']),
                'wall_time_seconds': row['wall_time_seconds'],
                **metadata,
                'tag_groups': tag_groups(metadata['tags'], report.inference),
                'inference': public_inference(report.inference),
                'runtime': report.agent.runtime,
                'status': report.run.status,
                'benchmark': deepcopy(row['benchmark']),
                **metrics,
            }

    def download(self, rid: str, login: str) -> StoredBody:
        """Open an available archive stream after checking run ownership."""
        with self.lock:
            self._require_available(rid)
            row = self.require_owner(rid, login)
            return self.store.open(row['prefix'] + 'archive.tar.gz')
