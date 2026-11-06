"""Local daemon lifecycle and safe administrative CLI transport on mock storage."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

import boto3
import pytest
from botocore.stub import Stubber
from viewer import __version__
from viewer.cli import ProcessRecord, administer, lifecycle
from viewer.config import Config
from viewer.util import json_bytes


@pytest.mark.parametrize('shutdown_seconds', [15, 60])
def test_stop_waits_for_both_server_shutdown_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown_seconds: int
) -> None:
    """The stop client allows HTTP draining followed by maintenance shutdown."""
    config = Config(storage='mock', runtime=tmp_path, shutdown_seconds=shutdown_seconds)
    elapsed = [0.0]
    exit_at = 2 * shutdown_seconds - 1

    def advance(seconds: float) -> None:
        """Advance simulated time without waiting or signalling a real process."""
        elapsed[0] += seconds

    monkeypatch.setattr('viewer.cli._running', lambda _: ProcessRecord(123, 'ticks'))
    monkeypatch.setattr('viewer.cli._identity', lambda _: 'ticks' if elapsed[0] < exit_at else None)
    monkeypatch.setattr('viewer.cli.os.kill', lambda pid, sig: None)
    monkeypatch.setattr('viewer.cli.time.monotonic', lambda: elapsed[0])
    monkeypatch.setattr('viewer.cli.time.sleep', advance)
    assert lifecycle('stop', config, tmp_path / 'config.json') == {'running': False}
    assert elapsed[0] >= exit_at


@pytest.mark.parametrize('dev', ['0', '1'])
def test_make_daemon_lifecycle_on_current_host(tmp_path: Path, dev: str) -> None:
    """Verify make daemon lifecycle on current host."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    cfg = {
        'runtime': str(tmp_path / 'runtime'),
        'admin_token_file': str(tmp_path / 'runtime/admin-token'),
        'storage': 'mock',
        'port': port,
    }
    path = tmp_path / 'config.json'
    path.write_bytes(json_bytes(cfg))
    config = Config.load(path)
    assert lifecycle('status', config, path) == {'running': False, 'pid': None}
    try:
        result = subprocess.run(
            ['make', '--no-print-directory', 'start', f'CONFIG={path}', f'DEV={dev}', f'VENV_PYTHON={sys.executable}'],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        started = json.loads(result.stdout)
        with urlopen(f'http://127.0.0.1:{port}/') as response:
            document = response.read().decode()
        label = 'v' + __version__ + (' [dev]' if dev == '1' else '')
        assert f'id="build-info">{label}</span>' in document
        assert started is not None
        assert started['running']
        repeated = lifecycle('start', config, path)
        assert repeated is not None
        assert repeated['pid'] == started['pid']
        status = lifecycle('status', config, path)
        assert status is not None
        assert status['running']
        assert config.admin_token_file.stat().st_mode & 0o777 == 0o400
    finally:
        lifecycle('stop', config, path)
    stopped = lifecycle('status', config, path)
    assert stopped is not None
    assert stopped['running'] is False
    assert 'Application startup complete' in (config.runtime / 'viewer.log').read_text()


def test_private_cli_password_not_in_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify private cli password not in argv."""
    config = Config(storage='mock', runtime=tmp_path / 'runtime')
    monkeypatch.setenv('PRSIM_VIEWER_LOGIN', 'student')
    monkeypatch.setattr('getpass.getpass', lambda prompt: 'hidden')
    seen = []
    monkeypatch.setattr('viewer.cli._request', lambda *a: seen.append(a) or {'ok': True})
    assert administer('admin-user-create', config) == {'ok': True}
    assert seen[0][3] == {'login': 'student', 'password': 'hidden'}
    assert seen[0][4] is True


@pytest.mark.parametrize('target', ['admin-user-enable', 'serve'])
def test_make_variables_cannot_execute_shell_text(tmp_path: Path, target: str) -> None:
    """Verify make variables cannot execute shell text."""
    spy = tmp_path / 'spy.py'
    out = tmp_path / 'args.json'
    marker = tmp_path / 'should-not-exist'
    spy.write_text('#!' + sys.executable + ('''
"""Record Make arguments without executing their shell-looking contents."""

import json
import os
import sys
from pathlib import Path

Path(os.environ['SPY_OUTPUT']).write_text(json.dumps({'argv': sys.argv[1:], 'login': os.getenv('PRSIM_VIEWER_LOGIN')}))
'''))
    spy.chmod(0o755)
    # A shell-looking string stays an environment value, never Make/shell code.
    env = dict(os.environ)
    env['SPY_OUTPUT'] = str(out)
    literal = '$(shell touch ' + str(marker) + ')'
    subprocess.run(
        ['make', target, 'VENV_PYTHON=' + str(spy), 'LOGIN=' + literal],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
    )
    value = json.loads(out.read_text())
    assert value['argv'] == ['-m', 'viewer', target]
    assert value['login'] == literal
    assert not marker.exists()


def test_make_help_in_empty_checkout_needs_no_python_config_or_credentials(tmp_path: Path) -> None:
    """Verify make help in empty checkout needs no python config or credentials."""
    (tmp_path / 'Makefile').write_bytes((Path(__file__).parents[1] / 'Makefile').read_bytes())
    result = subprocess.run(
        ['make', 'help', 'PYTHON=/missing/python', 'VENV_PYTHON=/missing/venv', 'CONFIG=/missing/config'],
        cwd=tmp_path,
        env={'PATH': '/usr/bin:/bin'},
        check=True,
        capture_output=True,
        text=True,
    )
    assert 'make start' in result.stdout and 'make admin-profile-create' in result.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == ['Makefile']


def test_trash_policy_is_an_operator_artifact_not_a_runtime_purge() -> None:
    """Verify trash policy is an operator artifact not a runtime purge."""
    path = Path(__file__).parents[1] / 'ops/trash-lifecycle.example.json'
    policy = json.loads(path.read_bytes())
    assert policy['Rules'][0]['Expiration']['Days'] == 90
    assert all(rule['Filter']['Prefix'] == 'trash/' for rule in policy['Rules'])
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='mock', aws_secret_access_key='mock')
    with Stubber(client) as stub:
        params = {'Bucket': 'operator-example', 'LifecycleConfiguration': policy}
        stub.add_response('put_bucket_lifecycle_configuration', {}, params)
        client.put_bucket_lifecycle_configuration(**params)


@pytest.mark.parametrize('command', ['start', 'serve'])
def test_invalid_dev_flag_rejects_startup(tmp_path: Path, command: str) -> None:
    """Reject an invalid deployment label flag before creating runtime state."""
    result = subprocess.run(
        ['make', command, 'DEV=yes', f'CONFIG={tmp_path / 'missing.json'}', f'VENV_PYTHON={sys.executable}'],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert 'DEV must be 0 or 1.' in result.stderr
