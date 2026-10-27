"""Exact official OpenCode assets, verified caches and credential-free probes."""

import asyncio
import hashlib
import os
import platform
import re
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from benchmark.errors import BenchmarkError
from benchmark.execution.cancellation import cancellable, check_cancelled
from benchmark.execution.process import run_process
from benchmark.execution.python_runtime import PythonRuntime
from benchmark.models.runtime import ProvisioningReceipt
from benchmark.persistence.storage import bytes_sha256, file_lock, read_json, write_json
from benchmark.vcs.git import git_environment

_SELECTOR = re.compile(r'opencode:(v[0-9]+\.[0-9]+\.[0-9]+)\Z')
_RELEASE_API = 'https://api.github.com/repos/anomalyco/opencode/releases/tags/'
_DOWNLOAD_BASE = 'https://github.com/anomalyco/opencode/releases/download/'


@dataclass(frozen=True)
class Runtime:
    """One verified immutable executable and its full source receipt."""

    binary: Path
    receipt: ProvisioningReceipt
    python: PythonRuntime | None = None


def select_asset() -> tuple[str, str]:
    """Select baseline CPU code for the supported Linux x64 glibc platform."""
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise BenchmarkError(
            'unsupported_platform', 'This runtime adapter is validated on Linux x64 only.', exit_code=1
        )
    if platform.libc_ver()[0] != 'glibc':
        raise BenchmarkError('unsupported_platform', 'The initial OpenCode adapter requires glibc Linux.', exit_code=1)
    return 'opencode-linux-x64-baseline.tar.gz', 'linux-x64-glibc-baseline'


async def _download(
    tag: str, asset_name: str, destination: Path, timeout: int, supplied: httpx.AsyncClient | None
) -> dict:
    """Download an official asset into private cache staging."""
    client = supplied or httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={'User-Agent': 'pr-similarity-os-benchmark'}
    )
    try:
        async with asyncio.timeout(timeout):
            response = await client.get(_RELEASE_API + tag)
            response.raise_for_status()
            release = response.json()
            if release['tag_name'] != tag:
                raise BenchmarkError(
                    'runtime_release_mismatch', 'The runtime release does not match the requested tag.', exit_code=1
                )
            matches = [item for item in release['assets'] if item['name'] == asset_name]
            if len(matches) != 1:
                raise BenchmarkError(
                    'runtime_asset_missing', 'The requested release has no supported platform asset.', exit_code=1
                )
            asset = matches[0]
            url = _DOWNLOAD_BASE + tag + '/' + asset_name
            if asset['browser_download_url'] != url or type(asset['size']) is not int or asset['size'] <= 0:
                raise BenchmarkError(
                    'runtime_asset_invalid', 'Official release asset metadata is inconsistent.', exit_code=1
                )
            digest = asset.get('digest')
            if digest is not None and re.fullmatch(r'sha256:[0-9a-f]{64}', digest) is None:
                raise BenchmarkError(
                    'runtime_asset_invalid', 'The release uses an unsupported checksum format.', exit_code=1
                )
            hashed = hashlib.sha256()
            size = 0
            async with client.stream('GET', url, follow_redirects=True) as stream:
                stream.raise_for_status()
                with destination.open('wb') as output:
                    async for chunk in stream.aiter_bytes():
                        size += len(chunk)
                        if size > asset['size']:
                            raise BenchmarkError(
                                'runtime_asset_invalid', 'Downloaded asset exceeds its published size.', exit_code=1
                            )
                        output.write(chunk)
                        hashed.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            actual_digest = hashed.hexdigest()
            if size != asset['size'] or (digest is not None and actual_digest != digest.removeprefix('sha256:')):
                raise BenchmarkError(
                    'runtime_digest_mismatch',
                    'OpenCode download checksum or size differs; retry provisioning.',
                    exit_code=1,
                )
            return {
                'requested_version': tag,
                'release_id': release['id'],
                'asset_id': asset['id'],
                'asset_name': asset_name,
                'asset_url': url,
                'size_bytes': size,
                'published_archive_sha256': None if digest is None else digest.removeprefix('sha256:'),
                'archive_sha256': actual_digest,
                'digest_verified': digest is not None,
            }
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as exc:
        raise BenchmarkError(
            'runtime_download_failed',
            'Could not download the pinned OpenCode release; check network and retry.',
            exit_code=1,
        ) from exc
    finally:
        if supplied is None:
            await client.aclose()


def extract_binary(archive: Path, destination: Path) -> None:
    """Read the one regular CLI member; never extract tar paths or links."""
    try:
        with tarfile.open(archive, 'r:gz') as source:
            candidates = []
            for item in source.getmembers():
                parts = item.name.rstrip('/').split('/')
                if (
                    item.name.startswith('/')
                    or any(part in ('', '.', '..') for part in parts)
                    or item.issym()
                    or item.islnk()
                ):
                    raise BenchmarkError(
                        'runtime_archive_unsafe', 'OpenCode archive contains unsafe paths or links.', exit_code=1
                    )
                if not item.isdir() and not item.isfile():
                    raise BenchmarkError(
                        'runtime_archive_unsafe', 'OpenCode archive contains unsupported file types.', exit_code=1
                    )
                if item.isfile() and parts[-1] == 'opencode':
                    candidates.append(item)
            if len(candidates) != 1:
                raise BenchmarkError(
                    'runtime_binary_missing', 'OpenCode archive must contain exactly one CLI executable.', exit_code=1
                )
            member = source.extractfile(candidates[0])
            if member is None:
                raise BenchmarkError(
                    'runtime_binary_missing', 'OpenCode CLI cannot be read from the archive.', exit_code=1
                )
            with member, destination.open('wb') as output:
                while chunk := member.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            destination.chmod(0o700)
    except (tarfile.TarError, OSError) as exc:
        raise BenchmarkError(
            'runtime_archive_failed', 'Cannot unpack the pinned OpenCode asset; check disk and retry.', exit_code=1
        ) from exc


def probe_version(binary: Path, timeout: int, *, cancel: threading.Event | None = None) -> str:
    """Version checks get no host OpenCode profile or provider credentials."""
    with TemporaryDirectory(prefix='benchmark-version-') as cwd:
        environment = git_environment()
        environment.update(
            HOME=cwd,
            XDG_CONFIG_HOME=cwd + '/config',
            XDG_CACHE_HOME=cwd + '/cache',
            XDG_DATA_HOME=cwd + '/data',
            XDG_STATE_HOME=cwd + '/state',
            OPENCODE_DISABLE_AUTOUPDATE='true',
            OPENCODE_CONFIG_CONTENT='{}',
        )
        lines = []
        result = run_process(
            [str(binary), '--version'],
            env=environment,
            cwd=Path(cwd),
            timeout=timeout,
            grace=0.2,
            cancel=cancel or threading.Event(),
            log=Path(cwd) / 'version.log',
            on_stdout=lines.append,
        )
        if result.status == 'cancelled':
            check_cancelled(cancel)
        if result.status != 'completed':
            raise BenchmarkError(
                'runtime_probe_failed', 'OpenCode failed its version probe; check platform support.', exit_code=1
            )
        return '\n'.join(lines).strip().removeprefix('v')


def provision_opencode(
    selector: str,
    cache_dir: Path,
    *,
    timeout: int = 120,
    client: httpx.AsyncClient | None = None,
    cancel: threading.Event | None = None,
) -> Runtime:
    """Resolve and verify one exact OpenCode release for the supported platform.

    Args:
        cache_dir: Shared download cache guarded by an installation lock.
        timeout: Deadline for each release or archive request.
        selector: Exact version tag; latest and ambient PATH binaries are rejected.
        client: Optional injected transport for release/download validation.
        cancel: Optional signal intent for cache waits, network requests and native probes.

    Returns:
        Executable path and provenance receipt with version and content digests.

    Raises:
        BenchmarkError: If release selection, download, digest or version checks fail.

    """
    match = _SELECTOR.fullmatch(selector)
    if match is None:
        raise BenchmarkError('invalid_runtime', 'Set an exact runtime such as opencode:v1.18.33.')
    tag = match[1]
    asset_name, platform_name = select_asset()
    directory = cache_dir.expanduser().resolve() / 'opencode' / tag / platform_name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(directory / 'install.lock', timeout=timeout, cancel=cancel):
        binary = directory / 'opencode'
        receipt_path = directory / 'receipt.json'
        if binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK) and receipt_path.is_file():
            try:
                receipt = read_json(receipt_path)
                if (
                    receipt['selector'] == selector
                    and receipt['asset_name'] == asset_name
                    and receipt['platform'] == platform_name
                    and receipt['actual_version'] == tag[1:]
                    and receipt['binary_sha256'] == bytes_sha256(binary.read_bytes())
                    and probe_version(binary, timeout, cancel=cancel) == tag[1:]
                ):
                    return Runtime(binary, ProvisioningReceipt.model_validate(receipt))
            except (BenchmarkError, KeyError, OSError):
                check_cancelled(cancel)
        with TemporaryDirectory(prefix='install-', dir=directory) as temporary:
            staging = Path(temporary)
            receipt = asyncio.run(
                cancellable(_download(tag, asset_name, staging / 'archive.tar.gz', timeout, client), cancel)
            )
            check_cancelled(cancel)
            candidate = staging / 'opencode'
            extract_binary(staging / 'archive.tar.gz', candidate)
            version = probe_version(candidate, timeout, cancel=cancel)
            if version != tag[1:]:
                raise BenchmarkError(
                    'runtime_version_mismatch', 'Downloaded OpenCode version differs from the pinned tag.', exit_code=1
                )
            receipt.update(
                selector=selector,
                actual_version=version,
                platform=platform_name,
                binary_sha256=bytes_sha256(candidate.read_bytes()),
            )
            os.replace(candidate, binary)
            write_json(receipt_path, receipt)
            return Runtime(binary, ProvisioningReceipt.model_validate(receipt))
