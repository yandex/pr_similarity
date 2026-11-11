"""Durable catalogue and fenced backup intents sharing the account database."""

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from decimal import Decimal

from pydantic import BaseModel
from viewer.bundles import INDEX
from viewer.catalogue import CatalogueSnapshot
from viewer.errors import ViewerError
from viewer.models.report import ReportAgent, ReportBasket, ReportInference, ReportRun
from viewer.persistence.database import Database
from viewer.presentation.measurements import DisplayMeasurement, Measurement
from viewer.presentation.task_measurements import TaskMeasurements
from viewer.publication import CachedReport, CatalogueEntry

PROJECTION_VERSION = 1
_MODEL_TYPES: dict[str, type[BaseModel]] = {
    cls.__name__: cls for cls in (ReportRun, ReportAgent, ReportBasket, ReportInference)
}
_RECORD_TYPES: dict[str, Callable[..., object]] = {
    cls.__name__: cls for cls in (CachedReport, Measurement, DisplayMeasurement, TaskMeasurements)
}


def _encode(value: object) -> list[object]:
    """Tag supported projection values while preserving decimals and non-string dictionary keys."""
    if isinstance(value, Decimal):
        return ['decimal', str(value)]
    if isinstance(value, BaseModel) and type(value).__name__ in _MODEL_TYPES:
        return ['model', type(value).__name__, value.model_dump(mode='json', exclude_unset=True)]
    if type(value).__name__ in _RECORD_TYPES and is_dataclass(value):
        return ['record', type(value).__name__, {f.name: _encode(getattr(value, f.name)) for f in fields(value)}]
    if isinstance(value, dict):
        return ['dict', [[_encode(k), _encode(v)] for k, v in value.items()]]
    if isinstance(value, (list, tuple)):
        return ['list', [_encode(v) for v in value]]
    if value is None or isinstance(value, (str, int, float, bool)):
        return ['scalar', value]
    raise TypeError(f'Unsupported catalogue value: {type(value).__name__}')


def _decode(value: list) -> object:
    """Reconstruct only the explicitly supported projection model and scalar types."""
    kind = value[0]
    if kind == 'decimal':
        return Decimal(value[1])
    if kind == 'model':
        return _MODEL_TYPES[value[1]].model_validate(value[2])
    if kind == 'record':
        return _RECORD_TYPES[value[1]](**{k: _decode(v) for k, v in value[2].items()})
    if kind == 'dict':
        return {_decode(k): _decode(v) for k, v in value[1]}
    if kind == 'list':
        return [_decode(v) for v in value[1]]
    if kind == 'scalar':
        return value[1]
    raise ValueError('Unsupported catalogue value tag.')


def encode_entry(entry: CatalogueEntry) -> str:
    """Serialize projections losslessly without executable Python payloads."""
    return json.dumps({'version': PROJECTION_VERSION, 'entry': _encode(entry)}, allow_nan=False, separators=(',', ':'))


def decode_entry(data: str) -> CatalogueEntry:
    """Restore typed projections and reject unsupported durable versions."""
    value = json.loads(data)
    if value['version'] != PROJECTION_VERSION:
        raise ViewerError('projection_version', 'Unsupported catalogue projection.', 503)
    decoded = _decode(value['entry'])
    if not isinstance(decoded, dict):
        raise TypeError('Catalogue projection must decode to a dictionary.')
    return CatalogueEntry(**decoded)


@dataclass(frozen=True)
class LocalRecord:
    """One immutable publication identity and its durable mutable state."""

    run_id: str
    owner: str
    generation: int
    metadata_revision: int
    entry: CatalogueEntry
    metadata: dict
    local_path: str | None
    backup_state: str = 'pending'
    cache_state: str = 'ready'
    last_access: float = 0
    deleted: bool = False
    projection_version: int = PROJECTION_VERSION
    cache_bytes: int = 0
    backup_metadata_revision: int = 0
    backup_manifest_key: str | None = None
    backup_valid: bool = True
    superseded_at: float | None = None

    @property
    def archive_sha256(self) -> str:
        """Return the immutable archive identity retained in the catalogue marker."""
        return self.entry['marker']['archive_sha256']


@dataclass(frozen=True)
class BackupJob:
    """A leased exact version; its token fences late worker completion."""

    id: int
    run_id: str
    generation: int
    metadata_revision: int
    kind: str
    worker: str
    token: str
    lease_until: float
    record: LocalRecord
    attempts: int = 0

    @property
    def lease_id(self) -> str:
        """Expose the fencing token associated with this exact execution lease."""
        return self.token


class LocalRepository:
    """Persist local catalogue state within the existing SQLite transaction domain."""

    def __init__(self, database: Database, *, begin_migration: bool = False) -> None:
        """Upgrade the shared account database and initialize durable local publication tables."""
        self.database = database
        self.connection = database.connection
        self.lock = database.lock
        with self.lock:
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version < 3:
                database.backup(database.path.with_name('viewer-v2-backup-' + secrets.token_hex(8) + '.sqlite'))
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                for statement in (
                    (
                        'CREATE TABLE IF NOT EXISTS local_records(run_id TEXT NOT NULL,generation INTEG'
                        'ER NOT NULL,metadata_revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY('
                        'run_id,generation,metadata_revision))'
                    ),
                    (
                        'CREATE TABLE IF NOT EXISTS local_heads(run_id TEXT PRIMARY KEY,generation INTE'
                        'GER NOT NULL,metadata_revision INTEGER NOT NULL)'
                    ),
                    (
                        'CREATE TABLE IF NOT EXISTS local_backup_jobs(id INTEGER PRIMARY KEY AUTOINCREM'
                        'ENT,run_id TEXT NOT NULL,generation INTEGER NOT NULL,metadata_revision INTEGER'
                        ' NOT NULL,kind TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 0,state TEXT NOT '
                        'NULL DEFAULT \'pending\',worker TEXT,token TEXT,lease_until REAL NOT NULL DEFAUL'
                        'T 0,error TEXT,retry_at REAL NOT NULL DEFAULT 0,UNIQUE(run_id,generation,metad'
                        'ata_revision))'
                    ),
                    'CREATE TABLE IF NOT EXISTS local_identity_floors('
                    'run_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,metadata_revision INTEGER NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS local_settings(key TEXT PRIMARY KEY,data TEXT NOT NULL)',
                    (
                        'CREATE TABLE IF NOT EXISTS local_operations(actor TEXT NOT NULL,key TEXT NOT N'
                        'ULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(actor,key))'
                    ),
                    'CREATE TABLE IF NOT EXISTS local_refresh(id TEXT PRIMARY KEY,data TEXT NOT NULL)',
                ):
                    self.connection.execute(statement)
                columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(local_backup_jobs)')}
                if 'attempts' not in columns:
                    self.connection.execute(
                        'ALTER TABLE local_backup_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0'
                    )
                if begin_migration:
                    self.connection.execute(
                        'INSERT OR IGNORE INTO local_refresh VALUES (?,?)',
                        (
                            'legacy-import',
                            json.dumps({'status': 'planning', 'completed': {}, 'settings_applied': False}),
                        ),
                    )
                self.connection.execute('PRAGMA user_version=3')

    def _serialize(self, record: LocalRecord) -> str:
        """Encode a complete local version record with its typed catalogue projection."""
        data = {f.name: getattr(record, f.name) for f in fields(record)}
        data['entry'] = encode_entry(record.entry)
        return json.dumps(data, allow_nan=False, separators=(',', ':'))

    def _deserialize(self, data: str) -> LocalRecord:
        """Restore a local record only when its projection version is supported."""
        value = json.loads(data)
        if value['projection_version'] != PROJECTION_VERSION:
            raise ViewerError('projection_version', 'Unsupported catalogue projection.', 503)
        value['entry'] = decode_entry(value['entry'])
        return LocalRecord(**value)

    def _get(self, run_id: str) -> LocalRecord | None:
        """Read the exact record selected by the active generation and metadata head."""
        row = self.connection.execute(
            (
                'SELECT r.data FROM local_records r JOIN local_heads h USING(run_id,generation,'
                'metadata_revision) WHERE run_id=?'
            ),
            (run_id,),
        ).fetchone()
        return self._deserialize(row['data']) if row else None

    def get(self, run_id: str, *, include_deleted: bool = False) -> LocalRecord | None:
        """Return a detached head record, excluding tombstones unless explicitly requested."""
        with self.lock:
            value = self._get(run_id)
            return value if value and (include_deleted or not value.deleted) else None

    def records(self, *, include_deleted: bool = False) -> list[LocalRecord]:
        """Load detached current heads in deterministic run order."""
        with self.lock:
            rows = self.connection.execute(
                (
                    'SELECT r.data FROM local_records r JOIN local_heads h USING(run_id,generation,'
                    'metadata_revision) ORDER BY run_id'
                )
            ).fetchall()
            values = [self._deserialize(row['data']) for row in rows]
            return [row for row in values if include_deleted or not row.deleted]

    def _retire_head(self, replacement: LocalRecord) -> None:
        """Set the supersession time on every revision of the replaced generation."""
        current = self._get(replacement.run_id)
        if current is None or current.generation == replacement.generation:
            return
        now = self.database.clock()
        rows = self.connection.execute(
            'SELECT data FROM local_records WHERE run_id=? AND generation=?', (current.run_id, current.generation)
        ).fetchall()
        for result in rows:
            record = self._deserialize(result['data'])
            if record.superseded_at is None:
                record = replace(record, superseded_at=now)
                self.connection.execute(
                    'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                    (self._serialize(record), record.run_id, record.generation, record.metadata_revision),
                )

    def _write_record(self, record: LocalRecord) -> None:
        """Insert one revision, switch its active head and advance the catalogue revision."""
        self._retire_head(record)
        self.connection.execute(
            'INSERT INTO local_records VALUES (?,?,?,?)',
            (record.run_id, record.generation, record.metadata_revision, self._serialize(record)),
        )
        self.connection.execute(
            'INSERT OR REPLACE INTO local_heads VALUES (?,?,?)',
            (record.run_id, record.generation, record.metadata_revision),
        )
        self._bump_revision()

    def _bump_revision(self) -> None:
        """Advance the durable catalogue revision inside the caller's transaction."""
        row = self.connection.execute('SELECT data FROM local_settings WHERE key=\'revision\'').fetchone()
        self.connection.execute(
            'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
            ('revision', str((int(row['data']) if row else 0) + 1)),
        )

    def next_generation(self, run_id: str) -> int:
        """Allocate above both the current head and every reconciled remote reservation."""
        with self.lock:
            current = self._get(run_id)
            floor = self.connection.execute(
                'SELECT generation FROM local_identity_floors WHERE run_id=?', (run_id,)
            ).fetchone()
            return max(current.generation if current else 0, floor['generation'] if floor else 0) + 1

    def next_metadata_revision(self, current: LocalRecord) -> int:
        """Skip reserved revisions even when an unregistered later generation exists."""
        with self.lock:
            floor = self.connection.execute(
                'SELECT metadata_revision FROM local_identity_floors WHERE run_id=?', (current.run_id,)
            ).fetchone()
            return max(current.metadata_revision, floor['metadata_revision'] if floor else 0) + 1

    def commit(
        self,
        record: LocalRecord,
        *,
        expected_generation: int | None,
        actor: str,
        idempotency_key: str | None = None,
        fingerprint: str = '',
        receipt: dict | None = None,
        activate_backup: bool = False,
        grant_token: str | None = None,
    ) -> LocalRecord:
        """Commit head, ownership, actor receipt and dormant intent atomically."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.require_reconciled()
            if idempotency_key:
                operation = self.connection.execute(
                    'SELECT fingerprint FROM local_operations WHERE actor=? AND key=?', (actor, idempotency_key)
                ).fetchone()
                if operation and operation['fingerprint'] != fingerprint:
                    raise ViewerError('idempotency_conflict', 'Operation key identifies different content.', 409)
            current = self._get(record.run_id)
            if (current.generation if current else None) != expected_generation:
                raise ViewerError('generation_conflict', 'Run changed during the operation.', 409)
            if record.generation != self.next_generation(record.run_id) or record.metadata_revision != 1:
                raise ViewerError('generation_conflict', 'Invalid next publication generation.', 409)
            marker = record.entry['marker']
            if marker['run_id'] != record.run_id or marker['owner'] != record.owner:
                raise ViewerError('owner_conflict', 'Projection identity differs from its record.', 409)
            owner = self.connection.execute(
                'SELECT login,basket_hash FROM owners WHERE run_id=?', (record.run_id,)
            ).fetchone()
            if owner and (owner['login'] != record.owner or owner['basket_hash'] != marker['basket_sha256']):
                raise ViewerError('owner_conflict', 'Run owner or basket differs.', 409)
            self.connection.execute(
                'INSERT OR IGNORE INTO owners VALUES (?,?,?)', (record.run_id, record.owner, marker['basket_sha256'])
            )
            if grant_token:
                cursor = self.connection.execute(
                    'UPDATE grants SET consumed=1 WHERE token_hash=? AND login=? AND consumed=0',
                    (hashlib.sha256(grant_token.encode()).hexdigest(), actor),
                )
                if cursor.rowcount != 1:
                    raise ViewerError('auth_denied', 'Publication grant is unavailable.', 401)
            self._write_record(record)
            self._intent(record, 'tombstone' if record.deleted else 'archive', activate_backup)
            if receipt is not None:
                if not idempotency_key:
                    raise ValueError('A receipt requires an idempotency key.')
                prior = self.connection.execute(
                    'SELECT fingerprint,data FROM receipts WHERE login=? AND idem_key=?', (actor, idempotency_key)
                ).fetchone()
                if prior:
                    raise ViewerError('idempotency_conflict', 'Publication key was already committed.', 409)
                self.connection.execute(
                    'INSERT INTO receipts VALUES (?,?,?,?)', (actor, idempotency_key, fingerprint, json.dumps(receipt))
                )
                previous_operation = self.connection.execute(
                    'SELECT data FROM local_operations WHERE actor=? AND key=?', (actor, idempotency_key)
                ).fetchone()
                operation_data = json.loads(previous_operation['data']) if previous_operation else {}
                operation_data.update(phase='available', receipt=receipt)
                for field in ('message', 'operation_id'):
                    if field in receipt:
                        operation_data[field] = receipt[field]
                self.connection.execute(
                    'INSERT OR REPLACE INTO local_operations VALUES (?,?,?,?)',
                    (actor, idempotency_key, fingerprint, json.dumps(operation_data)),
                )
        return record

    def _intent(self, record: LocalRecord, kind: str, active: bool) -> None:
        """Insert the exact generation and metadata revision into the backup queue."""
        self.connection.execute(
            ('INSERT INTO local_backup_jobs(run_id,generation,metadata_revision,kind,active)' ' VALUES (?,?,?,?,?)'),
            (record.run_id, record.generation, record.metadata_revision, kind, int(active)),
        )

    def mutate_metadata(
        self, run_id: str, metadata: dict, *, expected_generation: int, expected_metadata_revision: int
    ) -> LocalRecord:
        """CAS editable metadata without changing the archive generation, then queue its backup."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.require_reconciled()
            old = self._get(run_id)
            if (
                old is None
                or old.deleted
                or (old.generation, old.metadata_revision) != (expected_generation, expected_metadata_revision)
            ):
                raise ViewerError('generation_conflict', 'Run changed during the operation.', 409)
            value = replace(
                old, metadata=metadata, metadata_revision=self.next_metadata_revision(old), backup_state='pending'
            )
            self._write_record(value)
            self._intent(value, 'metadata', True)
            return value

    def tombstone(self, run_id: str, *, expected_generation: int, actor: str) -> LocalRecord:
        """Create a new deleted generation while retaining the immutable owner binding."""
        with self.lock:
            old = self._get(run_id)
            if old is None or old.deleted:
                raise ViewerError('not_found', 'Run not found.', 404)
            return self.commit(
                replace(
                    old,
                    generation=self.next_generation(run_id),
                    metadata_revision=1,
                    deleted=True,
                    backup_state='pending',
                    backup_manifest_key=None,
                    backup_metadata_revision=0,
                    backup_valid=False,
                ),
                expected_generation=expected_generation,
                actor=actor,
                activate_backup=True,
            )

    def activate_backup(self, run_id: str) -> None:
        """Make committed intents eligible after the publication response has been sent."""
        with self.lock, self.connection:
            self.connection.execute('UPDATE local_backup_jobs SET active=1 WHERE run_id=? AND active=0', (run_id,))

    def recover_jobs(self) -> None:
        """Activate committed intents after process recovery, including lost replies."""
        with self.lock, self.connection:
            self.connection.execute(
                (
                    'UPDATE local_backup_jobs SET active=1,state=CASE WHEN state=\'uploading\' THEN \''
                    'pending\' ELSE state END,token=NULL,worker=NULL,lease_until=0 WHERE state NOT I'
                    'N (\'backed_up\',\'blocked\',\'superseded\')'
                )
            )

    def claim_backup(self, worker: str, *, lease_seconds: float = 60) -> BackupJob | None:
        """Lease ordered revisions, bypassing blocked chains of older generations.

        A replacement archive has its own payload. An older blocked archive and
        metadata jobs waiting on it must not prevent backing up the replacement.
        Live leases and retryable predecessors still serialize registry updates.
        """
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if self.restore_reconciliation()['status'] == 'required':
                return None
            now = self.database.clock()
            row = self.connection.execute(
                (
                    'SELECT * FROM local_backup_jobs j WHERE active=1 AND ((state IN (\'pending\',\'re'
                    'try_wait\') AND retry_at<=?) OR (state=\'uploading\' AND lease_until<=?)) AND NOT'
                    ' EXISTS (SELECT 1 FROM local_backup_jobs p WHERE p.run_id=j.run_id AND p.id<j.'
                    'id AND p.state NOT IN (\'backed_up\',\'superseded\') AND (p.generation>=j.generati'
                    'on OR NOT EXISTS (SELECT 1 FROM local_backup_jobs b WHERE b.run_id=p.run_id AN'
                    'D b.generation=p.generation AND b.id<=p.id AND b.state=\'blocked\'))) ORDER BY i'
                    'd LIMIT 1'
                ),
                (now, now),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            deadline = now + lease_seconds
            self.connection.execute(
                (
                    'UPDATE local_backup_jobs SET state=\'uploading\',worker=?,token=?,lease_until=?,'
                    'attempts=attempts+1 WHERE id=?'
                ),
                (worker, token, deadline, row['id']),
            )
            data = self.connection.execute(
                'SELECT data FROM local_records WHERE run_id=? AND generation=? AND metadata_revision=?',
                (row['run_id'], row['generation'], row['metadata_revision']),
            ).fetchone()
            return BackupJob(
                row['id'],
                row['run_id'],
                row['generation'],
                row['metadata_revision'],
                row['kind'],
                worker,
                token,
                deadline,
                self._deserialize(data['data']),
                row['attempts'] + 1,
            )

    def renew_backup(self, job: BackupJob, *, lease_seconds: float = 60) -> bool:
        """Extend only an unexpired lease still owned by the same worker and token."""
        with self.lock, self.connection:
            now = self.database.clock()
            cursor = self.connection.execute(
                (
                    'UPDATE local_backup_jobs SET lease_until=? WHERE id=? AND token=? AND worker=?'
                    ' AND state=\'uploading\' AND lease_until>?'
                ),
                (now + lease_seconds, job.id, job.token, job.worker, now),
            )
            return cursor.rowcount == 1

    def finish_backup(
        self,
        job: BackupJob,
        *,
        manifest_key: str | None = None,
        error: str | None = None,
        retry_seconds: float = 30,
        blocked: bool = False,
    ) -> bool:
        """Fence worker completion and confirm only the exact immutable closing manifest."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            now = self.database.clock()
            state = 'blocked' if blocked else ('retry_wait' if error else 'backed_up')
            saved = self.connection.execute(
                (
                    'SELECT r.data FROM local_backup_jobs j JOIN local_records r USING(run_id,gener'
                    'ation,metadata_revision) WHERE j.id=? AND j.token=? AND j.worker=? AND j.state'
                    '=\'uploading\' AND j.lease_until>?'
                ),
                (job.id, job.token, job.worker, now),
            ).fetchone()
            if saved is None:
                return False
            persisted = self._deserialize(saved['data'])
            if (persisted.run_id, persisted.generation, persisted.metadata_revision) != (
                job.run_id,
                job.generation,
                job.metadata_revision,
            ):
                raise ViewerError('backup_identity', 'Backup job identity differs from its durable record.', 409)
            if state == 'backed_up':
                expected_key = (
                    f'{INDEX}{persisted.owner}/{persisted.run_id}/'
                    f'{persisted.generation}/{persisted.metadata_revision}.json'
                )
                if manifest_key != expected_key:
                    raise ViewerError(
                        'backup_manifest', 'Verified backup requires its exact closing manifest key.', 409
                    )
            cursor = self.connection.execute(
                (
                    'UPDATE local_backup_jobs SET state=?,error=?,retry_at=?,lease_until=0 WHERE id'
                    '=? AND token=? AND worker=? AND state=\'uploading\' AND lease_until>?'
                ),
                (state, error, now + retry_seconds, job.id, job.token, job.worker, now),
            )
            if cursor.rowcount != 1:
                return False
            row = self.connection.execute(
                'SELECT data FROM local_records WHERE run_id=? AND generation=? AND metadata_revision=?',
                (job.run_id, job.generation, job.metadata_revision),
            ).fetchone()
            record = replace(self._deserialize(row['data']), backup_state=state)
            if state == 'backed_up':
                record = replace(
                    record,
                    backup_metadata_revision=job.metadata_revision,
                    backup_manifest_key=manifest_key,
                    backup_valid=True,
                )
            self.connection.execute(
                'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                (self._serialize(record), job.run_id, job.generation, job.metadata_revision),
            )
            if state == 'backed_up':
                later_rows = self.connection.execute(
                    'SELECT data FROM local_records WHERE run_id=? AND generation=? AND metadata_revision>?',
                    (job.run_id, job.generation, job.metadata_revision),
                ).fetchall()
                for later in later_rows:
                    current = self._deserialize(later['data'])
                    if current.backup_metadata_revision >= job.metadata_revision:
                        continue
                    current = replace(
                        current, backup_metadata_revision=job.metadata_revision, backup_manifest_key=manifest_key
                    )
                    self.connection.execute(
                        'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                        (self._serialize(current), current.run_id, current.generation, current.metadata_revision),
                    )
            return True

    def invalidate_backup(self, run_id: str, generation: int, error: str) -> bool:
        """Protect a local generation when its remote evidence fails verification."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            row = self._get(run_id)
            if row is None or row.generation != generation:
                return False
            row = replace(row, backup_valid=False, backup_state='blocked')
            self.connection.execute(
                'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                (self._serialize(row), run_id, generation, row.metadata_revision),
            )
            self.connection.execute(
                'UPDATE local_backup_jobs SET error=? WHERE run_id=? AND generation=?', (error, run_id, generation)
            )
            return True

    def update_local_state(
        self,
        run_id: str,
        *,
        generation: int,
        local_path: str | None,
        cache_state: str,
        last_access: float | None = None,
        backup_state: str | None = None,
        cache_bytes: int | None = None,
    ) -> bool:
        """CAS a restore/eviction/access result without replacing current metadata."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            rows = self.connection.execute(
                'SELECT data FROM local_records WHERE run_id=? AND generation=?', (run_id, generation)
            ).fetchall()
            for result in rows:
                row = self._deserialize(result['data'])
                row = replace(
                    row,
                    local_path=local_path,
                    cache_state=cache_state,
                    last_access=row.last_access if last_access is None else max(row.last_access, last_access),
                    backup_state=backup_state or row.backup_state,
                    cache_bytes=row.cache_bytes if cache_bytes is None else cache_bytes,
                )
                self.connection.execute(
                    'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                    (self._serialize(row), row.run_id, row.generation, row.metadata_revision),
                )
            return bool(rows)

    def touch_access(self, run_id: str, generation: int, at: float) -> None:
        """Record reader completion without restoring its possibly retired file path."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            rows = self.connection.execute(
                'SELECT data FROM local_records WHERE run_id=? AND generation=?', (run_id, generation)
            ).fetchall()
            for result in rows:
                row = self._deserialize(result['data'])
                row = replace(row, last_access=max(row.last_access, at))
                self.connection.execute(
                    'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                    (self._serialize(row), row.run_id, row.generation, row.metadata_revision),
                )

    def versions(self) -> list[LocalRecord]:
        """Return the latest metadata revision of every retained generation."""
        with self.lock:
            rows = self.connection.execute(
                (
                    'SELECT r.data FROM local_records r WHERE metadata_revision=(SELECT MAX(p.metad'
                    'ata_revision) FROM local_records p WHERE p.run_id=r.run_id AND p.generation=r.'
                    'generation) ORDER BY run_id,generation'
                )
            ).fetchall()
            return [self._deserialize(row['data']) for row in rows]

    def observe_retired_versions(self) -> None:
        """Start a conservative retention clock for preexisting historical rows."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            rows = self.connection.execute(
                (
                    'SELECT r.data FROM local_records r JOIN local_heads h ON h.run_id=r.run_id WHE'
                    'RE r.generation<h.generation'
                )
            ).fetchall()
            now = self.database.clock()
            for result in rows:
                record = self._deserialize(result['data'])
                if record.superseded_at is None:
                    record = replace(record, superseded_at=now)
                    self.connection.execute(
                        'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                        (self._serialize(record), record.run_id, record.generation, record.metadata_revision),
                    )

    def _retention_candidate(self, run_id: str, generation: int, before: float) -> LocalRecord | None:
        """Select an expired historical generation only when every backup requirement is satisfied."""
        head = self._get(run_id)
        if head is None or head.generation <= generation:
            return None
        result = self.connection.execute(
            (
                'SELECT data FROM local_records WHERE run_id=? AND generation=? ORDER BY metada'
                'ta_revision DESC LIMIT 1'
            ),
            (run_id, generation),
        ).fetchone()
        if result is None:
            return None
        record = self._deserialize(result['data'])
        expected_key = f'{INDEX}{record.owner}/{record.run_id}/{record.generation}/{record.metadata_revision}.json'
        if (
            record.superseded_at is None
            or max(record.superseded_at, record.last_access) > before
            or record.backup_state != 'backed_up'
            or not record.backup_valid
            or record.backup_metadata_revision != record.metadata_revision
            or record.backup_manifest_key != expected_key
        ):
            return None
        pending = self.connection.execute(
            (
                'SELECT 1 FROM local_backup_jobs WHERE run_id=? AND generation=? '
                'AND state NOT IN (\'backed_up\',\'superseded\') LIMIT 1'
            ),
            (run_id, generation),
        ).fetchone()
        return None if pending else record

    def retention_candidate(self, run_id: str, generation: int, *, before: float) -> LocalRecord | None:
        """Read an eligible historical cleanup candidate under the shared database lock."""
        with self.lock:
            return self._retention_candidate(run_id, generation, before)

    def prune_version(self, record: LocalRecord, *, before: float) -> bool:
        """Remove only the unchanged confirmed historical generation's local index."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            current = self._retention_candidate(record.run_id, record.generation, before)
            if current != record:
                return False
            self.connection.execute(
                'DELETE FROM local_backup_jobs WHERE run_id=? AND generation=?', (record.run_id, record.generation)
            )
            self.connection.execute(
                'DELETE FROM local_records WHERE run_id=? AND generation=?', (record.run_id, record.generation)
            )
            return True

    def merge_remote(
        self, record: LocalRecord, *, expected_generation: int | None, expected_metadata_revision: int | None
    ) -> bool:
        """CAS a verified remote bundle without scheduling writes back to storage."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            old = self._get(record.run_id)
            actual = (old.generation, old.metadata_revision) if old else (None, None)
            if actual != (expected_generation, expected_metadata_revision):
                return False
            marker = record.entry['marker']
            if marker['run_id'] != record.run_id or marker['owner'] != record.owner:
                raise ViewerError('owner_conflict', 'Projection identity differs from its record.', 409)
            owner = self.connection.execute(
                'SELECT login,basket_hash FROM owners WHERE run_id=?', (record.run_id,)
            ).fetchone()
            if owner and (owner['login'] != record.owner or owner['basket_hash'] != marker['basket_sha256']):
                raise ViewerError('owner_conflict', 'Remote owner or basket differs.', 409)
            existing = self.connection.execute(
                'SELECT data FROM local_records WHERE run_id=? AND generation=? LIMIT 1',
                (record.run_id, record.generation),
            ).fetchone()
            if existing:
                identity = self._deserialize(existing['data'])
                if identity.archive_sha256 != record.archive_sha256 or identity.deleted != record.deleted:
                    raise ViewerError('generation_conflict', 'Remote generation has conflicting content.', 409)
            if old and (record.generation, record.metadata_revision) < (old.generation, old.metadata_revision):
                return False
            if old and record.generation == old.generation:
                if record.metadata_revision == old.metadata_revision and record.metadata != old.metadata:
                    raise ViewerError('generation_conflict', 'Remote metadata revision has conflicting content.', 409)
                record = replace(record, last_access=old.last_access)
            self.connection.execute(
                'INSERT OR IGNORE INTO owners VALUES (?,?,?)', (record.run_id, record.owner, marker['basket_sha256'])
            )
            self._retire_head(record)
            self.connection.execute(
                'INSERT OR REPLACE INTO local_records VALUES (?,?,?,?)',
                (record.run_id, record.generation, record.metadata_revision, self._serialize(record)),
            )
            self.connection.execute(
                'INSERT OR REPLACE INTO local_heads VALUES (?,?,?)',
                (record.run_id, record.generation, record.metadata_revision),
            )
            self._bump_revision()
            return True

    def restore_reconciliation(self) -> dict:
        """Return the durable write barrier without accessing remote storage."""
        with self.lock:
            row = self.connection.execute(
                'SELECT data FROM local_settings WHERE key=?', ('restore_reconciliation',)
            ).fetchone()
            return json.loads(row['data']) if row else {'status': 'not_required'}

    def require_reconciled(self) -> None:
        """Reject new identities until a restored checkpoint has a current audit."""
        if self.restore_reconciliation()['status'] == 'required':
            raise ViewerError(
                'restore_reconciliation_required',
                'Restore reconciliation is required; run a complete administrative refresh before writing.',
                503,
            )

    def bind_restore_refresh(self, request_id: str) -> None:
        """Bind recovery to a refresh explicitly requested after this restore."""
        with self.lock, self.connection:
            state = self.restore_reconciliation()
            if state['status'] == 'required':
                state.update(request_id=request_id, error_code=None)
                self.connection.execute(
                    'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
                    ('restore_reconciliation', json.dumps(state)),
                )

    def restore_reconciliation_failed(self, code: str) -> None:
        """Keep the barrier and expose a failed completeness check to operators."""
        with self.lock, self.connection:
            state = self.restore_reconciliation()
            if state['status'] == 'required':
                state['error_code'] = code
                self.connection.execute(
                    'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
                    ('restore_reconciliation', json.dumps(state)),
                )

    def _restored_heads_cover(self, plan: list[dict], results: dict) -> bool:
        """Check every audited identity against its durably merged local head."""
        for item in plan:
            identity = item['identity']
            current = self._get(identity['run_id'])
            if results.get(item['manifest_key'], {}).get('result') not in ('updated', 'protected'):
                return False
            if current is None or current.owner != identity['owner']:
                return False
            if (current.generation, current.metadata_revision) < (
                identity['generation'],
                identity['metadata_revision'],
            ):
                return False
            if current.generation == identity['generation'] and (
                current.archive_sha256 != identity['archive_sha256'] or current.deleted != item['tombstone']
            ):
                return False
        return True

    def _release_restored_jobs(self, plan: list[dict]) -> None:
        """Retire stale intents and resume only metadata with a verified archive."""
        for item in plan:
            identity = item['identity']
            current = self._get(identity['run_id'])
            assert current is not None
            self.connection.execute(
                (
                    'UPDATE local_backup_jobs SET state=?,active=0 WHERE run_id=? AND state=? AND ('
                    'generation<? OR (generation=? AND metadata_revision<=?))'
                ),
                (
                    'superseded',
                    current.run_id,
                    'blocked',
                    identity['generation'],
                    identity['generation'],
                    identity['metadata_revision'],
                ),
            )
            if (
                current.generation == identity['generation']
                and current.metadata_revision > identity['metadata_revision']
            ):
                self.connection.execute(
                    (
                        'UPDATE local_backup_jobs SET state=?,active=0 WHERE run_id=? AND generation=? '
                        'AND state=? AND metadata_revision<?'
                    ),
                    ('superseded', current.run_id, current.generation, 'blocked', current.metadata_revision),
                )
                self.connection.execute(
                    (
                        'UPDATE local_backup_jobs SET state=?,active=1,error=NULL,retry_at=0 WHERE run_'
                        'id=? AND generation=? AND metadata_revision=? AND state=?'
                    ),
                    ('pending', current.run_id, current.generation, current.metadata_revision, 'blocked'),
                )

    def _release_restored_tombstones(self, plan: list[dict]) -> None:
        """Resume current deletion intents above the verified remote generation floor."""
        remote = {item['identity']['run_id']: item['identity'] for item in plan}
        for current in self.records(include_deleted=True):
            if not current.deleted:
                continue
            previous = remote.get(current.run_id)
            if previous is not None and current.generation <= previous['generation']:
                continue
            self.connection.execute(
                'UPDATE local_backup_jobs SET state=?,active=1,error=NULL,retry_at=0 '
                'WHERE run_id=? AND generation=? AND metadata_revision=? AND kind=? AND state=?',
                ('pending', current.run_id, current.generation, current.metadata_revision, 'tombstone', 'blocked'),
            )

    def complete_restore_reconciliation(
        self, request_id: str, head_sha256: str, count: int, floors: dict[str, tuple[int, int]]
    ) -> bool:
        """Remove the barrier only for a fresh complete audit of the current registry."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            state = self.restore_reconciliation()
            if state['status'] != 'required':
                return True
            audit = self.load_refresh('active')
            if audit is None or state.get('request_id') != request_id or audit.get('request_id') != request_id:
                return False
            plan = audit.get('plan', [])
            if (
                audit.get('status') != 'succeeded'
                or not audit.get('discovery_complete')
                or audit.get('discovery_failed')
                or audit.get('errors')
                or audit.get('deferred')
                or audit.get('checked') != count
                or len(plan) != count
                or audit.get('boundary', {}).get('head_sha256') != head_sha256
                or not self._restored_heads_cover(plan, audit.get('results', {}))
            ):
                return False
            for rid, (generation, revision) in floors.items():
                self.connection.execute(
                    'INSERT INTO local_identity_floors VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET '
                    'generation=MAX(generation,excluded.generation),'
                    'metadata_revision=MAX(metadata_revision,excluded.metadata_revision)',
                    (rid, generation, revision),
                )
            self._release_restored_jobs(plan)
            self._release_restored_tombstones(plan)
            state.update(status='reconciled', boundary=audit['boundary'], error_code=None)
            self.connection.execute(
                'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
                ('restore_reconciliation', json.dumps(state)),
            )
            return True

    def settings(self) -> dict:
        """Load detached durable settings without consulting remote storage."""
        with self.lock:
            return {r['key']: json.loads(r['data']) for r in self.connection.execute('SELECT * FROM local_settings')}

    def registry_initialized(self) -> bool:
        """Report whether this runtime has previously verified or created the remote registry."""
        with self.lock:
            row = self.connection.execute(
                'SELECT data FROM local_settings WHERE key=?', ('registry_initialized',)
            ).fetchone()
            return row is not None and json.loads(row['data']) is True

    def mark_registry_initialized(self) -> None:
        """Persist remote-registry provenance only after its authoritative head verifies."""
        with self.lock, self.connection:
            self.connection.execute(
                'INSERT OR REPLACE INTO local_settings VALUES (?,?)', ('registry_initialized', 'true')
            )

    def save_settings(self, *, order: list[str], display: dict) -> None:
        """Commit basket order and display flags together with a catalogue revision."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            for key, value in (('order', order), ('display', display)):
                self.connection.execute('INSERT OR REPLACE INTO local_settings VALUES (?,?)', (key, json.dumps(value)))
            self._bump_revision()

    def load_snapshot(self) -> CatalogueSnapshot:
        """Build the visible catalogue and quarantine metadata entirely from durable local state."""
        with self.lock:
            all_rows = self.records()
            rows = [row for row in all_rows if row.cache_state != 'unavailable']
            quarantined = {
                row.run_id: {
                    'code': 'local_version_unavailable',
                    'generation': row.generation,
                    'basket_hash': row.entry['marker']['basket_sha256'],
                }
                for row in all_rows
                if row.cache_state == 'unavailable'
            }
            settings = self.settings()
            hashes = {r.entry['marker']['basket_sha256'] for r in rows}
            protected_hashes = {r.entry['marker']['basket_sha256'] for r in all_rows}
            stored_order = [h for h in settings.get('order', []) if h in protected_hashes]
            order = [h for h in stored_order if h in hashes]
            first = {
                h: min(r.entry['marker']['published_at'] for r in rows if r.entry['marker']['basket_sha256'] == h)
                for h in hashes
            }
            order += sorted(hashes - set(order), key=lambda h: (first[h], h))
            display = {h: settings.get('display', {}).get(h, {'hidden': False, 'collapsed': False}) for h in order}
            persisted_order = stored_order + [h for h in order if h not in stored_order]
            persisted_display = {
                h: settings.get('display', {}).get(h, {'hidden': False, 'collapsed': False}) for h in persisted_order
            }
            return CatalogueSnapshot(
                quarantined=quarantined,
                runs={r.run_id: r.entry for r in rows},
                metadata={r.run_id: r.metadata for r in all_rows},
                order=order,
                display=display,
                persisted_order=persisted_order,
                persisted_display=persisted_display,
                revision=settings.get('revision', 0),
                last_refresh=settings.get('last_refresh'),
            )

    def operation(self, actor: str, key: str) -> dict | None:
        """Read an actor-scoped publication operation and its immutable request fingerprint."""
        with self.lock:
            row = self.connection.execute(
                'SELECT fingerprint,data FROM local_operations WHERE actor=? AND key=?', (actor, key)
            ).fetchone()
            return {'fingerprint': row['fingerprint'], **json.loads(row['data'])} if row else None

    def operation_by_grant(self, key: str, grant_hash: str) -> tuple[str, dict] | None:
        """Read an admitted operation independently of ephemeral grant retention."""
        with self.lock:
            matches = []
            for row in self.connection.execute(
                'SELECT actor,fingerprint,data FROM local_operations WHERE key=?', (key,)
            ):
                data = json.loads(row['data'])
                if isinstance(data.get('grant_hash'), str) and secrets.compare_digest(data['grant_hash'], grant_hash):
                    matches.append((row['actor'], {**data, 'fingerprint': row['fingerprint']}))
            return matches[0] if len(matches) == 1 else None

    def set_operation(self, actor: str, key: str, fingerprint: str, data: dict) -> None:
        """Persist progress without changing the request identity or replacing a terminal receipt."""
        with self.lock, self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            old = self.connection.execute(
                'SELECT fingerprint,data FROM local_operations WHERE actor=? AND key=?', (actor, key)
            ).fetchone()
            if old and old['fingerprint'] != fingerprint:
                raise ViewerError('idempotency_conflict', 'Operation key identifies different content.', 409)
            if old and json.loads(old['data']).get('phase') == 'available':
                return
            self.connection.execute(
                'INSERT OR REPLACE INTO local_operations VALUES (?,?,?,?)', (actor, key, fingerprint, json.dumps(data))
            )

    def save_refresh(self, refresh_id: str, data: dict) -> None:
        """Persist a resumable discovery plan and per-identity outcomes."""
        with self.lock, self.connection:
            self.connection.execute('INSERT OR REPLACE INTO local_refresh VALUES (?,?)', (refresh_id, json.dumps(data)))
            if data.get('status') == 'succeeded':
                self.connection.execute(
                    'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
                    ('last_refresh', json.dumps(data.get('last_success_at'))),
                )

    def load_refresh(self, refresh_id: str) -> dict | None:
        """Read the saved discovery plan and outcomes for one audit request."""
        with self.lock:
            row = self.connection.execute('SELECT data FROM local_refresh WHERE id=?', (refresh_id,)).fetchone()
            return json.loads(row['data']) if row else None
