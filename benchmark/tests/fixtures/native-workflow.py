#!/usr/bin/python3
"""Provider-free executable exercising native process, source and SQLite adapters."""

import json
import os
import sqlite3
import sys
import sysconfig
import time
from pathlib import Path


def emit(event: dict) -> None:
    """Write one native event using the public compact JSONL transport."""
    print(json.dumps(event, ensure_ascii=False, separators=(',', ':')), flush=True)


def usage() -> None:
    """Create an actual private native database including a field that must be sanitized."""
    database = Path(os.environ['XDG_DATA_HOME']) / 'opencode/opencode.db'
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute('pragma journal_mode=WAL')
        connection.execute('create table session(id text primary key,parent_id text,agent text)')
        connection.execute('create table message(id text primary key,session_id text,data text)')
        connection.execute('create table part(id text primary key,message_id text,session_id text,data text)')
        connection.execute('create table credential(secret text)')
        connection.execute('insert into credential values(?)', ('OFFLINE_PRIVATE_SENTINEL',))
        tokens = {'input': 10, 'output': 2, 'reasoning': 1, 'cache': {'read': 3, 'write': 0}, 'total': 16}
        message = {
            'role': 'assistant',
            'providerID': 'provider',
            'modelID': 'model',
            'finish': 'stop',
            'tokens': tokens,
            'cost': 0.5,
            'text': 'OFFLINE_PRIVATE_SENTINEL',
        }
        part = {
            'type': 'step-finish',
            'tokens': tokens,
            'cost': 0.5,
            'reason': 'stop',
            'irrelevant': 'OFFLINE_PRIVATE_SENTINEL',
        }
        connection.execute('insert into session values(?,?,?)', ('root', None, 'build'))
        connection.execute('insert into message values(?,?,?)', ('msg-root', 'root', json.dumps(message)))
        connection.execute('insert into part values(?,?,?,?)', ('part-root', 'msg-root', 'root', json.dumps(part)))


def changes() -> None:
    """Modify only the before snapshot, including empty files and a file-to-directory transition."""
    assert Path('src/value.py').read_text() == 'value = 1\n'
    assert not Path('src/human-only.py').exists()
    Path('src/value.py').write_text('value = 3\n')
    Path('src/deleted.py').unlink()
    Path('src/added.py').write_text('added = True\n')
    Path('src/agent-only.py').write_text('agent_only = True\n')
    Path('src/empty.py').chmod(0o755)
    Path('src/shape.py').unlink()
    Path('src/shape.py').mkdir()
    Path('src/shape.py/leaf.py').write_text('leaf = 3\n')
    Path('src/naïve file.py').write_text('greeting = "Привет"\n')


def mark_attempt() -> None:
    """Prove a real retried or resumed native attempt never inherits earlier packages."""
    assert sys.prefix == '/sandbox-home/.venv'
    marker = Path(sysconfig.get_path('purelib')) / 'previous_attempt_marker.py'
    assert not marker.exists(), 'A new native attempt inherited an earlier venv.'
    marker.write_text('ATTEMPT_MARKER = True\n')


def main() -> None:
    """Expose version/config and one bounded native run without any provider or network."""
    if '--version' in sys.argv:
        print('1.18.33')
    elif 'debug' in sys.argv:
        print(os.environ['OPENCODE_CONFIG_CONTENT'])
    else:
        assert 'run' in sys.argv and '--format' in sys.argv
        mark_attempt()
        assert sys.stdin.buffer.read()
        changes()
        usage()
        emit({'type': 'step_start', 'sessionID': 'root', 'part': {'type': 'step-start'}})
        if os.environ.get('OFFLINE_HOLD') == 'hold-native-process-for-crash-test':
            emit(
                {
                    'type': 'text',
                    'sessionID': 'root',
                    'part': {'text': 'Holding live native child for crash verification.'},
                }
            )
            time.sleep(60)
        emit({'type': 'text', 'sessionID': 'root', 'part': {'text': 'Offline fixture: Завершено'}})
        emit({'type': 'step_finish', 'sessionID': 'root', 'part': {'type': 'step-finish', 'reason': 'stop'}})
        print('Offline native fixture completed.', file=sys.stderr)


if __name__ == '__main__':
    main()
