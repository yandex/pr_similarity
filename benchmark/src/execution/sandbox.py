"""Source-only filesystem sandbox with persistent private HOME and shared network."""

import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.errors import BenchmarkError
from benchmark.execution.cancellation import check_cancelled
from benchmark.execution.process import run_process
from benchmark.execution.python_runtime import (
    PYTHON_CONTROLS,
    PYTHON_ROOT,
    VENV_ROOT,
    PythonRuntime,
)
from benchmark.execution.python_sandbox import PYTHON_PROBE, python_mounts
from benchmark.models.runtime import PythonProbe, SandboxReceipt
from benchmark.persistence.storage import bytes_sha256
from benchmark.vcs.git import git_environment


@dataclass(frozen=True)
class Sandbox:
    """Define source, private state and approved readonly launch mounts."""

    executable: str
    source: Path
    cwd: Path
    home: Path
    runtime_files: Path
    binary: Path
    python: PythonRuntime | None = None

    def __post_init__(self) -> None:
        """Validate mount ownership and initialize private HOME directories."""
        source = self.source.resolve()
        cwd = self.cwd.resolve()
        if source == Path('/') or not cwd.is_relative_to(source):
            raise BenchmarkError(
                'unsafe_sandbox', 'Sandbox cwd must stay inside a dedicated source snapshot.', exit_code=1
            )
        for directory in (self.home, self.runtime_files):
            if directory.resolve().is_relative_to(source):
                raise BenchmarkError(
                    'unsafe_sandbox', 'Private state and launcher files must stay outside source.', exit_code=1
                )
        self.runtime_files.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        for name in ('config', 'cache', 'data', 'state'):
            (self.home / name).mkdir(exist_ok=True, mode=0o700)

    def command(self, argv: list[str]) -> list[str]:
        """Mount only approved system files, the before tree and attempt-private state."""
        args = [
            self.executable,
            '--unshare-user',
            '--unshare-pid',
            '--unshare-ipc',
            '--unshare-uts',
            '--disable-userns',
            '--cap-drop',
            'ALL',
            '--die-with-parent',
            '--new-session',
        ]
        for name in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
            path = Path(name)
            if path.is_symlink():
                args.extend(['--symlink', os.readlink(path), name])
            elif path.exists():
                args.extend(['--ro-bind', name, name])
        for name in (
            '/etc/resolv.conf',
            '/etc/hosts',
            '/etc/nsswitch.conf',
            '/etc/ssl/certs',
            '/etc/ld.so.cache',
            '/etc/alternatives',
        ):
            if Path(name).exists():
                args.extend(['--ro-bind', name, name])
        args.extend(
            [
                '--proc',
                '/proc',
                '--dev',
                '/dev',
                '--tmpfs',
                '/tmp',
                '--bind',
                str(self.source.resolve()),
                '/repo',
                '--bind',
                str(self.home.resolve()),
                '/sandbox-home',
                '--ro-bind',
                str(self.runtime_files.resolve()),
                '/benchmark-runtime',
            ]
        )
        if self.python is not None:
            args.extend(python_mounts(self.python))
        metadata = self.source / '.git'
        if metadata.exists():
            # A fresh local root enables project detection without exporting original history.
            args.extend(['--ro-bind', str(metadata.resolve()), '/repo/.git'])
        args.extend(['--ro-bind', str(self.binary.resolve()), '/benchmark-bin/opencode'])
        relative = self.cwd.resolve().relative_to(self.source.resolve()).as_posix()
        args.extend(['--chdir', '/repo' if relative == '.' else '/repo/' + relative])
        args.extend(['--', *argv])
        return args

    def environment(
        self, bindings: dict[str, str], *, config_content: str | None = None, auth_content: str | None = None
    ) -> dict[str, str]:
        """Pass provider secrets only through the child environment."""
        reserved = {'HOME', 'PATH', 'TMPDIR', 'SHELL', 'PYTHONHASHSEED'} | PYTHON_CONTROLS
        if any(key in reserved or key.startswith(('XDG_', 'OPENCODE_', 'LD_')) for key in bindings):
            raise BenchmarkError('invalid_environment', 'Bindings must not replace private launcher controls.')
        result = {
            'HOME': '/sandbox-home',
            'XDG_CONFIG_HOME': '/sandbox-home/config',
            'XDG_CACHE_HOME': '/sandbox-home/cache',
            'XDG_DATA_HOME': '/sandbox-home/data',
            'XDG_STATE_HOME': '/sandbox-home/state',
            'TMPDIR': '/tmp',
            'PATH': '/benchmark-bin:/sandbox-home/.local/bin:/sandbox-home/bin:/usr/local/bin:/usr/bin:/bin',
            'SHELL': '/bin/bash',
            'LANG': 'C.UTF-8',
            'LC_ALL': 'C.UTF-8',
            'USER': 'agent',
            'LOGNAME': 'agent',
            'PYTHONHASHSEED': '42',
            'OPENCODE_DISABLE_AUTOUPDATE': 'true',
            'OPENCODE_DISABLE_SHARE': 'true',
            **bindings,
        }
        if self.python is not None:
            result.update(VIRTUAL_ENV=VENV_ROOT, PYTHONNOUSERSITE='1', PIP_CONFIG_FILE='/dev/null')
            result['PATH'] = VENV_ROOT + '/bin:' + result['PATH']
        if config_content is not None:
            result['OPENCODE_CONFIG_CONTENT'] = config_content
        if auth_content is not None:
            result['OPENCODE_AUTH_CONTENT'] = auth_content
        return result

    def preflight(self, timeout: int = 10, *, cancel: threading.Event | None = None) -> SandboxReceipt:
        """Reject namespace failures without an unsandboxed fallback."""
        found = shutil.which(self.executable)
        if found is None:
            raise BenchmarkError('sandbox_missing', 'Install bubblewrap or enable sandbox.auto_install.', exit_code=1)
        versions = []
        with TemporaryDirectory(prefix='benchmark-sandbox-probe-') as temporary:
            root = Path(temporary)
            for command, environment, callback in (
                ([found, '--version'], git_environment(), versions.append),
                (self.command(['/usr/bin/true']), self.environment({}), None),
            ):
                result = run_process(
                    command,
                    env=environment,
                    cwd=root,
                    timeout=timeout,
                    grace=0.2,
                    cancel=cancel or threading.Event(),
                    log=root / 'probe.log',
                    on_stdout=callback,
                )
                if result.status == 'cancelled':
                    check_cancelled(cancel)
                if result.status != 'completed':
                    raise BenchmarkError(
                        'sandbox_unavailable',
                        'Bubblewrap namespaces are unavailable; fix the host sandbox setup.',
                        exit_code=1,
                    )
        return SandboxReceipt(
            backend='bubblewrap',
            version='\n'.join(versions).strip(),
            binary_sha256=bytes_sha256(Path(found).read_bytes()),
            network='shared',
        )

    def _python_process(
        self,
        argv: list[str],
        *,
        timeout: int,
        cancel: threading.Event | None,
        log: Path,
        bindings: dict[str, str] | None = None,
    ) -> list[str]:
        """Run preparation without model credentials and preserve bounded diagnostics."""
        output = []
        result = run_process(
            self.command(argv),
            env=self.environment(bindings or {}),
            cwd=self.source,
            timeout=timeout,
            grace=0.2,
            cancel=cancel or threading.Event(),
            log=log,
            on_stdout=output.append,
        )
        if result.status == 'cancelled':
            check_cancelled(cancel)
        if result.status != 'completed':
            raise BenchmarkError(
                'python_preflight_failed',
                'Cannot prepare isolated Python 3.12; check the Python preparation log and make setup interpreter.',
                exit_code=1,
            )
        return output

    def prepare_python(
        self, *, timeout: int = 60, cancel: threading.Event | None = None, bindings: dict[str, str] | None = None
    ) -> PythonProbe:
        """Create one new private venv using only the selected benchmark runtime."""
        if self.python is None:
            raise BenchmarkError('python_runtime_missing', 'An isolated Python runtime is required.')
        if (self.home / '.venv').exists():
            raise BenchmarkError('python_venv_exists', 'A fresh attempt requires a new Python environment.')
        self._python_process(
            [PYTHON_ROOT + '/bin/python3.12', '-I', '-m', 'venv', VENV_ROOT],
            timeout=timeout,
            cancel=cancel,
            log=self.runtime_files / 'python-prepare.log',
        )
        return self.check_python(timeout=timeout, cancel=cancel, bindings=bindings)

    def check_python(
        self, *, timeout: int = 30, cancel: threading.Event | None = None, bindings: dict[str, str] | None = None
    ) -> PythonProbe:
        """Reject a missing or modified attempt environment before invoking the model."""
        (self.runtime_files / 'python-probe.py').write_text(PYTHON_PROBE)
        lines = self._python_process(
            [VENV_ROOT + '/bin/python', '/benchmark-runtime/python-probe.py'],
            timeout=timeout,
            cancel=cancel,
            log=self.runtime_files / 'python-check.log',
            bindings=bindings,
        )
        return PythonProbe.model_validate(json.loads('\n'.join(lines)))

    def save_python_logs(self, destination: Path) -> None:
        """Retain credential-free preparation logs before private state is deleted."""
        for name in ('python-prepare.log', 'python-check.log'):
            source = self.runtime_files / name
            if source.is_file():
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination / name)
