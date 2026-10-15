"""Prefer a capable system bubblewrap, with a pinned unprivileged cache fallback."""

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
import threading
from collections.abc import Callable
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
# Quoted in diagnostics only: support is decided by the capability probe below, so a
# build that backports the required options is still accepted.
MINIMUM_VERSION = '0.8.0'
# A parse-only invocation that exercises every namespace option the sandbox command
# uses, without creating a sandbox: the trailing command has no mounted root to run in.
CAPABILITY_ARGV = (
    '--unshare-user',
    '--unshare-pid',
    '--unshare-ipc',
    '--unshare-uts',
    '--disable-userns',
    '--cap-drop',
    'ALL',
    '--die-with-parent',
    '--new-session',
    '--',
    'true',
)
PROBE_TIMEOUT_SECONDS = 10
_OPTION_REJECTIONS = ('unknown option', 'unrecognized option', 'invalid option')


def _valid(path: Path) -> bool:
    """Check that a candidate executable is a working bubblewrap binary."""
    return (
        path.is_file()
        and not path.is_symlink()
        and os.access(path, os.X_OK)
        and hashlib.sha256(path.read_bytes()).hexdigest() == BINARY_SHA256
    )


def _version(executable: Path, timeout: int) -> str:
    """Return a printable version for a discovered binary, or an explicit unknown."""
    try:
        result = subprocess.run(
            [str(executable), '--version'], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    reported = f'{result.stdout or ""}{result.stderr or ""}'.strip().splitlines()
    return reported[0] if reported else 'unknown'


def _capable(executable: Path, timeout: int) -> bool:
    """Report whether one candidate accepts the options the sandbox command requires.

    Only an unknown option is a rejection: a binary that parses the options and then
    fails for a host reason is still usable, because the preflight probe reports that
    failure separately. Both output streams are read, because a rejection may arrive
    on either one while the other carries unrelated diagnostics.
    """
    try:
        result = subprocess.run(
            [str(executable), *CAPABILITY_ARGV], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    message = f'{result.stdout or ""}{result.stderr or ""}'.lower()
    return not any(marker in message for marker in _OPTION_REJECTIONS)


def resolve_bubblewrap(
    executable: str,
    cache_dir: Path,
    *,
    auto_install: bool = True,
    timeout: int = 120,
    cancel: threading.Event | None = None,
    on_notice: Callable[[str], None] | None = None,
) -> Path:
    """Never replace a custom executable or use a privileged package installation.

    A configured custom name is used as given: the operator owns that choice. The
    default ``bwrap`` is probed for the options the sandbox command requires, and a
    build that rejects one is passed over in favor of the verified pinned copy.

    Args:
        executable: Configured name or path; a custom name is always used as given.
        cache_dir: Parent directory for the verified pinned fallback.
        auto_install: Permit provisioning the pinned package when nothing usable exists.
        timeout: Bound for package work, in seconds.
        cancel: Cooperative cancellation for provisioning work.
        on_notice: Optional sink for a substitution warning the caller can present.

    Returns:
        A bubblewrap executable that accepts the options the sandbox requires.

    """
    check_cancelled(cancel)
    found = shutil.which(executable)
    candidate = Path(found).resolve() if found else None
    if candidate is not None and executable != 'bwrap':
        return candidate
    if candidate is not None and _capable(candidate, min(timeout, PROBE_TIMEOUT_SECONDS)):
        return candidate
    # A discovered build that fails the probe rejects an option the sandbox command needs.
    incompatible = None
    if candidate is not None:
        version = _version(candidate, min(timeout, PROBE_TIMEOUT_SECONDS))
        incompatible = f'{candidate} reports {version} and cannot create the required sandbox'
    if executable != 'bwrap' or (incompatible is None and not auto_install):
        raise BenchmarkError(
            'sandbox_missing',
            f'Install bubblewrap {MINIMUM_VERSION} or newer, or fix sandbox.executable.',
            exit_code=1,
        )
    if incompatible is not None and not auto_install:
        raise BenchmarkError(
            'sandbox_missing',
            f'{incompatible}. Install bubblewrap {MINIMUM_VERSION} or newer, '
            f'or set sandbox.auto_install: true to use the verified pinned copy.',
            exit_code=1,
        )
    # Announced only here, where automatic provisioning is enabled and about to be used.
    if incompatible is not None and on_notice is not None:
        on_notice(f'{incompatible}; using the verified pinned fallback.')
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
                'sandbox_missing',
                f'Install bubblewrap {MINIMUM_VERSION} or newer manually, '
                f'or install dpkg-deb to use the automatic setup.',
                exit_code=1,
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
