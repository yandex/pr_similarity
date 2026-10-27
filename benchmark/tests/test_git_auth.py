"""Host Git authentication is verified independently with a local native HTTP server."""

import base64
import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import pytest

from benchmark.vcs import git as module
from benchmark.vcs.git import GitCache
from benchmark.vcs.git_auth import credential_environment, isolated_environment


def committed_source(directory: Path) -> str:
    """Create real objects for retrieval tests without remote systems."""
    directory.mkdir()
    for arguments in [
        ('init', '-q', '--initial-branch=main'),
        ('config', 'user.name', 'Fixture'),
        ('config', 'user.email', 'fixture@example.invalid'),
    ]:
        subprocess.run(['git', '-C', str(directory), *arguments], check=True)
    (directory / 'file.py').write_text('value = 1\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(directory), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(directory), 'commit', '-qm', 'Fixture'], check=True)
    return subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()


def configure_host_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install an owned synthetic credential helper and unrelated host Git settings."""
    home = tmp_path / 'home'
    home.mkdir()
    helper = home / 'helper'
    helper.write_text(
        '#!/bin/sh\ncat >/dev/null\n'
        'if [ "$1" = get ]; then printf "username=fixture\\npassword=public-fixture-only\\n"; fi\n',
        encoding='utf-8',
    )
    helper.chmod(0o700)
    (home / '.gitconfig').write_text(
        '[credential]\n\thelper = ' + str(helper) + '\n'
        '[init]\n\ttemplateDir = /invalid-host-template\n'
        '[core]\n\thooksPath = /invalid-host-hooks\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('GIT_CONFIG_GLOBAL', raising=False)


def test_host_credential_helper_is_used_only_for_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch an authenticated native pack while ignoring unrelated host Git settings."""
    upstream = tmp_path / 'source'
    head = committed_source(upstream)
    bare = tmp_path / 'repo.git'
    subprocess.run(['git', 'clone', '--bare', '-q', str(upstream), str(bare)], check=True)
    backend = subprocess.check_output(['git', '--exec-path'], text=True).strip() + '/git-http-backend'
    configure_host_helper(tmp_path, monkeypatch)
    authenticated: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        """Serve the owned fixture via the real Git smart HTTP backend."""

        def log_message(self, format: str, *args: object) -> None:
            """Suppress HTTP logs so authorization headers can never enter output."""

        def serve(self) -> None:
            """Challenge once, then pass authenticated requests to the native backend."""
            expected = 'Basic ' + base64.b64encode(b'fixture:public-fixture-only').decode('ascii')
            if self.headers.get('Authorization') != expected:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="fixture"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            authenticated.append(self.path)
            target = urlsplit(self.path)
            env = os.environ.copy()
            env.update(
                {
                    'GIT_PROJECT_ROOT': str(tmp_path),
                    'GIT_HTTP_EXPORT_ALL': '1',
                    'REQUEST_METHOD': self.command,
                    'PATH_INFO': target.path,
                    'QUERY_STRING': target.query,
                    'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                    'CONTENT_LENGTH': self.headers.get('Content-Length', '0'),
                    'REMOTE_USER': 'fixture',
                }
            )
            body = self.rfile.read(int(env['CONTENT_LENGTH']))
            response = subprocess.run([backend], env=env, input=body, capture_output=True, check=True).stdout
            headers, separator, payload = response.partition(b'\r\n\r\n')
            assert separator
            lines = [line.decode('ascii') for line in headers.split(b'\r\n')]
            status = next((int(line.split()[1]) for line in lines if line.startswith('Status:')), 200)
            self.send_response(status)
            for line in lines:
                name, value = line.split(':', 1)
                if name != 'Status':
                    self.send_header(name, value.strip())
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            """Serve reference discovery."""
            self.serve()

        def do_POST(self) -> None:
            """Serve pack negotiation."""
            self.serve()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        cache = GitCache(tmp_path / 'cache', 'https://fixture.invalid/repo.git', timeout=10)
        endpoint = f'http://127.0.0.1:{server.server_port}/repo.git'
        native = cast(Callable[..., bytes], module.run_git)

        def local(arguments: list[str], **kwargs: object) -> bytes:
            """Route the fixture transport while preserving actual host credential projection."""
            if 'fetch' in arguments:
                assert cache.clone_url in arguments
                arguments = [endpoint if value == cache.clone_url else value for value in arguments]
            return native(arguments, **kwargs)

        monkeypatch.setattr(module, 'run_git', local)
        cache.ensure([head])
        assert cache.has_commit(head) and authenticated
        saved = (cache.path / 'config').read_text()
        assert 'helper' not in saved and 'public-fixture-only' not in saved
        assert cache.run('config', '--get', 'core.bare').strip() == b'true'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_credential_probe_is_cancelled_and_reaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Global credential discovery follows the same cancellation contract as fetch."""
    executable = tmp_path / 'git'
    process_id = tmp_path / 'pid'
    executable.write_text('#!/bin/sh\nprintf "%s" "$$" > "' + str(process_id) + '"\nsleep 30\n', encoding='utf-8')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation = pool.submit(credential_environment, isolated_environment(), 10, stop)
        deadline = time.monotonic() + 5
        while not process_id.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process_id.exists()
        identifier = int(process_id.read_text())
        stop.set()
        with pytest.raises(RuntimeError, match='cancelled'):
            operation.result(timeout=3)
        with pytest.raises(ProcessLookupError):
            os.kill(identifier, 0)


def test_operator_trace_overrides_do_not_persist_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore inherited curl/Git trace destinations that could write credentials."""
    monkeypatch.setenv('GIT_TRACE_CURL', '/untrusted/trace-output')
    monkeypatch.setenv('GIT_TRACE_CURL_NO_DATA', '0')
    monkeypatch.setenv('GIT_CURL_VERBOSE', '1')
    environment = isolated_environment()
    assert not any(key.startswith('GIT_TRACE') for key in environment)
    assert 'GIT_CURL_VERBOSE' not in environment
