"""Consistent private SQLite snapshots and immutable sanitized usage artifacts."""

import json
import logging
import lzma
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.errors import BenchmarkError
from benchmark.execution.usage import (
    billing_tokens,
    native_tokens,
    telemetry_from_usage,
    validate_usage,
)
from benchmark.json_types import JsonObject
from benchmark.models import Usage
from benchmark.models.base import json_value
from benchmark.models.report import (
    ReportTelemetry,
    ReportTelemetryCost,
    ReportTelemetryDiagnosticItem,
    ReportTelemetrySessionDb,
    ReportTelemetryTokens,
    ReportTelemetryUsageLedger,
)
from benchmark.models.telemetry import (
    TelemetryMetadata,
    TelemetryOwner,
    TelemetryReceipt,
)
from benchmark.models.usage import (
    UsageRequestItem,
    UsageRequestItemBillingTokens,
    UsageSessionItem,
)
from benchmark.persistence.storage import (
    atomic_write,
    bytes_sha256,
    document_bytes,
    read_json,
    sha256,
    write_json,
)

ADAPTER_VERSION = 'opencode-1.18.33-sql-v1'


@dataclass(frozen=True)
class TelemetryResult:
    """Pair captured usage with its independent archive completeness."""

    data: ReportTelemetry
    archive_complete: bool


def unavailable_telemetry(code: str = 'not_started', message: str = 'OpenCode has not started.') -> ReportTelemetry:
    """Unknown counters are distinct from a measured empty ledger."""
    return ReportTelemetry(
        source='opencode',
        status='unavailable',
        tokens=ReportTelemetryTokens(
            input=None, output=None, reasoning=None, cache_read=None, cache_write=None, total=None
        ),
        total_source='unavailable',
        cost=ReportTelemetryCost(amount=None, currency='USD', source='opencode'),
        transport='none',
        session_db=None,
        usage_ledger=None,
        session_counts=None,
        diagnostics=[ReportTelemetryDiagnosticItem(code=code, message=message)],
        api_equivalent=None,
    )


def _snapshot(source: Path, target: Path) -> None:
    """Create a consistent SQLite backup including committed WAL data."""
    if source.is_symlink() or not source.is_file():
        raise BenchmarkError('telemetry_missing', 'The private OpenCode database is missing.', exit_code=1)
    with (
        closing(sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True, timeout=10)) as input_db,
        closing(sqlite3.connect(target)) as output,
        output,
    ):
        input_db.backup(output)
        if output.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise BenchmarkError('telemetry_corrupt', 'OpenCode database integrity check failed.', exit_code=1)


def _session_inventory(db: sqlite3.Connection, root_id: str | None) -> tuple[list[UsageSessionItem], str]:
    """Validate native tables and preserve explicit launch-session ownership."""
    required = {
        'session': {'id', 'parent_id', 'agent'},
        'message': {'id', 'session_id', 'data'},
        'part': {'id', 'message_id', 'session_id', 'data'},
    }
    for table, columns in required.items():
        actual = {row[0] for row in db.execute('select name from pragma_table_info(?)', (table,))}
        if not columns <= actual:
            raise BenchmarkError(
                'telemetry_schema_unsupported', 'OpenCode database schema is unsupported.', exit_code=1
            )
    schema = {
        name: db.execute('select sql from sqlite_master where name=?', (name,)).fetchone()[0] for name in required
    }
    raw_sessions = db.execute('select id,parent_id,agent from session order by id').fetchall()
    ids = {row[0] for row in raw_sessions}
    if root_id is not None and root_id not in ids:
        raise BenchmarkError(
            'telemetry_root_missing', 'The launch session is absent from the private database.', exit_code=1
        )
    sessions = []
    for sid, parent, agent in raw_sessions:
        if sid == root_id and parent is not None:
            raise BenchmarkError('telemetry_root_invalid', 'Launch session is not a root session.', exit_code=1)
        if agent is not None and not isinstance(agent, str):
            agent = None
        sessions.append(
            UsageSessionItem(
                id=sid,
                parent_id=parent,
                agent_name=agent,
                kind='root' if sid == root_id else ('subagent' if parent is not None else 'detached'),
            )
        )
    return sessions, sha256(schema)


def _native_ledger(path: Path, root_id: str | None) -> tuple[list[UsageSessionItem], list[UsageRequestItem], bool, str]:
    """Extract only owned session and billing fields from the native schema."""
    with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)) as db:
        sessions, schema_hash = _session_inventory(db, root_id)
        assistants: dict[str, tuple[str, JsonObject]] = {}
        for mid, sid, data in db.execute('select id,session_id,data from message order by id'):
            value = json_value(json.loads(data))
            if not isinstance(value, dict):
                raise ValueError('Native SQLite JSON must be an object.')
            if value.get('role') == 'assistant':
                assistants[mid] = (sid, value)
        requests: list[UsageRequestItem] = []
        covered = set()
        for pid, mid, sid, data in db.execute('select id,message_id,session_id,data from part order by id'):
            value = json_value(json.loads(data))
            if not isinstance(value, dict):
                raise ValueError('Native SQLite JSON must be an object.')
            if value.get('type') != 'step-finish':
                continue
            if mid not in assistants or sid != assistants[mid][0]:
                raise BenchmarkError(
                    'telemetry_foreign_request', 'OpenCode billing unit has a foreign message/session.', exit_code=1
                )
            info = assistants[mid][1]
            raw = native_tokens(value.get('tokens'))
            billable, context = billing_tokens(raw)
            cost = value.get('cost')
            if cost is not None and (
                not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0
            ):
                raise BenchmarkError('invalid_usage', 'OpenCode reported an invalid cost.', exit_code=1)
            provider, model = info['providerID'], info['modelID']
            if not isinstance(provider, str) or not isinstance(model, str):
                raise ValueError('Native provider and model must be strings.')
            assert cost is None or isinstance(cost, (int, float))
            requests.append(
                UsageRequestItem(
                    id=pid,
                    session_id=sid,
                    provider=provider,
                    model=model,
                    complete=value.get('reason') is not None,
                    usage=raw,
                    billing_tokens=billable,
                    input_context_tokens=context,
                    reported_cost_usd=cost,
                )
            )
            covered.add(mid)
        complete = True
        for mid, (sid, info) in assistants.items():
            if mid in covered:
                continue
            # An unfinished call has no billable finish record. Interim default zeros are not final usage.
            provider, model = info['providerID'], info['modelID']
            if not isinstance(provider, str) or not isinstance(model, str):
                raise ValueError('Native provider and model must be strings.')
            requests.append(
                UsageRequestItem(
                    id='unfinished:' + mid,
                    session_id=sid,
                    provider=provider,
                    model=model,
                    complete=False,
                    usage=ReportTelemetryTokens(
                        input=None, output=None, reasoning=None, cache_read=None, cache_write=None, total=None
                    ),
                    billing_tokens=UsageRequestItemBillingTokens(
                        input=None, output=None, cache_read=None, cache_write=None
                    ),
                    input_context_tokens=None,
                    reported_cost_usd=None,
                )
            )
            complete = False
        requests.sort(key=lambda row: row.id)
        return sessions, requests, complete and all(row.complete for row in requests), schema_hash


def _projection(
    path: Path, metadata: TelemetryMetadata, sessions: list[UsageSessionItem], requests: list[UsageRequestItem]
) -> bytes:
    """Export only allowlisted session and usage fields in SQL tables."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('create table metadata(key text primary key,value text not null)')
        db.execute('create table session(id text primary key,parent_id text,agent_name text,kind text not null)')
        db.execute(
            'create table request(id text primary key,session_id text,provider text,model text,complete integer,'
            'usage text,billing_tokens text,input_context_tokens integer,reported_cost_usd real)'
        )
        db.executemany(
            'insert into metadata values(?,?)', [(key, json.dumps(value)) for key, value in metadata.to_wire().items()]
        )
        db.executemany(
            'insert into session values(?,?,?,?)',
            [(row.id, row.parent_id, row.agent_name, row.kind) for row in sessions],
        )
        db.executemany(
            'insert into request values(?,?,?,?,?,?,?,?,?)',
            [
                (
                    row.id,
                    row.session_id,
                    row.provider,
                    row.model,
                    int(row.complete),
                    json.dumps(row.usage.to_wire()),
                    json.dumps(row.billing_tokens.to_wire()),
                    row.input_context_tokens,
                    row.reported_cost_usd,
                )
                for row in requests
            ],
        )
        db.commit()
        if db.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise BenchmarkError(
                'telemetry_corrupt', 'Sanitized usage database failed integrity validation.', exit_code=1
            )
    return lzma.compress(path.read_bytes())


def read_saved_telemetry(directory: Path, *, run_id: str, task_id: str, attempt_id: str) -> TelemetryResult:
    """Validate immutable final artifacts before offline reuse."""
    receipt = TelemetryReceipt.model_validate(read_json(directory / 'receipt.json'))
    if (receipt.run_id, receipt.task_id, receipt.attempt_id) != (run_id, task_id, attempt_id):
        raise BenchmarkError('telemetry_owner_mismatch', 'Telemetry belongs to another attempt.', exit_code=1)
    if receipt.status != 'saved':
        assert receipt.telemetry is not None
        return TelemetryResult(receipt.telemetry, receipt.archive_complete)
    ledger = validate_usage(read_json(directory / 'usage.json'))
    if (ledger.run_id, ledger.task_id, ledger.attempt_id) != (run_id, task_id, attempt_id):
        raise BenchmarkError('telemetry_owner_mismatch', 'Usage ledger belongs to another attempt.', exit_code=1)
    for name, expected in [('usage.json', receipt.usage_sha256), ('opencode.sqlite.xz', receipt.db_sha256)]:
        if (directory / name).is_symlink() or bytes_sha256((directory / name).read_bytes()) != expected:
            raise BenchmarkError(
                'telemetry_digest_mismatch', 'Saved telemetry digest differs; restore the original run.', exit_code=1
            )
    data = telemetry_from_usage(ledger)
    assert receipt.usage_path is not None and receipt.usage_sha256 is not None
    data.usage_ledger = ReportTelemetryUsageLedger(path=receipt.usage_path, sha256=receipt.usage_sha256)
    if not ledger.complete:
        data.diagnostics.append(
            ReportTelemetryDiagnosticItem(
                code='telemetry_partial', message='Some OpenCode billing units did not finish.'
            )
        )
    return TelemetryResult(data, True)


def _save_telemetry(
    directory: Path,
    ledger: Usage,
    dump: bytes,
    metadata: TelemetryMetadata,
    path_prefix: str,
    secrets: list[str] | None,
) -> None:
    """Publish the sanitized database and ledger before its closing receipt."""
    validate_usage(ledger)
    encoded = document_bytes(ledger)
    if any(value and value.encode() in encoded for value in secrets or []):
        raise BenchmarkError(
            'telemetry_secret',
            'A secret appeared in allowlisted metadata; telemetry was withheld.',
            exit_code=1,
        )
    if (directory / 'usage.json').exists() or (directory / 'opencode.sqlite.xz').exists():
        raise BenchmarkError(
            'telemetry_immutable',
            'Incomplete telemetry files already exist; do not overwrite them.',
            exit_code=1,
        )
    atomic_write(directory / 'opencode.sqlite.xz', dump)
    atomic_write(directory / 'usage.json', encoded)
    receipt = TelemetryReceipt(
        run_id=metadata.run_id,
        task_id=metadata.task_id,
        attempt_id=metadata.attempt_id,
        status='saved',
        archive_complete=True,
        format='opencode_usage_v1',
        runtime=metadata.runtime,
        adapter_version=ADAPTER_VERSION,
        source_schema_sha256=metadata.source_schema_sha256,
        db_sha256=bytes_sha256(dump),
        db_size_bytes=len(dump),
        usage_sha256=bytes_sha256(encoded),
        usage_path=path_prefix + '/usage.json',
    )
    write_json(directory / 'receipt.json', receipt)


def capture_telemetry(
    source: Path,
    directory: Path,
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    runtime: str,
    root_id: str | None,
    opencode_started: bool,
    path_prefix: str = 'telemetry',
    secrets: list[str] | None = None,
) -> TelemetryResult:
    """Freeze attempt-owned native usage without retaining credential or tool tables.

    Returns:
        Sanitized usage and independent archive completeness. Unknown counters stay
        unknown; collection failure warns without rerunning the agent.

    """
    if (directory / 'receipt.json').exists():
        return read_saved_telemetry(directory, run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    owner = TelemetryOwner(run_id=run_id, task_id=task_id, attempt_id=attempt_id)
    if not opencode_started and not source.exists():
        data = unavailable_telemetry()
        write_json(
            directory / 'receipt.json',
            TelemetryReceipt(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                status='not_started',
                archive_complete=True,
                telemetry=data,
            ),
        )
        return TelemetryResult(data, True)
    try:
        with TemporaryDirectory(prefix='benchmark-private-usage-') as temporary:
            scratch = Path(temporary)
            _snapshot(source, scratch / 'raw.sqlite')
            sessions, requests, complete, schema_hash = _native_ledger(scratch / 'raw.sqlite', root_id)
            metadata = TelemetryMetadata(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                format='opencode_usage_v1',
                runtime=runtime,
                adapter_version=ADAPTER_VERSION,
                source_schema_sha256=schema_hash,
                root_session_id=root_id,
                complete=complete,
            )
            dump = _projection(scratch / 'usage.sqlite', metadata, sessions, requests)
            ledger = Usage(
                schema_family='pr_similarity_os.usage',
                schema_version=1,
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                runtime=runtime,
                adapter_version=ADAPTER_VERSION,
                source_db=ReportTelemetrySessionDb(path=path_prefix + '/opencode.sqlite.xz', sha256=bytes_sha256(dump)),
                complete=complete,
                sessions=sessions,
                requests=requests,
            )
            _save_telemetry(directory, ledger, dump, metadata, path_prefix, secrets)
            return read_saved_telemetry(
                directory, run_id=owner.run_id, task_id=owner.task_id, attempt_id=owner.attempt_id
            )
    except (BenchmarkError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        logging.getLogger(__name__).debug('Telemetry snapshot failed', exc_info=exc)
        data = unavailable_telemetry('telemetry_unavailable', 'OpenCode usage capture failed; see the DEBUG file log.')
        write_json(
            directory / 'receipt.json',
            TelemetryReceipt(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                status='unavailable',
                archive_complete=False,
                telemetry=data,
            ),
        )
        return TelemetryResult(data, False)
