"""Closed private SQLite checkpoints and explicit offline disaster recovery."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from viewer.bundles import BundleRegistrySnapshot, BundleRepository
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_repository import (
    PROJECTION_VERSION,
    LocalRecord,
    LocalRepository,
    decode_entry,
    encode_entry,
)
from viewer.persistence.database import Database
from viewer.persistence.storage import Store
from viewer.util import digest, json_bytes, safe_path, utc_now

CHECKPOINT_VERSION = 1
CHECKPOINT_PREFIX = 'checkpoints/v1/'
CHECKPOINT_DATA_PREFIX = 'checkpoint-data/v1/'


def ensure_registry(
    local_repository: LocalRepository,
    bundles: BundleRepository,
    lock: 'threading.RLock',
) -> BundleRegistrySnapshot:
    """Bootstrap only a pristine namespace and retain durable evidence against later head loss."""
    with lock:
        local_repository.require_reconciled()
        if local_repository.registry_initialized():
            return bundles.registry_snapshot()
        snapshot = bundles.initialize_registry()
        local_repository.mark_registry_initialized()
        return snapshot


def _require(condition: bool) -> None:
    """Reject an incomplete or unsupported checkpoint contract with a stable public error."""
    if not condition:
        raise ViewerError('invalid_checkpoint', 'Checkpoint is incomplete or incompatible.', 409)


def _read_record(data: str) -> LocalRecord:
    """Decode a checkpoint record without opening or modifying the live repository."""
    value = json.loads(data)
    _require(value.get('projection_version') == PROJECTION_VERSION)
    value['entry'] = decode_entry(value['entry'])
    return LocalRecord(**value)


def _record_wire(record: LocalRecord) -> str:
    """Serialize a rewritten restore record with its original typed projection."""
    data = dict(vars(record))
    data['entry'] = encode_entry(record.entry)
    return json.dumps(data, allow_nan=False)


def _versions(connection: sqlite3.Connection) -> list[LocalRecord]:
    """Select the latest metadata revision of each retained archive generation."""
    return [
        _read_record(row[0])
        for row in connection.execute(
            (
                'SELECT r.data FROM local_records r WHERE metadata_revision=(SELECT MAX(p.metad'
                'ata_revision) FROM local_records p WHERE p.run_id=r.run_id AND p.generation=r.'
                'generation) ORDER BY run_id,generation'
            )
        )
    ]


def _validate_database(connection: sqlite3.Connection) -> None:
    """Reject incompatible or inconsistent metadata before installing a runtime."""
    _require(connection.execute('PRAGMA user_version').fetchone()[0] == 3)
    _require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
    _require(not connection.execute('SELECT name FROM sqlite_master WHERE type IN (\'trigger\',\'view\')').fetchall())
    tables = {row[0] for row in connection.execute('SELECT name FROM sqlite_master WHERE type=\'table\'')}
    _require(
        {
            'users',
            'owners',
            'receipts',
            'local_records',
            'local_heads',
            'local_backup_jobs',
            'local_settings',
            'local_operations',
            'local_refresh',
        }
        <= tables
    )
    for login, password, enabled, role in connection.execute('SELECT login,password,enabled,role FROM users'):
        _require(
            isinstance(login, str) and isinstance(password, str) and enabled in (0, 1) and role in ('user', 'admin')
        )
    owners = {
        rid: (owner, basket) for rid, owner, basket in connection.execute('SELECT run_id,login,basket_hash FROM owners')
    }
    for rid, generation, revision, raw in connection.execute(
        'SELECT run_id,generation,metadata_revision,data FROM local_records'
    ):
        record = _read_record(raw)
        _require((record.run_id, record.generation, record.metadata_revision) == (rid, generation, revision))
        _require(owners.get(rid) == (record.owner, record.entry['marker']['basket_sha256']))
        _require(record.entry['marker']['run_id'] == rid and record.entry['marker']['owner'] == record.owner)
    _require(
        not connection.execute(
            (
                'SELECT h.run_id FROM local_heads h LEFT JOIN local_records r USING(run_id,gene'
                'ration,metadata_revision) WHERE r.run_id IS NULL'
            )
        ).fetchall()
    )
    for actor, key, fingerprint, data in connection.execute('SELECT login,idem_key,fingerprint,data FROM receipts'):
        _require(all(isinstance(value, str) and value for value in (actor, key)) and isinstance(fingerprint, str))
        _require(isinstance(json.loads(data), dict))
    _require(not connection.execute('SELECT id FROM operations').fetchall())


def _file_identity(path: Path) -> tuple[int, str]:
    """Stream a file to calculate its complete size and SHA-256 digest."""
    sha = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(1 << 20):
            size += len(chunk)
            sha.update(chunk)
    return size, sha.hexdigest()


def _verify_object(store: Store, reference: dict, *, deadline: float, destination: Path | None = None) -> None:
    """Verify a bounded remote object and optionally fsync its downloaded bytes."""
    _require(isinstance(reference, dict) and set(reference) == {'key', 'size', 'sha256'})
    _require(isinstance(reference['key'], str))
    safe_path(reference['key'])
    _require(type(reference['size']) is int and reference['size'] >= 0)
    _require(isinstance(reference['sha256'], str) and len(reference['sha256']) == 64)
    sha = hashlib.sha256()
    size = 0
    output = destination.open('xb') if destination else None
    try:
        with store.open(reference['key']) as source:
            while chunk := source.read(1 << 20):
                size += len(chunk)
                _require(size <= reference['size'] and time.monotonic() <= deadline)
                sha.update(chunk)
                if output:
                    output.write(chunk)
        _require(size == reference['size'] and sha.hexdigest() == reference['sha256'])
        if output:
            output.flush()
            os.fsync(output.fileno())
    finally:
        if output:
            output.close()


def _reference(key: str, data: bytes) -> dict:
    """Bind an immutable object key to the exact size and digest of its bytes."""
    return {'key': key, 'size': len(data), 'sha256': digest(data)}


def _verify_dependency(bundles: BundleRepository, dependency: dict, *, deadline: float) -> None:
    """Verify every object in a closed bundle and match its declared checkpoint identity."""
    _require(
        isinstance(dependency, dict)
        and set(dependency) == {'run_id', 'generation', 'archive_sha256', 'manifest_key', 'objects'}
    )
    _require(isinstance(dependency['objects'], list))
    for reference in dependency['objects']:
        _verify_object(bundles.store, reference, deadline=deadline)
    descriptor = bundles.read_descriptor(dependency['manifest_key'])
    identity = descriptor.identity
    _require(
        (identity.run_id, identity.generation, identity.archive_sha256)
        == (dependency['run_id'], dependency['generation'], dependency['archive_sha256'])
    )
    required = {identity.closing_key, identity.metadata_key}
    if not descriptor.tombstone:
        required.add(identity.archive_key)
    _require({ref['key'] for ref in dependency['objects']} == required)


@dataclass(frozen=True)
class RestoreResult:
    """Report the restored target and exact heads unavailable after disk loss."""

    runtime: Path
    restored_runs: list[str]
    lost_runs: list[str]


class CheckpointRepository:
    """Create private account checkpoints only after all declared dependencies verify."""

    def __init__(
        self,
        config: Config,
        db: Database,
        local_repository: LocalRepository,
        bundles: BundleRepository,
        registry_lock: 'threading.RLock | None' = None,
    ) -> None:
        """Bind the account snapshot source and the private remote bundle namespace."""
        self.config, self.db, self.local_repository, self.bundles = config, db, local_repository, bundles
        self.registry_lock = registry_lock or threading.RLock()

    def create(self) -> str:
        """Publish a checkpoint closure only after its database snapshot and dependencies verify."""
        ensure_registry(self.local_repository, self.bundles, self.registry_lock)
        checkpoint_id = str(uuid.uuid4())
        key = CHECKPOINT_PREFIX + checkpoint_id + '/manifest.json'
        database_key = CHECKPOINT_DATA_PREFIX + checkpoint_id + '/viewer.sqlite'
        with tempfile.TemporaryDirectory(prefix='viewer-checkpoint-') as directory:
            snapshot = Path(directory) / 'viewer.sqlite'
            self.db.backup(snapshot)
            with closing(sqlite3.connect(snapshot)) as connection:
                _validate_database(connection)
                records = _versions(connection)
            dependencies = []
            unavailable = []
            deadline = time.monotonic() + self.config.limits.transfer_seconds
            for record in records:
                if record.backup_manifest_key and record.backup_valid:
                    descriptor = self.bundles.read_descriptor(record.backup_manifest_key)
                    identity = descriptor.identity
                    _require(
                        (identity.run_id, identity.owner, identity.generation, identity.archive_sha256)
                        == (record.run_id, record.owner, record.generation, record.archive_sha256)
                    )
                    _require(
                        identity.metadata_revision == record.backup_metadata_revision
                        and descriptor.tombstone == record.deleted
                    )
                    objects = [
                        _reference(object_key, self.bundles.store.get(object_key, self.config.limits.json_bytes))
                        for object_key in (identity.closing_key, identity.metadata_key)
                    ]
                    if not descriptor.tombstone:
                        objects.append(
                            {
                                'key': identity.archive_key,
                                'size': descriptor.archive_size,
                                'sha256': identity.archive_sha256,
                            }
                        )
                    dependency = {
                        'run_id': record.run_id,
                        'generation': record.generation,
                        'archive_sha256': record.archive_sha256,
                        'manifest_key': identity.closing_key,
                        'objects': objects,
                    }
                    _verify_dependency(self.bundles, dependency, deadline=deadline)
                    dependencies.append(dependency)
                elif not record.deleted:
                    unavailable.append(
                        {
                            'run_id': record.run_id,
                            'generation': record.generation,
                            'archive_sha256': record.archive_sha256,
                        }
                    )
            size, sha = _file_identity(snapshot)
            reference = {'key': database_key, 'size': size, 'sha256': sha}
            self.bundles.store.put_file(database_key, snapshot, deadline=deadline)
            _verify_object(self.bundles.store, reference, deadline=deadline)
            manifest = {
                'version': CHECKPOINT_VERSION,
                'database_version': 3,
                'created_at': utc_now(self.db.clock()),
                'database': reference,
                'dependencies': dependencies,
                'unavailable': unavailable,
            }
            data = json_bytes(manifest)
            _require(len(data) <= self.config.limits.json_bytes)
            self.bundles.store.put(key, data)
            _require(self.bundles.store.get(key, self.config.limits.json_bytes) == data)
            return key


def _target(target: Path) -> Path:
    """Require an absent restore directory beneath an existing path without symlinks."""
    if '..' in Path(target).parts:
        raise ViewerError('unsafe_runtime', 'Restore target must not contain parent traversal.', 409)
    target = Path(os.path.abspath(target))
    for parent in (target, *target.parents):
        if parent.is_symlink():
            raise ViewerError('unsafe_runtime', 'Restore target cannot traverse symbolic links.', 409)
    if target.exists():
        raise ViewerError('restore_exists', 'Restore requires a new runtime directory.', 409)
    if not target.parent.is_dir():
        raise ViewerError('unsafe_runtime', 'Restore parent directory must already exist.', 409)
    return target


def _fsync_directory(path: Path) -> None:
    """Persist the directory entry that installs the restored database."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reset_restored_work(connection: sqlite3.Connection, checkpoint_key: str) -> None:
    """Fence restored writes and discard refresh/upload work from the old host."""
    connection.execute(
        'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
        (
            'restore_reconciliation',
            json.dumps(
                {'status': 'required', 'checkpoint_key': checkpoint_key, 'request_id': None, 'error_code': None}
            ),
        ),
    )
    connection.execute('DELETE FROM local_refresh WHERE id=?', ('active',))
    for actor, key, data in connection.execute('SELECT actor,key,data FROM local_operations').fetchall():
        operation = json.loads(data)
        if operation.get('phase') not in ('available', 'rejected'):
            operation.update(phase='rejected', message='The uncommitted upload was not restored; submit it again.')
            operation.pop('archive_path', None)
            connection.execute(
                'UPDATE local_operations SET data=? WHERE actor=? AND key=?',
                (json.dumps(operation), actor, key),
            )


def _checkpoint_manifest(config: Config, store: Store, checkpoint_key: str) -> dict:
    """Validate the closed checkpoint envelope and its exact database object key."""
    manifest = json.loads(store.get(checkpoint_key, config.limits.json_bytes))
    _require(
        isinstance(manifest, dict)
        and set(manifest) == {'version', 'database_version', 'created_at', 'database', 'dependencies', 'unavailable'}
    )
    _require(manifest['version'] == CHECKPOINT_VERSION and manifest['database_version'] == 3)
    _require(isinstance(manifest['dependencies'], list) and isinstance(manifest['unavailable'], list))
    _require(
        manifest['database']['key']
        == CHECKPOINT_DATA_PREFIX
        + checkpoint_key[len(CHECKPOINT_PREFIX) :].removesuffix('/manifest.json')
        + '/viewer.sqlite'
    )
    return manifest


def _validate_restored_versions(
    connection: sqlite3.Connection,
    bundles: BundleRepository,
    dependencies: dict[tuple[str, int], dict],
    missing: dict[tuple[str, int], str],
) -> None:
    """Match every retained generation to its verified dependency or explicit loss record."""
    records = _versions(connection)
    expected_dependencies = {
        (record.run_id, record.generation) for record in records if record.backup_manifest_key and record.backup_valid
    }
    expected_missing = {
        (record.run_id, record.generation)
        for record in records
        if not record.deleted and (record.run_id, record.generation) not in expected_dependencies
    }
    _require(set(dependencies) == expected_dependencies and set(missing) == expected_missing)
    for record in records:
        identity = (record.run_id, record.generation)
        if identity in dependencies:
            dependency = dependencies[identity]
            descriptor = bundles.read_descriptor(dependency['manifest_key'])
            _require(
                descriptor.identity.owner == record.owner
                and descriptor.identity.metadata_revision == record.backup_metadata_revision
                and descriptor.tombstone == record.deleted
            )
            _require(
                dependency['archive_sha256'] == record.archive_sha256
                and dependency['manifest_key'] == record.backup_manifest_key
                and record.backup_valid
            )
        elif not record.deleted:
            _require(missing.get(identity) == record.archive_sha256)


def _rewrite_restored_database(
    connection: sqlite3.Connection,
    dependencies: dict[tuple[str, int], dict],
    checkpoint_key: str,
) -> tuple[list[str], list[str]]:
    """Clear source-host paths and install durable quarantine, job and write barriers atomically."""
    with connection:
        for (raw,) in connection.execute('SELECT data FROM local_records').fetchall():
            record = _read_record(raw)
            ready = (record.run_id, record.generation) in dependencies or record.deleted
            record = replace(
                record,
                local_path=None,
                cache_bytes=0,
                cache_state='cold' if ready else 'unavailable',
                backup_valid=ready,
                backup_state=record.backup_state if ready else 'blocked',
            )
            connection.execute(
                'UPDATE local_records SET data=? WHERE run_id=? AND generation=? AND metadata_revision=?',
                (_record_wire(record), record.run_id, record.generation, record.metadata_revision),
            )
        connection.execute(
            (
                'UPDATE local_backup_jobs SET state=\'blocked\',active=0,worker=NULL,token=NULL,l'
                'ease_until=0,error=\'Restored checkpoint requires verified payload before retry'
                '\' WHERE state!=\'backed_up\''
            )
        )
        heads = [
            _read_record(row[0])
            for row in connection.execute(
                ('SELECT r.data FROM local_records r JOIN local_heads h USING(run_id,generation,' 'metadata_revision)')
            )
        ]
        lost = sorted(record.run_id for record in heads if not record.deleted and record.cache_state == 'unavailable')
        restored = sorted(
            record.run_id for record in heads if not record.deleted and record.cache_state != 'unavailable'
        )
        connection.execute(
            'INSERT OR REPLACE INTO local_settings VALUES (?,?)',
            (
                'restore_report',
                json.dumps({'checkpoint_key': checkpoint_key, 'lost_runs': lost, 'restored_runs': restored}),
            ),
        )
        _reset_restored_work(connection, checkpoint_key)
    return restored, lost


def restore(config: Config, store: Store, checkpoint_key: str, target_runtime: Path) -> RestoreResult:
    """Install a verified checkpoint into a new offline runtime, never a live DB."""
    target = _target(target_runtime)
    safe_path(checkpoint_key)
    _require(checkpoint_key.startswith(CHECKPOINT_PREFIX) and checkpoint_key.endswith('/manifest.json'))
    created_target = False
    completed = False
    try:
        manifest = _checkpoint_manifest(config, store, checkpoint_key)
        bundles = BundleRepository(config, store)
        deadline = time.monotonic() + config.limits.transfer_seconds
        dependencies = {}
        for dependency in manifest['dependencies']:
            _verify_dependency(bundles, dependency, deadline=deadline)
            identity = (dependency['run_id'], dependency['generation'])
            _require(identity not in dependencies)
            dependencies[identity] = dependency
        missing = {(row['run_id'], row['generation']): row['archive_sha256'] for row in manifest['unavailable']}
        _require(len(missing) == len(manifest['unavailable']) and not missing.keys() & dependencies.keys())
        with tempfile.TemporaryDirectory(prefix='.viewer-restore-', dir=target.parent) as directory:
            snapshot = Path(directory) / 'viewer.sqlite'
            _verify_object(store, manifest['database'], deadline=deadline, destination=snapshot)
            os.chmod(snapshot, 0o600)
            with closing(sqlite3.connect(snapshot)) as connection:
                _validate_database(connection)
                _validate_restored_versions(connection, bundles, dependencies, missing)
                restored, lost = _rewrite_restored_database(connection, dependencies, checkpoint_key)
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                connection.execute('PRAGMA journal_mode=DELETE')
            with snapshot.open('rb') as source:
                os.fsync(source.fileno())
            _target(target)
            target.mkdir(mode=0o700)
            created_target = True
            os.link(snapshot, target / 'viewer.sqlite')
            _fsync_directory(target)
            _fsync_directory(target.parent)
            result = RestoreResult(target, restored, lost)
            completed = True
            return result
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as error:
        raise ViewerError('invalid_checkpoint', 'Checkpoint is incomplete or incompatible.', 409) from error
    finally:
        if created_target and not completed:
            shutil.rmtree(target)
