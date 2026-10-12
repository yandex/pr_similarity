"""Real bubblewrap namespace isolation, persistence and shared-network checks."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from benchmark.errors import BenchmarkError
from benchmark.sandbox import Sandbox


def test_namespace_hides_sibling_reference_and_host_home(tmp_path: Path) -> None:
    """Verify namespace hides sibling reference and host home."""
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file.py').write_text('before')
    reference = tmp_path / 'reference'
    reference.write_text('must stay hidden')
    home = tmp_path / 'private'
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, home, runtime, Path('/usr/bin/true'))
    code = (
        'import os,json; print(json.dumps({"ref":os.path.exists(' + repr(str(reference)) + '),'
        '"host_home":os.path.exists(' + repr(str(Path.home())) + '),"source":open("file.py").read()}))'
    )
    result = subprocess.run(
        sandbox.command(['/usr/bin/python3', '-c', code]),
        env=sandbox.environment({}),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data == {'ref': False, 'host_home': False, 'source': 'before'}


def test_setup_home_persists_and_source_writes_are_visible(tmp_path: Path) -> None:
    """Verify setup home persists and source writes are visible."""
    source = tmp_path / 'source'
    source.mkdir()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'private', runtime, Path('/usr/bin/true'))
    first = ['/bin/bash', '-c', 'printf installed > "$HOME/tool"; printf changed > result.py']
    subprocess.run(sandbox.command(first), env=sandbox.environment({}), timeout=10, check=True)
    result = subprocess.run(
        sandbox.command(['/bin/bash', '-c', 'cat "$HOME/tool"']),
        env=sandbox.environment({}),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout == 'installed'
    assert (source / 'result.py').read_text() == 'changed'


def test_no_network_namespace_and_no_secret_argv(tmp_path: Path) -> None:
    """Verify no network namespace and no secret argv."""
    source = tmp_path / 'source'
    source.mkdir()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'private', runtime, Path('/usr/bin/true'))
    command = sandbox.command(['/usr/bin/true'])
    assert '--unshare-net' not in command
    assert '--unshare-user' in command and '--unshare-pid' in command
    env = sandbox.environment({'PROVIDER_KEY': 'TEST_SECRET'})
    assert env['PROVIDER_KEY'] == 'TEST_SECRET'
    assert 'TEST_SECRET' not in ' '.join(command)


def test_open_code_runs_inside_namespace(tmp_path: Path) -> None:
    """Verify open code runs inside namespace."""
    runtime_path = os.environ.get('BENCHMARK_TEST_OPENCODE')
    if runtime_path is None:

        pytest.skip('Set BENCHMARK_TEST_OPENCODE for the pinned binary smoke')
    source = tmp_path / 'source'
    source.mkdir()
    files = tmp_path / 'runtime'
    files.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', files, Path(runtime_path))
    result = subprocess.run(
        sandbox.command(['/benchmark-bin/opencode', '--version']),
        env=sandbox.environment({}, config_content='{}'),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert result.stdout.strip() == '1.18.33'


def test_preflight_creates_private_mount_directories(tmp_path: Path) -> None:
    """Verify preflight creates private mount directories."""
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    assert sandbox.preflight()['backend'] == 'bubblewrap'
    assert sandbox.home.is_dir() and sandbox.runtime_files.is_dir()


def test_cancelled_sandbox_probe_stops_native_process(tmp_path: Path) -> None:
    """Bound cancellation while the configured sandbox executable is probing its version."""
    binary = tmp_path / 'slow-bwrap'
    binary.write_text('#!/bin/sh\nsleep 30\n')
    binary.chmod(0o700)
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox(str(binary), source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(BenchmarkError) as caught:
            sandbox.preflight(timeout=120, cancel=cancel)
        assert caught.value.code == 'operation_cancelled'
        assert time.monotonic() - started < 2
    finally:
        timer.join()
