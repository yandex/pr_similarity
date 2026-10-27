"""Private copies of the benchmark interpreter without its installed packages."""

import hashlib
import os
import platform
import re
import shutil
import sys
import sysconfig
import threading
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.errors import BenchmarkError
from benchmark.execution.bin_projection import BinProjection
from benchmark.execution.cancellation import check_cancelled
from benchmark.execution.process import run_process
from benchmark.models.runtime import PythonFile, PythonReceipt
from benchmark.persistence.storage import file_lock

PYTHON_ROOT = '/benchmark-python'
VENV_ROOT = '/sandbox-home/.venv'
PYTHON_CONTROLS = frozenset(
    {
        'PYTHONHOME',
        'PYTHONPATH',
        'PYTHONUSERBASE',
        'PYTHONNOUSERSITE',
        'VIRTUAL_ENV',
        'PYTHONPLATLIBDIR',
        'PYTHONEXECUTABLE',
        '__PYVENV_LAUNCHER__',
        '_PYTHON_SYSCONFIGDATA_NAME',
        '_PYTHON_SYSCONFIGDATA_PATH',
        'PIP_CONFIG_FILE',
    }
)


@dataclass(frozen=True)
class PythonRuntime:
    """Bind a package-free read-only runtime tree to its measured content."""

    root: Path
    receipt: PythonReceipt
    bins: BinProjection = field(default_factory=BinProjection, compare=False, repr=False)


def _digest(path: Path) -> str:
    """Hash file contents without retaining a complete binary in memory."""
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _stdlib_files(root: Path, prefix: str = 'lib/python3.12/') -> dict[str, Path]:
    """Collect only the selected standard library, excluding package and bytecode trees."""
    result = {}
    excluded = {'site-packages', 'dist-packages', '__pycache__'}
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if name not in excluded)
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in subdirectories):
            raise BenchmarkError(
                'python_layout_unsupported', 'Python stdlib has a linked directory; use a regular install.'
            )
        for name in sorted(filenames):
            path = parent / name
            if path.suffix in {'.pyc', '.pyo'} or name in {'sitecustomize.py', 'usercustomize.py'}:
                continue
            if not path.is_file():
                raise BenchmarkError('python_layout_unsupported', 'Python stdlib contains an unreadable file.')
            result[prefix + path.relative_to(root).as_posix()] = path.resolve()
    return result


def _libraries(files: dict[str, Path], cancel: threading.Event | None) -> dict[str, Path]:
    """Resolve extension dependencies including private OpenSSL and SQLite libraries."""
    result = {}
    binaries = [path for name, path in files.items() if name.startswith('bin/') or '.so' in path.name]
    for binary in binaries:
        check_cancelled(cancel)
        output = []
        with TemporaryDirectory(prefix='benchmark-python-discovery-') as temporary:
            probe = run_process(
                ['ldd', str(binary)],
                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                cwd=Path(temporary),
                timeout=15,
                grace=0.2,
                cancel=cancel or threading.Event(),
                log=Path(temporary) / 'ldd.log',
                on_stdout=output.append,
            )
        if probe.status == 'cancelled':
            check_cancelled(cancel)
        if any('not found' in line for line in output):
            raise BenchmarkError('python_library_missing', 'A Python shared library dependency is unavailable.')
        if probe.status != 'completed':
            raise BenchmarkError('python_layout_unsupported', 'Cannot inspect Python ELF dependencies with ldd.')
        for line in output:
            match = re.search(r'(?:=>\s+|^\s*)(/[^\s]+)\s+\(', line)
            if match is None:
                continue
            library = Path(match[1])
            if library.is_relative_to('/usr') or library.is_relative_to('/lib') or library.is_relative_to('/lib64'):
                continue
            name = 'lib/' + library.name
            resolved = library.resolve()
            if name in result and result[name] != resolved:
                raise BenchmarkError('python_layout_unsupported', 'Python requires conflicting private library names.')
            result[name] = resolved
    return result


def discover_python(cancel: threading.Event | None = None) -> tuple[dict[str, Path], PythonReceipt]:
    """Measure the running setup interpreter and its standard-library dependencies.

    Returns:
        Files for a relocatable runtime and their portable content receipt.

    Raises:
        BenchmarkError: If the installation cannot supply isolated Python 3.12.

    """
    if sys.version_info[:2] != (3, 12) or sys.implementation.name != 'cpython':
        raise BenchmarkError('python_version_unsupported', 'Run make setup with Python 3.12.')
    base_executable = getattr(sys, '_base_executable', None)
    if not isinstance(base_executable, str):
        raise BenchmarkError('python_layout_unsupported', 'Cannot locate the base setup interpreter.')
    executable = Path(base_executable).resolve()
    files = _stdlib_files(Path(sysconfig.get_path('stdlib')))
    headers = Path(sysconfig.get_path('include'))
    if headers.is_dir():
        files.update(_stdlib_files(headers, 'include/python3.12/'))
    files['bin/python3.12'] = executable
    files.update(_libraries(files, cancel))
    manifest = []
    for name, path in sorted(files.items()):
        check_cancelled(cancel)
        manifest.append(PythonFile(path=name, sha256=_digest(path), size_bytes=path.stat().st_size))
    identity = hashlib.sha256()
    for item in manifest:
        identity.update(f'{item.path}\0{item.sha256}\0{item.size_bytes}\n'.encode())
    receipt = PythonReceipt(
        version=platform.python_version(),
        implementation=sys.implementation.name,
        platform=platform.system().lower() + '-' + platform.machine(),
        origin='benchmark_venv',
        content_sha256=identity.hexdigest(),
        files=manifest,
    )
    return files, receipt


def prepare_python(cache: Path, *, cancel: threading.Event | None = None) -> PythonRuntime:
    """Publish a verified immutable runtime snapshot in the benchmark cache.

    Args:
        cache: Benchmark-owned cache, never a project source or attempt directory.
        cancel: Optional shared cancellation signal.

    Returns:
        A runtime whose package-free files can be shared read-only by attempts.

    """
    files, receipt = discover_python(cancel)
    parent = cache / 'python'
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / receipt.content_sha256
    with file_lock(parent / '.lock', timeout=120, cancel=cancel):
        if destination.is_symlink():
            raise BenchmarkError('python_cache_corrupt', 'Python cache entry must not be a symlink.')
        if destination.exists():
            expected = {item.path for item in receipt.files}
            actual = set()
            for path in destination.rglob('*'):
                check_cancelled(cancel)
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise BenchmarkError('python_cache_corrupt', 'Python cache contains an unsafe entry.')
                if path.is_file():
                    actual.add(path.relative_to(destination).as_posix())
                elif not any(name.startswith(path.relative_to(destination).as_posix() + '/') for name in expected):
                    raise BenchmarkError('python_cache_corrupt', 'Python cache contains an unexpected directory.')
            if actual != expected:
                raise BenchmarkError('python_cache_corrupt', 'Python cache file inventory differs; recreate its entry.')
            for item in receipt.files:
                check_cancelled(cancel)
                path = destination / item.path
                if not path.is_file() or path.is_symlink() or _digest(path) != item.sha256:
                    raise BenchmarkError(
                        'python_cache_corrupt', 'Python cache differs; remove its damaged entry and retry.'
                    )
            return PythonRuntime(destination, receipt)
        with TemporaryDirectory(prefix='prepare-', dir=parent) as temporary:
            staging = Path(temporary) / 'runtime'
            for item in receipt.files:
                check_cancelled(cancel)
                target = staging / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(files[item.path], target)
                target.chmod(0o755 if item.path.startswith('bin/') or '.so' in target.name else 0o644)
                if _digest(target) != item.sha256:
                    raise BenchmarkError(
                        'python_source_changed', 'Python changed during preparation; retry with a stable install.'
                    )
            staging.rename(destination)
    return PythonRuntime(destination, receipt)


def validate_setup_python(*, timeout: int = 120) -> None:
    """Verify that setup's interpreter can bootstrap a clean venv with pip offline."""
    discover_python()
    with TemporaryDirectory(prefix='benchmark-python-setup-') as temporary:
        root = Path(temporary)
        venv = root / 'venv'
        environment = {'PATH': os.defpath, 'HOME': str(root), 'PYTHONNOUSERSITE': '1'}
        for argv in (
            [sys.executable, '-I', '-m', 'venv', str(venv)],
            [str(venv / 'bin/python'), '-I', '-m', 'pip', '--version'],
        ):
            result = run_process(
                argv,
                env=environment,
                cwd=root,
                timeout=timeout,
                grace=0.2,
                cancel=threading.Event(),
                log=root / 'bootstrap.log',
            )
            if result.status != 'completed':
                raise BenchmarkError(
                    'python_bootstrap_unavailable',
                    'Setup Python cannot create a clean venv with pip; repair its venv/ensurepip installation.',
                    exit_code=1,
                )


def main() -> int:
    """Report setup-time Python readiness without exposing subprocess internals."""
    try:
        validate_setup_python()
    except BenchmarkError as error:
        print(error.message, file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('Cannot verify setup Python; check its installation and local file access.', file=sys.stderr)
        return 1
    print('Python 3.12 runtime and clean venv bootstrap verified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
