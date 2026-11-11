"""Explicit offline import of legacy archive publications into local storage."""

import hashlib
import tempfile
import time
import uuid
from pathlib import Path

from viewer.catalogue import CatalogueBuilder
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_repository import LocalRepository
from viewer.local_service import LocalViewerService
from viewer.persistence.database import LOGIN, Database
from viewer.persistence.storage import Store
from viewer.util import digest, parse_json, require_object
from viewer.validation.archive import validate_archive
from viewer.validation.contracts import require

MIGRATION_ID = 'legacy-import'


def _settings(config: Config, store: Store) -> dict:
    """Validate and freeze legacy run metadata and basket presentation before import."""
    builder = CatalogueBuilder(config, store, {})
    metadata = builder._object('settings/viewer-metadata.json', {}, code='invalid_metadata')
    builder._validate_metadata(metadata)
    order = builder._json('settings/basket-order.json', [])
    require(
        isinstance(order, list) and all(isinstance(item, str) for item in order) and len(set(order)) == len(order),
        'invalid_basket_order',
    )
    display = builder._object(
        'settings/basket-display.json', {'version': 1, 'items': {}}, code='invalid_basket_display'
    )
    require(
        set(display) == {'version', 'items'} and type(display['version']) is int and display['version'] == 1,
        'invalid_basket_display',
    )
    builder._validate_display(display['items'])
    return {'metadata': metadata, 'order': order, 'display': display['items']}


def _plan(config: Config, store: Store, max_objects: int) -> list[dict]:
    """Capture a bounded, deterministic list of exact legacy markers with their digests."""
    require(type(max_objects) is int and max_objects > 0, 'invalid_migration_limit')
    result = []
    seen = set()
    for count, key in enumerate(store.list('runs/'), 1):
        require(count <= max_objects, 'migration_object_limit')
        if not key.endswith('/publication.json'):
            continue
        parts = key.split('/')
        require(len(parts) == 4 and parts[0] == 'runs' and LOGIN.fullmatch(parts[1]) is not None, 'invalid_publication')
        try:
            require(str(uuid.UUID(parts[2])) == parts[2], 'invalid_publication')
        except (ValueError, AttributeError):
            raise ViewerError('invalid_publication', 'Invalid legacy publication identity.') from None
        require(parts[2] not in seen, 'duplicate_run')
        seen.add(parts[2])
        raw = store.get(key, config.limits.json_bytes)
        marker = require_object(parse_json(raw))
        require(marker.get('owner') == parts[1] and marker.get('run_id') == parts[2], 'owner_conflict')
        result.append({'key': key, 'run_id': parts[2], 'owner': parts[1], 'marker_sha256': digest(raw)})
    return sorted(result, key=lambda item: item['key'])


def _download(config: Config, store: Store, key: str, marker: dict, destination: Path) -> None:
    """Stream one original archive while enforcing its declared size, digest and deadline."""
    size = marker.get('archive_size')
    sha256 = marker.get('archive_sha256')
    require(type(size) is int and 0 <= size <= config.limits.archive_bytes, 'invalid_publication')
    assert isinstance(size, int)
    require(
        isinstance(sha256, str) and len(sha256) == 64 and all(c in '0123456789abcdef' for c in sha256),
        'invalid_publication',
    )
    actual = hashlib.sha256()
    count = 0
    deadline = time.monotonic() + config.limits.transfer_seconds
    with store.open(key) as source, destination.open('xb') as output:
        while chunk := source.read(1 << 20):
            count += len(chunk)
            require(count <= size, 'stored_digest_mismatch')
            require(time.monotonic() <= deadline, 'transfer_timeout')
            actual.update(chunk)
            output.write(chunk)
    require(count == size and actual.hexdigest() == sha256, 'stored_digest_mismatch')


def import_legacy(config: Config, store: Store, db: Database, *, max_objects: int = 100_000) -> dict:
    """Resume a frozen legacy plan while preserving committed local changes.

    The caller owns service.lock for the entire operation. This method never
    starts workers or writes to remote storage.
    """
    if config.publication_storage != 'local':
        raise ViewerError('local_storage_mode_required', 'Enable local publication storage before importing.', 409)
    if db.operations():
        raise ViewerError('legacy_recovery_pending', 'Complete legacy recovery before importing publications.', 409)
    LocalRepository(db, begin_migration=True)
    service = LocalViewerService(config, store, db, initialize=False, allow_legacy_import=True)
    repository = service.repository
    progress = repository.load_refresh(MIGRATION_ID) or {
        'status': 'planning',
        'completed': {},
        'settings_applied': False,
    }
    try:
        if progress['status'] == 'completed':
            return progress
        progress.update(status='planning' if 'plan' not in progress else 'running', error_code=None)
        repository.save_refresh(MIGRATION_ID, progress)
        if 'plan' not in progress:
            settings = _settings(config, store)
            plan = _plan(config, store, max_objects)
            progress.update(plan=plan, settings=settings, status='running')
            repository.save_refresh(MIGRATION_ID, progress)
        if not progress['settings_applied']:
            current = repository.settings()
            order = current.get('order', [])
            order = order + [item for item in progress['settings']['order'] if item not in order]
            display = {**progress['settings']['display'], **current.get('display', {})}
            repository.save_settings(order=order, display=display)
            progress['settings_applied'] = True
            repository.save_refresh(MIGRATION_ID, progress)
        for item in progress['plan']:
            rid = item['run_id']
            if rid in progress['completed']:
                continue
            raw = store.get(item['key'], config.limits.json_bytes)
            require(digest(raw) == item['marker_sha256'], 'migration_source_changed')
            marker = require_object(parse_json(raw))
            existing = repository.get(rid, include_deleted=True)
            if existing is not None:
                require(
                    not existing.deleted
                    and existing.owner == marker['owner']
                    and existing.archive_sha256 == marker['archive_sha256']
                    and existing.entry['marker']['basket_sha256'] == marker['basket_sha256'],
                    'migration_conflict',
                )
                progress['completed'][rid] = {
                    'archive_sha256': existing.archive_sha256,
                    'marker_sha256': item['marker_sha256'],
                }
                repository.save_refresh(MIGRATION_ID, progress)
                continue
            owner = db.connection.execute('SELECT login FROM users WHERE login=?', (item['owner'],)).fetchone()
            require(owner is not None, 'owner_missing')
            with tempfile.TemporaryDirectory(prefix='legacy-import-', dir=config.runtime) as directory:
                root = Path(directory)
                archive = root / 'archive.tar.gz'
                _download(
                    config, store, item['key'].removesuffix('publication.json') + 'archive.tar.gz', marker, archive
                )
                imported = validate_archive(archive, root / 'data', config.limits)
                metadata = progress['settings']['metadata'].get(
                    rid,
                    {
                        'display_name': imported.report.agent.name,
                        'tags': imported.report.run.tags,
                        'pinned': False,
                        'created_at': imported.report.run.created_at,
                    },
                )
                record = service.import_legacy(imported, marker, metadata)
                progress['completed'][rid] = {
                    'archive_sha256': record.archive_sha256,
                    'marker_sha256': item['marker_sha256'],
                }
                repository.save_refresh(MIGRATION_ID, progress)
        progress.update(status='completed', error_code=None)
        repository.save_refresh(MIGRATION_ID, progress)
        return progress
    except Exception as error:
        progress.update(status='failed', error_code=error.code if isinstance(error, ViewerError) else 'local_io')
        repository.save_refresh(MIGRATION_ID, progress)
        raise
    finally:
        service.close()
