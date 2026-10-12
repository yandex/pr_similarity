"""Actual SQLite/WAL and nested session billing-unit accounting."""

import json
import lzma
import sqlite3
from pathlib import Path

from benchmark.storage import read_json
from benchmark.telemetry import TelemetryResult, capture_telemetry


def native_db(path: Path) -> sqlite3.Connection:
    """Create a WAL-backed native database with nested sessions and private fields."""
    db = sqlite3.connect(path)
    db.execute('pragma journal_mode=WAL')
    db.execute('create table session(id text primary key,parent_id text,agent text)')
    db.execute('create table message(id text primary key,session_id text,data text)')
    db.execute('create table part(id text primary key,message_id text,session_id text,data text)')
    db.execute('create table credential(secret text)')
    db.execute('insert into credential values(?)', ('NEVER_ARCHIVE_SECRET',))
    for sid, parent in [('root', None), ('child', 'root'), ('grandchild', 'child'), ('detached', None)]:
        db.execute('insert into session values(?,?,?)', (sid, parent, 'build'))
        tokens = {'input': 10, 'output': 2, 'reasoning': 1, 'cache': {'read': 3, 'write': 0}, 'total': 16}
        message = {
            'role': 'assistant',
            'providerID': 'provider',
            'modelID': 'model',
            'finish': 'stop',
            'tokens': tokens,
            'cost': 0.5,
            'text': 'NEVER_ARCHIVE_SECRET',
        }
        part = {
            'type': 'step-finish',
            'tokens': tokens,
            'cost': 0.5,
            'reason': 'stop',
            'irrelevant': 'NEVER_ARCHIVE_SECRET',
        }
        db.execute('insert into message values(?,?,?)', ('msg-' + sid, sid, json.dumps(message)))
        db.execute('insert into part values(?,?,?,?)', ('part-' + sid, 'msg-' + sid, sid, json.dumps(part)))
    db.commit()
    return db


def capture(directory: Path, source: Path) -> TelemetryResult:
    """Capture the fixture database using fixed attempt ownership identities."""
    return capture_telemetry(
        source,
        directory,
        run_id='11111111-1111-4111-8111-111111111111',
        task_id='owner/repo#1',
        attempt_id='22222222-2222-4222-8222-222222222222',
        runtime='opencode:v1.18.33',
        root_id='root',
        opencode_started=True,
    )


def test_wal_snapshot_counts_all_owned_sessions_once(tmp_path: Path) -> None:
    """Verify wal snapshot counts all owned sessions once."""
    source = tmp_path / 'native.db'
    connection = native_db(source)
    try:
        result = capture(tmp_path / 'telemetry', source)
    finally:
        connection.close()
    assert result.archive_complete
    assert result.data['tokens'] == {
        'input': 40,
        'output': 8,
        'reasoning': 4,
        'cache_read': 12,
        'cache_write': 0,
        'total': 64,
    }
    assert result.data['cost']['amount'] == 2
    assert result.data['session_counts'] == {'root': 1, 'subagent': 2, 'detached': 1}
    ledger = read_json(tmp_path / 'telemetry/usage.json')
    encoded = (tmp_path / 'telemetry/usage.json').read_bytes()
    assert encoded == (json.dumps(ledger, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()
    assert len(ledger['requests']) == 4
    assert ledger['requests'][0]['billing_tokens']['output'] == 3
    dump = lzma.decompress((tmp_path / 'telemetry/opencode.sqlite.xz').read_bytes())
    assert b'NEVER_ARCHIVE_SECRET' not in dump
    assert b'NEVER_ARCHIVE_SECRET' not in (tmp_path / 'telemetry/usage.json').read_bytes()


def test_unknown_cache_stays_unknown(tmp_path: Path) -> None:
    """Verify unknown cache stays unknown."""
    source = tmp_path / 'native.db'
    connection = native_db(source)
    row = json.loads(connection.execute('select data from part limit 1').fetchone()[0])
    row['tokens']['cache'].pop('write')
    connection.execute('update part set data=? where id=?', (json.dumps(row), 'part-root'))
    connection.commit()
    result = capture(tmp_path / 'telemetry', source)
    connection.close()
    assert result.data['tokens']['cache_write'] is None
    assert result.data['tokens']['cache_read'] == 12
    ledger = read_json(tmp_path / 'telemetry/usage.json')
    request = next(item for item in ledger['requests'] if item['session_id'] == 'root')
    assert request['billing_tokens']['cache_write'] is None


def test_dump_failure_is_diagnostic_not_a_fake_zero(tmp_path: Path) -> None:
    """Verify dump failure is diagnostic not a fake zero."""
    result = capture(tmp_path / 'telemetry', tmp_path / 'absent.db')
    assert not result.archive_complete
    assert result.data['status'] == 'unavailable'
    assert result.data['tokens']['input'] is None
    assert result.data['diagnostics'][0]['code'] == 'telemetry_unavailable'


def test_not_started_requires_no_fake_dump(tmp_path: Path) -> None:
    """Verify not started requires no fake dump."""
    result = capture_telemetry(
        tmp_path / 'missing.db',
        tmp_path / 'telemetry',
        run_id='11111111-1111-4111-8111-111111111111',
        task_id='o/r#1',
        attempt_id='22222222-2222-4222-8222-222222222222',
        runtime='opencode:v1.18.33',
        root_id=None,
        opencode_started=False,
    )
    assert result.archive_complete
    assert not (tmp_path / 'telemetry/opencode.sqlite.xz').exists()


def test_saved_telemetry_is_immutable(tmp_path: Path) -> None:
    """Verify saved telemetry is immutable."""
    source = tmp_path / 'native.db'
    connection = native_db(source)
    one = capture(tmp_path / 'telemetry', source)
    content = (tmp_path / 'telemetry/usage.json').read_bytes()
    connection.execute('delete from part')
    connection.commit()
    two = capture(tmp_path / 'telemetry', source)
    connection.close()
    assert one.data == two.data
    assert (tmp_path / 'telemetry/usage.json').read_bytes() == content
