"""Exact immutable Git objects and sanitized, bounded Git subprocesses."""

import hashlib
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.git_auth import credential_environment, isolated_environment
from benchmark.sources import fetch_ref, https_url
from benchmark.storage import file_lock

_SHA = re.compile(r'[0-9a-f]{40}\Z')


def safe_path(value: str, *, allow_root: bool = False) -> str:
    """Require a normalized POSIX path, never a repository metadata path."""
    if allow_root and value == '.':
        return value
    if (
        not value
        or value.startswith('/')
        or '\\' in value
        or '\x00' in value
        or any(part in ('', '.', '..', '.git') for part in value.split('/'))
    ):
        raise BenchmarkError('unsafe_path', 'A repository path is unsafe or not normalized.')
    return value


def git_environment() -> dict[str, str]:
    """Ignore host Git config/hooks/filter overrides and do not pass API keys."""
    env = {
        key: os.environ[key]
        for key in ('PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY')
        if key in os.environ
    }
    env.update(
        PATH=env.get('PATH', os.defpath),
        HOME='/nonexistent',
        LC_ALL='C',
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM='1',
        GIT_NO_REPLACE_OBJECTS='1',
        GIT_TERMINAL_PROMPT='0',
        GIT_LFS_SKIP_SMUDGE='1',
        GIT_AUTHOR_NAME='Benchmark',
        GIT_COMMITTER_NAME='Benchmark',
        GIT_AUTHOR_EMAIL='benchmark@example.invalid',
        GIT_COMMITTER_EMAIL='benchmark@example.invalid',
    )
    return env


def _raise_git_error(stderr: bytes, host_fetch: bool) -> None:
    """Classify object and transport failures into actionable benchmark errors."""
    if host_fetch:
        detail = stderr.decode('utf-8', errors='replace').lower()
        if any(
            marker in detail
            for marker in (
                'authentication failed',
                'could not read username',
                'could not read password',
                'access denied',
                'http 401',
                'http 403',
                'requested url returned error: 401',
                'requested url returned error: 403',
            )
        ):
            raise BenchmarkError(
                'git_auth_error',
                'Git access denied; configure the host credential helper/askpass and check clone/fetch permissions. '
                'Agent API credentials do not configure Git.',
                exit_code=1,
            )
        if any(
            marker in detail
            for marker in (
                'not our ref',
                'couldn\'t find remote ref',
                'unadvertised object',
                'does not allow request',
                'no such ref',
            )
        ):
            raise BenchmarkError(
                'git_reference_unavailable',
                'The Git server no longer delivers the requested ref or object.',
                exit_code=1,
            )
        raise BenchmarkError(
            'git_fetch_failed',
            'Git fetch failed; check the fetch URL, host credentials, TLS and network, then resume.',
            exit_code=1,
        )
    raise BenchmarkError(
        'git_failed', 'Git object inspection failed; check the cache and available disk space.', exit_code=1
    )


def run_git(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    stop: threading.Event | None = None,
    accepted: tuple[int, ...] = (0,),
    input_bytes: bytes | None = None,
    host_fetch: bool = False,
    monitor: Callable[[], None] | None = None,
) -> bytes:
    """Reap an entire owned process group on timeout/cancellation."""
    if stop is not None and stop.is_set():
        raise BenchmarkError('operation_cancelled', 'Git preparation was cancelled.', exit_code=130)
    environment = git_environment()
    if host_fetch:
        try:
            environment = credential_environment(isolated_environment(), timeout, stop)
            environment['LC_ALL'] = 'C'
        except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired) as exc:
            if stop is not None and stop.is_set():
                raise BenchmarkError('operation_cancelled', 'Git preparation was cancelled.', exit_code=130) from exc
            raise BenchmarkError(
                'git_auth_configuration',
                'Cannot read host Git credentials; check the global credential helper/askpass.',
                exit_code=1,
            ) from exc
    try:
        process = subprocess.Popen(
            [
                'git',
                '-c',
                'core.hooksPath=/dev/null',
                '-c',
                'core.attributesFile=/dev/null',
                '-c',
                'gc.auto=0',
                *arguments,
            ],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise BenchmarkError(
            'git_unavailable', 'Cannot start Git; install it and check the configured paths.', exit_code=1
        ) from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            if stop is not None and stop.is_set():
                raise BenchmarkError('operation_cancelled', 'Git preparation was cancelled.', exit_code=130)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BenchmarkError('git_timeout', 'Git preparation timed out; check network and retry.', exit_code=1)
            if monitor is not None:
                monitor()
            try:
                stdout, stderr = process.communicate(input=input_bytes, timeout=min(remaining, 0.1))
                if monitor is not None:
                    monitor()
                break
            except subprocess.TimeoutExpired:
                input_bytes = None
                continue
        if process.returncode not in accepted:
            _raise_git_error(stderr, host_fetch)
        return stdout
    except BaseException:
        # The leader can exit while a credential helper still owns its pipe.
        # Reap the whole group even when poll() already reports the leader's exit.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise


@dataclass(frozen=True)
class TreeEntry:
    """One immutable tree file; blob modes are preserved."""

    mode: str
    oid: str


class GitCache:
    """Share objects through a per-repository lock; never share task workspaces."""

    def __init__(
        self,
        cache_dir: Path,
        clone_url: str,
        *,
        timeout: int = 120,
        stop: threading.Event | None = None,
        initial_depth: int = 1,
        max_depth: int = 4096,
        max_transfer_bytes: int = 536870912,
    ) -> None:
        """Configure a locked cache for exact frozen repository revisions."""
        self.clone_url = https_url(clone_url)
        key = hashlib.sha256(clone_url.encode('utf-8')).hexdigest()
        self.path = cache_dir / key
        self.timeout = timeout
        self.stop = stop
        self.initial_depth = initial_depth
        self.max_depth = max_depth
        self.max_transfer_bytes = max_transfer_bytes
        self.received_bytes = 0
        if min(timeout, initial_depth, max_depth, max_transfer_bytes) <= 0 or initial_depth > max_depth:
            raise BenchmarkError(
                'invalid_git_limits',
                'Git retrieval limits must be positive and initial_depth must not exceed max_depth.',
            )

    def run(self, *arguments: str) -> bytes:
        """Run Git against this cache with its timeout and cancellation policy."""
        return run_git(['--git-dir', str(self.path), *arguments], timeout=self.timeout, stop=self.stop)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize writers of this repository cache."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path.with_suffix('.lock'), timeout=self.timeout, cancel=self.stop):
            yield

    def has_commit(self, sha: str) -> bool:
        """Distinguish absent objects from infrastructure failure without parsing stderr."""
        output = (
            run_git(
                ['--git-dir', str(self.path), 'cat-file', '--batch-check=%(objecttype)'],
                timeout=self.timeout,
                stop=self.stop,
                input_bytes=(sha + '\n').encode('ascii'),
            )
            .decode('ascii')
            .strip()
        )
        if output == sha + ' missing':
            return False
        if output not in ('commit', 'tree', 'blob', 'tag'):
            raise BenchmarkError('git_corrupt', 'Cached Git object metadata is invalid.', exit_code=1)
        return output == 'commit'

    def object_bytes(self) -> int:
        """Count object and temporary-pack growth for a conservative transfer cap."""
        total = 0
        for parent, _directories, names in os.walk(self.path / 'objects'):
            for name in names:
                try:
                    total += (Path(parent) / name).stat().st_size
                except FileNotFoundError:
                    continue
        return total

    def fetch(self, reference: str, depth: int) -> None:
        """Fetch one delivery hint under timeout, cancellation and object-growth limits."""
        high_water = self.object_bytes()

        def budget() -> None:
            """Stop the owned child when cumulative downloaded object growth exceeds its cap."""
            nonlocal high_water
            size = self.object_bytes()
            self.received_bytes += max(0, size - high_water)
            high_water = max(high_water, size)
            if self.received_bytes > self.max_transfer_bytes:
                raise BenchmarkError(
                    'git_fetch_budget_exceeded',
                    'Git object transfer exceeded git.max_transfer_bytes; '
                    'increase the limit or use a smaller basket.',
                    exit_code=1,
                )

        run_git(
            [
                '--git-dir',
                str(self.path),
                'fetch',
                '--quiet',
                '--no-tags',
                '--no-recurse-submodules',
                '--depth=' + str(depth),
                self.clone_url,
                reference,
            ],
            timeout=self.timeout,
            stop=self.stop,
            host_fetch=True,
            monitor=budget,
        )

    def _deliver(self, sha: str, references: list[str]) -> None:
        """Use ordered ref hints solely to deliver the exact requested object."""
        if self.has_commit(sha):
            return
        try:
            self.fetch(sha, self.initial_depth)
        except BenchmarkError as exc:
            if exc.code != 'git_reference_unavailable':
                raise
        if self.has_commit(sha):
            return
        depth = self.initial_depth
        while references and depth <= self.max_depth:
            for reference in references:
                try:
                    self.fetch(reference, depth)
                except BenchmarkError as exc:
                    if exc.code != 'git_reference_unavailable':
                        raise
                if self.has_commit(sha):
                    return
            if depth == self.max_depth:
                break
            depth = min(self.max_depth, depth * 2)
        raise BenchmarkError(
            'source_revision_unavailable',
            'Frozen Git commit cannot be obtained within the fetch budget; '
            'check host access and source availability or increase Git depth.',
            exit_code=1,
        )

    def ensure(self, revisions: list[str], *, fetch_refs: list[str] | None = None) -> None:
        """Fetch exact source commits without hosting-specific API calls or ref construction."""
        if not revisions or any(_SHA.fullmatch(sha) is None for sha in revisions):
            raise BenchmarkError('invalid_revision', 'Basket revisions must be exact lowercase Git commit SHAs.')
        with self.lock():
            if not self.path.exists():
                run_git(['init', '--bare', '-q', str(self.path)], timeout=self.timeout, stop=self.stop)
            if self.run('rev-parse', '--is-bare-repository').strip() != b'true':
                raise BenchmarkError('git_corrupt', 'Git cache is not an isolated bare repository.', exit_code=1)
            references = [fetch_ref(reference) for reference in (fetch_refs or [])]
            for sha in revisions:
                self._deliver(sha, references)
            self.run('fsck', '--full', '--strict', '--no-reflogs')

    def tree(self, revision: str) -> dict[str, TreeEntry]:
        """Read paths as NUL records, preserving spaces and Unicode."""
        if not _SHA.fullmatch(revision):
            raise BenchmarkError('invalid_revision', 'Expected an exact Git commit SHA.')
        result = {}
        for record in self.run('ls-tree', '-rz', revision).split(b'\0'):
            if not record:
                continue
            try:
                header, encoded_path = record.split(b'\t', 1)
                mode, _kind, oid = header.decode('ascii').split()
                path = safe_path(encoded_path.decode('utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise BenchmarkError(
                    'unsupported_snapshot', 'The revision contains an unsupported repository path.', exit_code=1
                ) from exc
            result[path] = TreeEntry(mode, oid)
        return result

    def blob(self, entry: TreeEntry) -> bytes:
        """Read and validate one frozen Git blob."""
        content = self.run('cat-file', 'blob', entry.oid)
        actual = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()
        if actual != entry.oid:
            raise BenchmarkError('git_corrupt', 'A cached Git blob is damaged; repair the object cache.', exit_code=1)
        return content

    def verify_reference(self, row: dict) -> None:
        """Prove exported paths/blobs/modes against the frozen Git endpoints."""
        for role in ('before', 'after'):
            actual = self.run('rev-parse', row['revisions'][role] + '^{tree}').decode('ascii').strip()
            if actual != row['revisions'][role + '_tree']:
                raise BenchmarkError(
                    'reference_mismatch', 'Frozen Git tree identity differs from the basket.', exit_code=1
                )
        trees = {role: self.tree(row['revisions'][role]) for role in ('before', 'after')}
        changed = {
            path
            for path in trees['before'].keys() | trees['after'].keys()
            if trees['before'].get(path) != trees['after'].get(path)
        }
        expected = {item['path'] for item in row['changes']['files']}
        if changed != expected:
            raise BenchmarkError(
                'reference_mismatch', 'Basket changed paths disagree with its Git revisions.', exit_code=1
            )
        for item in row['changes']['files']:
            for role, tree in trees.items():
                entry = tree.get(item['path'])
                mode = None if entry is None else entry.mode
                oid = None if entry is None else entry.oid
                if mode != item[f'{role}_mode'] or oid != item[f'{role}_blob']:
                    raise BenchmarkError(
                        'reference_mismatch', 'Basket file receipts disagree with Git objects.', exit_code=1
                    )
