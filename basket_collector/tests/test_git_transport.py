"""Exercise real credential projection, cancellation and retrieval budgets offline."""

import base64
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from basket_collector.git import (
    GitAuthenticationError,
    GitRepository,
    GitStopRequested,
    RejectedChange,
)


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


def test_host_credential_helper_is_used_only_for_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fetch an authenticated native pack while ignoring unrelated host Git settings."""
    upstream = tmp_path / 'source'
    head = committed_source(upstream)
    bare = tmp_path / 'repo.git'
    subprocess.run(['git', 'clone', '--bare', '-q', str(upstream), str(bare)], check=True)
    backend = subprocess.check_output(['git', '--exec-path'], text=True).strip() + '/git-http-backend'
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
        cache = GitRepository(tmp_path / 'cache', f'http://127.0.0.1:{server.server_port}/repo.git', 10)
        cache.prepare()
        cache.fetch_commit(head)
        assert cache.has_commit(head) and authenticated
        saved = (cache.path / 'config').read_text()
        assert 'helper' not in saved and 'public-fixture-only' not in saved
        assert cache._run('config', '--get', 'core.bare').strip() == b'true'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_native_object_transfer_budget_is_enforced(tmp_path: Path) -> None:
    """A small configured limit fails before the downloaded source can be accepted."""
    upstream = tmp_path / 'source'
    head = committed_source(upstream)
    cache = GitRepository(tmp_path / 'cache', str(upstream), 10, max_transfer_bytes=1)
    cache.prepare()
    with pytest.raises(RejectedChange) as caught:
        cache.fetch_commit(head)
    assert caught.value.code == 'source_fetch_budget_exceeded'


def test_fetch_auth_error_is_safe_and_does_not_try_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not misclassify rejected credentials as an unavailable frozen revision."""
    cache = GitRepository(tmp_path / 'cache', 'https://fixture.invalid/repo.git', 10)
    cache.prepare()
    executable = tmp_path / 'git'
    executable.write_text(
        '#!/bin/sh\nprintf "Authentication failed: SENSITIVE_BODY\\n" >&2\nexit 128\n', encoding='utf-8'
    )
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr('basket_collector.git.credential_environment', lambda env, _timeout, _stop: env)
    # cat-file must remain native; only the fetch process uses the synthetic failure.
    original = cache.has_commit
    monkeypatch.setattr(cache, 'has_commit', lambda _sha: False)
    with pytest.raises(GitAuthenticationError) as caught:
        cache.fetch_commit('a' * 40, ['refs/heads/main'])
    assert 'helper/askpass' in str(caught.value) and 'SENSITIVE_BODY' not in str(caught.value)
    monkeypatch.setattr(cache, 'has_commit', original)


def test_native_unshallow_failure_does_not_retry_or_replace_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classify the actual child stderr once without falling back to another revision."""
    cache = GitRepository(tmp_path / 'cache', 'https://fixture.invalid/repo.git', 10)
    cache.prepare()
    executable = tmp_path / 'git'
    calls = tmp_path / 'calls'
    executable.write_text(
        '#!/bin/sh\nprintf "fetch\\n" >> "' + str(calls) + '"\n'
        'printf "fatal: object not found: unshallow ' + 'a' * 40 + '\\n" >&2\nexit 128\n',
        encoding='utf-8',
    )
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr('basket_collector.git.credential_environment', lambda env, _timeout, _stop: env)
    monkeypatch.setattr(cache, 'has_commit', lambda _sha: False)
    with pytest.raises(RejectedChange) as error:
        cache.fetch_commit('a' * 40, ['refs/heads/main'])
    assert error.value.code == 'source_history_unavailable'
    assert error.value.observed == {
        'cause': 'git_unshallow_object_missing',
        'operation': 'fetch',
        'reference': 'a' * 40,
        'depth': 1,
        'missing_commit': 'a' * 40,
    }
    assert calls.read_text().splitlines() == ['fetch']
    assert cache._stop_event.is_set() is False


def test_shutdown_reaps_active_git_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stopped host fetch must not leave a child running or start a fallback."""
    stop = threading.Event()
    cache = GitRepository(tmp_path / 'cache', 'https://fixture.invalid/repo.git', 10, stop_event=stop)
    cache.prepare()
    executable = tmp_path / 'git'
    pid_file = tmp_path / 'pid'
    executable.write_text('#!/bin/sh\nprintf "%s" "$$" > "' + str(pid_file) + '"\nsleep 30\n', encoding='utf-8')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setattr('basket_collector.git.credential_environment', lambda env, _timeout, _stop: env)
    monkeypatch.setattr(cache, 'has_commit', lambda _sha: False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cache.fetch_commit, 'a' * 40, ['refs/heads/main'])
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        process_id = int(pid_file.read_text())
        stop.set()
        with pytest.raises(GitStopRequested):
            future.result(timeout=3)
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)
