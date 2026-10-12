"""Prefer the system bubblewrap, with a pinned unprivileged cache fallback."""

import asyncio
import hashlib
import os
import platform
import shutil
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from benchmark.cancellation import cancellable, check_cancelled
from benchmark.errors import BenchmarkError
from benchmark.git import git_environment
from benchmark.process import run_process
from benchmark.storage import file_lock

PACKAGE_URL = 'https://deb.debian.org/debian/pool/main/b/bubblewrap/bubblewrap_0.8.0-2+deb12u1_amd64.deb'
PACKAGE_SHA256 = '3cc9134a3286ad01a323dcd924ba123eb634cefaeec82d774257e06308aeaadb'
BINARY_SHA256 = '85580dd52ed366ece8844e90fa75ac7c4de8802963071344e123221fb9f6f11e'


def _valid(path: Path) -> bool:
    """Check that a candidate executable is a working bubblewrap binary."""
    return (
        path.is_file()
        and not path.is_symlink()
        and os.access(path, os.X_OK)
        and hashlib.sha256(path.read_bytes()).hexdigest() == BINARY_SHA256
    )


def resolve_bubblewrap(
    executable: str,
    cache_dir: Path,
    *,
    auto_install: bool = True,
    timeout: int = 120,
    cancel: threading.Event | None = None,
) -> Path:
    """Never replace a custom executable or use a privileged package installation."""
    check_cancelled(cancel)
    if executable == 'bwrap' and Path('/usr/bin/bwrap').is_file():
        return Path('/usr/bin/bwrap')
    found = shutil.which(executable)
    if found:
        return Path(found).resolve()
    if executable != 'bwrap' or not auto_install:
        raise BenchmarkError('sandbox_missing', 'Install bubblewrap or fix sandbox.executable.', exit_code=1)
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise BenchmarkError(
            'sandbox_missing', 'Automatic bubblewrap provisioning supports Linux x64; install it manually.', exit_code=1
        )
    directory = cache_dir.expanduser().resolve() / 'bubblewrap' / PACKAGE_SHA256
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = directory / 'bwrap'
    with file_lock(directory / 'install.lock', timeout=timeout, cancel=cancel):
        if _valid(binary):
            return binary
        extractor = shutil.which('dpkg-deb')
        if extractor is None:
            raise BenchmarkError(
                'sandbox_missing', 'Automatic setup requires dpkg-deb; install bubblewrap manually.', exit_code=1
            )
        try:
            with TemporaryDirectory(prefix='install-', dir=directory) as temporary:
                staging = Path(temporary)
                content = asyncio.run(cancellable(_download_package(timeout), cancel))
                check_cancelled(cancel)
                if len(content) > 1024 * 1024 or hashlib.sha256(content).hexdigest() != PACKAGE_SHA256:
                    raise BenchmarkError(
                        'sandbox_digest_mismatch', 'Bubblewrap package checksum differs; retry setup.', exit_code=1
                    )
                package = staging / 'package.deb'
                package.write_bytes(content)
                result = run_process(
                    [extractor, '--extract', str(package), str(staging / 'root')],
                    env=git_environment(),
                    cwd=staging,
                    timeout=timeout,
                    grace=0.2,
                    cancel=cancel or threading.Event(),
                    log=staging / 'extract.log',
                )
                if result.status == 'cancelled':
                    check_cancelled(cancel)
                if result.status != 'completed':
                    raise BenchmarkError(
                        'sandbox_install_failed', 'Could not unpack bubblewrap; retry setup.', exit_code=1
                    )
                candidate = staging / 'root/usr/bin/bwrap'
                if not _valid(candidate):
                    raise BenchmarkError(
                        'sandbox_digest_mismatch', 'Bubblewrap executable checksum differs.', exit_code=1
                    )
                candidate.chmod(0o700)
                os.replace(candidate, binary)
                return binary
        except (OSError, httpx.HTTPError, TimeoutError) as exc:
            raise BenchmarkError(
                'sandbox_install_failed', 'Could not prepare bubblewrap; install it manually or retry.', exit_code=1
            ) from exc


async def _download_package(timeout: int) -> bytes:
    """Fetch the pinned small package without an unbounded response buffer."""
    async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        chunks = []
        size = 0
        async with client.stream('GET', PACKAGE_URL) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 1024 * 1024:
                    raise BenchmarkError(
                        'sandbox_digest_mismatch', 'Bubblewrap package exceeds its pinned size bound.', exit_code=1
                    )
                chunks.append(chunk)
        return b''.join(chunks)
