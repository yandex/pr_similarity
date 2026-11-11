"""Exercise the real benchmark publisher against a local HTTP viewer, without providers."""

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from test_producer_roundtrip import _producer_archive
from viewer.config import Config
from viewer.errors import StorageError
from viewer.local_service import LocalViewerService
from viewer.persistence.storage import MemoryStore
from viewer.web.api import create_app

SCRIPT = r'''
"""Probe publisher reply recovery and administrative overwrite through local HTTP."""

import json
import os
import sys
from pathlib import Path
import httpx
from benchmark.errors import BenchmarkError
from benchmark.publication.publish import publish

origin, directory = sys.argv[1:]


class LocalTransport(httpx.BaseTransport):
    """Route verified viewer requests locally and lose the first durable PUT response."""

    lose_reply = True

    def __init__(self) -> None:
        """Create the real HTTP transport used by the fixture proxy."""
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Rewrite the origin and inject one lost publication response."""
        request.url = httpx.URL(origin + request.url.raw_path.decode())
        response = self.inner.handle_request(request)
        if request.method == 'PUT' and self.lose_reply:
            self.lose_reply = False
            response.read()
            response.close()
            raise httpx.ReadError('Injected lost durable reply', request=request)
        return response

    def close(self) -> None:
        """Close the underlying HTTP connections."""
        self.inner.close()


with httpx.Client(transport=LocalTransport()) as client, httpx.Client(transport=LocalTransport()) as status:
    status._transport.lose_reply = False
    token = client.post(
        'https://viewer.example/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}
    ).json()['token']
    os.environ['FIXTURE_GRANT'] = token
    kwargs = dict(client=client, status_client=status, token_env='FIXTURE_GRANT', progress='off')
    try:
        result = publish(Path(directory), 'https://viewer.example', **kwargs)
    except BenchmarkError as error:
        assert error.code == 'publish_failed', error.code
    assert publish(Path(directory), 'https://viewer.example', **kwargs) == 0
    rid = json.loads((Path(directory) / 'report.json').read_text())['run']['id']
    row = client.get('https://viewer.example/api/v1/runs/' + rid).json()
    assert row['user'] == 'alice'
    token = client.post(
        'https://viewer.example/api/v1/auth/admin-overwrite-token', json={'login': 'operator', 'password': 'secret'}
    ).json()['token']
    os.environ['FIXTURE_ADMIN_GRANT'] = token
    assert (
        publish(
            Path(directory),
            'https://viewer.example',
            client=client,
            status_client=status,
            overwrite=True,
            admin_overwrite_token_env='FIXTURE_ADMIN_GRANT',
            expected_publication_sha256=row['publication_sha256'],
            progress='off',
        )
        == 0
    )
    assert client.get('https://viewer.example/api/v1/runs/' + rid).json()['user'] == 'alice'
'''


def test_real_publisher_recovers_reply_and_admin_overwrites_with_s3_unavailable(tmp_path: Path) -> None:
    """Recover a lost publish response and preserve ownership during admin overwrite."""
    archive = _producer_archive(tmp_path)
    exported = archive.parent
    run = exported / json.loads((exported / 'current.json').read_text())['run_directory']

    class UnavailableStore(MemoryStore):
        """Reject backup writes while local publication remains available."""

        def put_if_absent(self, key: str, data: bytes) -> bool:
            """Reject backup publication while the HTTP producer commits locally."""
            raise StorageError()

    token = tmp_path / 'admin-token'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(
        runtime=tmp_path / 'viewer',
        admin_token_file=token,
        storage='mock',
        publication_storage='local',
        disk_reserve_bytes=0,
    )
    app = create_app(config, UnavailableStore())
    service = app.state.service
    assert isinstance(service, LocalViewerService)
    service.db.create_user('alice', 'secret')
    service.db.create_user('operator', 'secret', role='admin')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='error'))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        benchmark = Path(__file__).parents[2] / 'benchmark'
        result = subprocess.run(
            [str(benchmark / '.venv/bin/python'), '-c', SCRIPT, origin, str(run)],
            cwd=benchmark,
            env={**os.environ, 'PYTHONHASHSEED': '42'},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        records = service.repository.records()
        assert len(records) == 1 and records[0].owner == 'alice' and records[0].generation == 2
        assert records[0].backup_state != 'backed_up'
    finally:
        server.should_exit = True
        thread.join(20)
        assert not thread.is_alive()
