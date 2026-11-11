"""Bounded administrator projections of active transfers, backups and audits."""

import json
from datetime import datetime
from sqlite3 import Row
from typing import TYPE_CHECKING

from viewer.activity import ActivityTracker
from viewer.local_repository import decode_entry
from viewer.local_service import LocalViewerService

if TYPE_CHECKING:
    from viewer.service import ViewerService


def _elapsed(started: object, now: float) -> float | None:
    """Return elapsed wall time only for a parseable timezone-aware start."""
    if not isinstance(started, str):
        return None
    try:
        value = datetime.fromisoformat(started.replace('Z', '+00:00'))
        return max(0.0, now - value.timestamp()) if value.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def _row(
    service: 'ViewerService', kind: str, identifier: str | None, rid: str | None, status: str, **values: object
) -> dict:
    """Build safe display fields from the current catalogue and explicit overrides."""
    entry = service.runs.get(rid) if rid else None
    metadata = service.metadata.get(rid, {}) if rid else {}
    return (
        dict(
            id=identifier,
            kind=kind,
            run_id=rid,
            status=status,
            run_available=entry is not None,
            name=metadata.get('display_name') or ('New publication' if rid else 'All published runs'),
            basket=entry['basket']['meta']['name'] if entry else None,
            owner=entry['marker']['owner'] if entry else None,
            phase=status,
            elapsed_seconds=None,
            received_bytes=None,
            total_bytes=None,
        )
        | values
    )


def _pending_uploads(service: LocalViewerService, observed: dict, limit: int) -> tuple[list[dict], int]:
    """Read interrupted uploads not represented by this process's observations."""
    items = []
    with service.db.lock:
        predicate = (
            '''json_extract(data,'$.phase') IN ('receiving','extracting','validating','committing') '''
            '''AND json_extract(data,'$.operation_id') NOT IN (SELECT value FROM json_each(?))'''
        )
        excluded = json.dumps(list(observed))
        total = service.db.connection.execute(
            'SELECT count(*) FROM local_operations WHERE ' + predicate, (excluded,)
        ).fetchone()[0]
        pending = service.db.connection.execute(
            'SELECT data FROM local_operations WHERE ' + predicate + ' ORDER BY actor,key LIMIT ?',
            (excluded, limit),
        ).fetchall()
        for (raw,) in pending:
            operation = json.loads(raw)
            items.append(
                _row(
                    service,
                    'upload',
                    operation['operation_id'],
                    operation['run_id'],
                    'waiting_recovery',
                    phase=operation['phase'],
                    actor=operation.get('actor_login'),
                )
            )
    return items, total


def _backup_rows(service: LocalViewerService, limit: int) -> tuple[list[dict], int]:
    """Project at most the requested number of outstanding exact-version backups."""
    with service.db.lock:
        count = service.db.connection.execute(
            '''SELECT count(*) FROM local_backup_jobs WHERE state IN ('pending','uploading','retry_wait','blocked')'''
        ).fetchone()[0]
        jobs = service.db.connection.execute(
            '''SELECT id,run_id,generation,metadata_revision,kind,state,attempts,retry_at,error '''
            '''FROM local_backup_jobs WHERE state IN ('pending','uploading','retry_wait','blocked') '''
            '''ORDER BY CASE state WHEN 'uploading' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,id LIMIT ?''',
            (limit,),
        ).fetchall()
    active = service.backups.activity.snapshot()
    return [_backup_row(service, job, active.get(str(job['id']), {})) for job in jobs], count


def _backup_row(service: LocalViewerService, job: Row, running: dict) -> dict:
    """Use the job's durable identity, including tombstones absent from the catalogue."""
    error = job['error']
    if not isinstance(error, str) or len(error) > 80 or not error.replace('_', '').isalnum():
        error = 'backup_failed' if error else None
    with service.db.lock:
        stored = service.db.connection.execute(
            'SELECT data FROM local_records WHERE run_id=? AND generation=? AND metadata_revision=?',
            (job['run_id'], job['generation'], job['metadata_revision']),
        ).fetchone()
    record = json.loads(stored[0]) if stored else {}
    entry = decode_entry(record['entry']) if record else None
    identity = {}
    if entry is not None:
        identity = dict(
            name=record['metadata'].get('display_name') or 'Published run',
            owner=record['owner'],
            basket=entry['basket']['meta']['name'] if 'basket' in entry else None,
        )
    archive_size = entry['marker']['archive_size'] if entry and job['kind'] != 'tombstone' else None
    return _row(
        service,
        'backup',
        str(job['id']),
        job['run_id'],
        job['state'],
        generation=job['generation'],
        metadata_revision=job['metadata_revision'],
        backup_kind=job['kind'],
        attempts=job['attempts'],
        error_code=error,
        retry_at=job['retry_at'] if job['state'] == 'retry_wait' else None,
        elapsed_seconds=running.get('elapsed_seconds'),
        archive_bytes=archive_size,
        **identity
    )


def _refresh_row(service: 'ViewerService', refresh: dict, limit: int) -> dict:
    """Keep safe active-run diagnostics and omit terminal running duration."""
    return _row(
        service,
        'refresh',
        refresh.get('request_id'),
        None,
        refresh['status'],
        phase=refresh.get('phase'),
        checked=refresh.get('checked'),
        total=refresh.get('total'),
        errors=refresh.get('errors'),
        deferred=refresh.get('deferred'),
        bytes=refresh.get('bytes'),
        error_code=refresh.get('error_code'),
        active=[
            {
                key: value
                for key, value in entry.items()
                if key in ('run_id', 'generation', 'object', 'stage', 'phase', 'bytes', 'last_progress')
            }
            for entry in refresh.get('active', [])[:limit]
        ],
        elapsed_seconds=(
            _elapsed(refresh.get('started_at'), service.db.clock()) if refresh['status'] == 'running' else None
        ),
    )


def activity_snapshot(service: 'ViewerService', uploads: ActivityTracker, stats: dict | None = None) -> dict:
    """Read bounded allowlisted rows under the caller's service lock without S3 scans."""
    limit = service.config.admin_activity_limit
    observed = uploads.snapshot()
    items = [
        _row(
            service,
            'upload',
            key,
            data['run_id'],
            data['phase'],
            actor=data['actor'],
            received_bytes=data['received_bytes'],
            total_bytes=data['total_bytes'],
            elapsed_seconds=data['elapsed_seconds'],
        )
        for key, data in observed.items()
    ]
    total = len(observed)
    if isinstance(service, LocalViewerService):
        pending, pending_count = _pending_uploads(service, observed, max(0, limit - len(items)))
        items.extend(pending)
        backups, backup_count = _backup_rows(service, max(0, limit - len(items)))
        items.extend(backups)
        total += pending_count + backup_count
    refresh = (stats if stats is not None else service.cache_stats()).get('refresh_job')
    if refresh and refresh.get('status') not in ('idle', 'succeeded'):
        total += 1
        item = _refresh_row(service, refresh, limit)
        if len(items) >= limit:
            items[-1] = item
        else:
            items.append(item)
    return {'items': items[:limit], 'total': total, 'limit': limit}
