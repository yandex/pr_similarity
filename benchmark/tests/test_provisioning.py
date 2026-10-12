"""Exact release selection, checked archives and cache recovery without network."""

import asyncio
import hashlib
import io
import tarfile
import threading
import time
from pathlib import Path

import httpx
import pytest
from benchmark.errors import BenchmarkError
from benchmark.provisioning import probe_version, provision_opencode
from benchmark.storage import file_lock


def archive(name: str = 'opencode', body: bytes = b'#!/bin/sh\nprintf "1.18.33\\n"\n') -> bytes:
    """Build an in-memory release archive containing a configurable executable."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        info = tarfile.TarInfo(name)
        info.mode = 0o755
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return stream.getvalue()


def client_for(content: bytes, *, expected_digest: str | None = None) -> tuple[httpx.AsyncClient, list[str]]:
    """Create a mock release client and its ordered request-path history."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Serve a pinned release manifest and executable archive from memory."""
        calls.append(request.url.path)
        if '/releases/tags/' in request.url.path:
            return httpx.Response(
                200,
                json={
                    'id': 1,
                    'tag_name': 'v1.18.33',
                    'assets': [
                        {
                            'id': 2,
                            'name': 'opencode-linux-x64-baseline.tar.gz',
                            'size': len(content),
                            'digest': expected_digest or 'sha256:' + hashlib.sha256(content).hexdigest(),
                            'browser_download_url': (
                                'https://github.com/anomalyco/opencode/releases/download/'
                                'v1.18.33/opencode-linux-x64-baseline.tar.gz'
                            ),
                        }
                    ],
                },
            )
        return httpx.Response(200, content=content)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond)), calls


def test_checked_cache_does_not_repeat_download(tmp_path: Path) -> None:
    """Verify checked cache does not repeat download."""
    client, calls = client_for(archive())
    one = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    two = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert one.binary == two.binary
    assert one.receipt['actual_version'] == '1.18.33'
    assert one.receipt['digest_verified'] is True
    assert len(calls) == 2


def test_corrupt_cached_binary_is_replaced(tmp_path: Path) -> None:
    """Verify corrupt cached binary is replaced."""
    client, calls = client_for(archive())
    one = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    one.binary.write_text('corrupted')
    two = provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert two.binary.read_bytes().startswith(b'#!/bin/sh')
    assert len(calls) == 4


@pytest.mark.parametrize('name', ['../opencode', '/tmp/opencode'])
def test_unsafe_archive_never_installs(tmp_path: Path, name: str) -> None:
    """Verify unsafe archive never installs."""
    client, _calls = client_for(archive(name))
    with pytest.raises(BenchmarkError):
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert not [path for path in tmp_path.rglob('opencode') if path.is_file()]


def test_published_digest_mismatch_fails_before_version_probe(tmp_path: Path) -> None:
    """Verify published digest mismatch fails before version probe."""
    client, _calls = client_for(archive(), expected_digest='sha256:' + 'a' * 64)
    with pytest.raises(BenchmarkError) as caught:
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert caught.value.code == 'runtime_digest_mismatch'


def test_actual_version_mismatch_is_not_cached(tmp_path: Path) -> None:
    """Verify actual version mismatch is not cached."""
    client, _calls = client_for(archive(body=b'#!/bin/sh\necho 1.2.3\n'))
    with pytest.raises(BenchmarkError) as caught:
        provision_opencode('opencode:v1.18.33', tmp_path, client=client)
    assert caught.value.code == 'runtime_version_mismatch'


@pytest.mark.parametrize('selector', ['opencode:latest', 'opencode:v1.x.y', 'opencode:>=1.0', 'opencode:v1.2'])
def test_runtime_is_exact(selector: str, tmp_path: Path) -> None:
    """Verify runtime is exact."""
    with pytest.raises(BenchmarkError):
        provision_opencode(selector, tmp_path)


def test_relative_cache_is_resolved_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify relative cache is resolved once."""
    monkeypatch.chdir(tmp_path)
    client, calls = client_for(archive())
    one = provision_opencode('opencode:v1.18.33', Path('relative-cache'), client=client)
    two = provision_opencode('opencode:v1.18.33', Path('relative-cache'), client=client)
    assert one.binary.is_absolute() and one.binary == two.binary
    assert len(calls) == 2


def test_cancelled_runtime_cache_wait_does_not_wait_for_timeout(tmp_path: Path) -> None:
    """Cancel a real held cache lock promptly without reporting infrastructure timeout."""
    directory = tmp_path / 'opencode/v1.18.33/linux-x64-glibc-baseline'
    directory.mkdir(parents=True)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    with file_lock(directory / 'install.lock'):
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(BenchmarkError) as caught:
                provision_opencode('opencode:v1.18.33', tmp_path, timeout=120, cancel=cancel)
            assert caught.value.code == 'operation_cancelled'
            assert time.monotonic() - started < 1
        finally:
            timer.join()


def test_cancelled_runtime_request_reaps_owned_async_operation(tmp_path: Path) -> None:
    """Abort an in-flight mock request and await its cleanup before returning cancellation."""
    cancel = threading.Event()
    cleaned = threading.Event()

    async def slow(_request: httpx.Request) -> httpx.Response:
        """Simulate bounded startup I/O that must be cancelled before completion."""
        try:
            await asyncio.sleep(30)
            raise AssertionError('startup request should have been cancelled')
        finally:
            cleaned.set()

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(BenchmarkError) as caught:
            provision_opencode('opencode:v1.18.33', tmp_path, timeout=120, client=client, cancel=cancel)
        assert caught.value.code == 'operation_cancelled'
        assert cleaned.is_set() and time.monotonic() - started < 1
    finally:
        timer.join()
        asyncio.run(client.aclose())


def test_cancelled_native_version_probe_terminates_process(tmp_path: Path) -> None:
    """Cancel an actual native process instead of waiting for the startup probe deadline."""
    binary = tmp_path / 'probe'
    binary.write_text('#!/bin/sh\nsleep 30\n')
    binary.chmod(0o700)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(BenchmarkError) as caught:
            probe_version(binary, 120, cancel=cancel)
        assert caught.value.code == 'operation_cancelled'
        assert time.monotonic() - started < 2
    finally:
        timer.join()
