"""Source-only filesystem sandbox with persistent private HOME and shared network."""

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.cancellation import check_cancelled
from benchmark.errors import BenchmarkError
from benchmark.git import git_environment
from benchmark.process import run_process
from benchmark.storage import bytes_sha256


@dataclass(frozen=True)
class Sandbox:
    """Define source, private state and approved readonly launch mounts."""

    executable: str
    source: Path
    cwd: Path
    home: Path
    runtime_files: Path
    binary: Path

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
        for name in ('/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ssl/certs', '/etc/ld.so.cache'):
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
        reserved = {'HOME', 'PATH', 'TMPDIR', 'SHELL', 'PYTHONHASHSEED'}
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
        if config_content is not None:
            result['OPENCODE_CONFIG_CONTENT'] = config_content
        if auth_content is not None:
            result['OPENCODE_AUTH_CONTENT'] = auth_content
        return result

    def preflight(self, timeout: int = 10, *, cancel: threading.Event | None = None) -> dict:
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
        return {
            'backend': 'bubblewrap',
            'version': '\n'.join(versions).strip(),
            'binary_sha256': bytes_sha256(Path(found).read_bytes()),
            'network': 'shared',
        }
