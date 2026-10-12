"""Read exact PR revisions and text changes from an isolated bare Git cache."""

import fcntl
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from basket_collector.contracts import (
    ChangedFile,
    Changes,
    ChangeStats,
    CommitMessage,
    JsonValue,
    ResolvedRevisions,
    SourceRevisions,
)
from basket_collector.git_auth import credential_environment, isolated_environment
from basket_collector.sources import RetrievalReceipt, SourceDescriptor, source_hash
from basket_collector.storage import utc_now

_SHA = re.compile(r'[0-9a-f]{40}\Z')
_UNSHALLOW_MISSING = re.compile(r'fatal: object not found: unshallow ([0-9a-f]{40})')
_LFS = b'version https://git-lfs.github.com/spec/v1\n'
_MODES = {'100644', '100755'}


class RejectedChange(Exception):
    """A supported, deterministic reason to reject one PR."""

    def __init__(self, code: str, message: str, observed: JsonValue, expected: JsonValue) -> None:
        """Record a stable rejection code and its observed and expected values."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.observed = observed
        self.expected = expected


class GitInfrastructureError(RuntimeError):
    """An operation failed without proving that the PR itself is invalid."""

    code = 'git_infrastructure_error'
    retryable = True


class GitAuthenticationError(GitInfrastructureError):
    """Host Git credentials were rejected independently of hosting API access."""

    code = 'git_auth_error'


class GitReferenceUnavailable(GitInfrastructureError):
    """The server does not advertise or deliver this particular ref/object."""

    code = 'git_reference_unavailable'


class GitTimeoutError(GitInfrastructureError):
    """An owned Git child exceeded its configured timeout."""


class GitStopRequested(GitInfrastructureError):
    """Leave this PR pending because shutdown was requested."""


class GitRepository:
    """Read a PR from an isolated bare Git cache without executing repository code."""

    def __init__(
        self,
        cache_dir: Path,
        clone_url: str,
        timeout_seconds: int,
        stop_event: threading.Event | None = None,
        *,
        initial_depth: int = 1,
        max_depth: int = 4096,
        max_transfer_bytes: int = 536870912,
    ) -> None:
        """Configure the cache, source URL, timeout, and cooperative stop signal."""
        self.path = Path(cache_dir)
        self.clone_url = clone_url
        self.timeout_seconds = timeout_seconds
        self.initial_depth = initial_depth
        self.max_depth = max_depth
        self.max_transfer_bytes = max_transfer_bytes
        self._received_bytes = 0
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._fetch_lock = threading.Lock()

    def _check_stop(self) -> None:
        """Leave the current PR pending when shutdown has been requested."""
        if self._stop_event.is_set():
            raise GitStopRequested('Git operation interrupted before next operation')

    @contextmanager
    def _cache_lock(self) -> Iterator[None]:
        """Serialize this cache across threads, instances, and processes."""
        self._check_stop()
        while not self._fetch_lock.acquire(timeout=0.1):
            self._check_stop()
        try:
            self._check_stop()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + '.lock')
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            try:
                while True:
                    self._check_stop()
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        self._stop_event.wait(0.1)
                try:
                    self._check_stop()
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            self._fetch_lock.release()

    def _run(self, *args: str, input_bytes: bytes | None = None) -> bytes:
        """Run one isolated Git command with a deadline and sanitized environment.

        Args:
            *args: Git subcommand and its arguments.
            input_bytes: Optional bytes written to standard input.

        Returns:
            Standard output from a successful Git command.

        Raises:
            GitStopRequested: If shutdown was requested before launch.
            GitTimeoutError: If the command exceeds its deadline.
            GitInfrastructureError: If launch or execution fails.

        """
        self._check_stop()
        env = isolated_environment()
        is_fetch = args[0] == 'fetch'
        if is_fetch:
            try:
                env = credential_environment(env, self.timeout_seconds, self._stop_event)
            except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired) as exc:
                self._check_stop()
                raise GitInfrastructureError(
                    'Cannot read host Git credentials; check the global credential helper/askpass.'
                ) from exc
        command = ['git', '--git-dir', str(self.path), '-c', 'core.hooksPath=/dev/null', *args]
        self._check_stop()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise GitInfrastructureError(f'cannot start Git: {exc.strerror}') from exc
        deadline = time.monotonic() + self.timeout_seconds
        high_water = self._object_bytes() if is_fetch else 0
        try:
            while True:
                self._check_stop()
                if is_fetch:
                    size = self._object_bytes()
                    self._received_bytes += max(0, size - high_water)
                    high_water = max(high_water, size)
                    if self._received_bytes > self.max_transfer_bytes:
                        raise RejectedChange(
                            'source_fetch_budget_exceeded',
                            'Git object transfer exceeded git.max_transfer_bytes; '
                            'increase the budget or select smaller changes.',
                            self._received_bytes,
                            self.max_transfer_bytes,
                        )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GitTimeoutError('Git timed out; check the network or increase git.timeout_seconds.')
                try:
                    stdout, stderr = process.communicate(input=input_bytes, timeout=min(0.1, remaining))
                    if is_fetch:
                        size = self._object_bytes()
                        self._received_bytes += max(0, size - high_water)
                        if self._received_bytes > self.max_transfer_bytes:
                            raise RejectedChange(
                                'source_fetch_budget_exceeded',
                                'Git object transfer exceeded git.max_transfer_bytes; '
                                'increase the budget or select smaller changes.',
                                self._received_bytes,
                                self.max_transfer_bytes,
                            )
                    break
                except subprocess.TimeoutExpired:
                    input_bytes = None
        except BaseException:
            self._terminate(process)
            raise
        if args[:2] == ('merge-base', '--all') and process.returncode == 1 and not stdout and not stderr:
            # Unrelated commits have no merge base; Git reports this with exit 1.
            return stdout
        if process.returncode:
            self._raise_command_error(args, stderr, is_fetch)
        return stdout

    def _raise_command_error(self, args: tuple[str, ...], stderr: bytes, is_fetch: bool) -> None:
        """Classify native failures without exposing raw Git diagnostics to the console."""
        detail = stderr.decode('utf-8', errors='replace').strip()
        if is_fetch:
            lower = detail.lower()
            if any(
                marker in lower
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
                raise GitAuthenticationError(
                    'Git access denied; configure the host credential helper/askpass '
                    'and check clone/fetch permissions. '
                    'Hosting API tokens do not configure Git.'
                )
            if any(
                marker in lower
                for marker in (
                    'not our ref',
                    'couldn\'t find remote ref',
                    'unadvertised object',
                    'does not allow request',
                    'no such ref',
                )
            ):
                raise GitReferenceUnavailable('The Git server no longer delivers the requested ref or object.')
            missing = _UNSHALLOW_MISSING.fullmatch(detail)
            depths = [value for value in args if re.fullmatch(r'--depth=[1-9][0-9]*', value)]
            if missing is not None and len(args) >= 4 and args[0] == 'fetch' and len(depths) == 1:
                reference = args[-1]
                try:
                    if _SHA.fullmatch(reference) is None:
                        SourceDescriptor.check_refs([reference])
                except ValueError:
                    pass
                else:
                    raise RejectedChange(
                        'source_history_unavailable',
                        'Git could not obtain the frozen shallow history for this change.',
                        {
                            'cause': 'git_unshallow_object_missing',
                            'operation': 'fetch',
                            'reference': reference,
                            'depth': int(depths[0].removeprefix('--depth=')),
                            'missing_commit': missing.group(1),
                        },
                        'retrievable frozen Git history',
                    )
            raise GitInfrastructureError(
                'Git fetch failed; check the fetch URL, host credentials, TLS and network, then resume.'
            )
        raise GitInfrastructureError('Git ' + args[0] + ' failed; check the local cache and available disk space.')

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """Reap the complete owned process group before reporting timeout/cancellation."""
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

    def _object_bytes(self) -> int:
        """Count packs, loose objects and temporary downloads for a conservative transfer cap."""
        directory = self.path / 'objects'
        if not directory.is_dir():
            return 0
        total = 0
        for parent, _directories, names in os.walk(directory):
            for name in names:
                try:
                    total += (Path(parent) / name).stat().st_size
                except FileNotFoundError:
                    # Git renames completed temporary packs while this sampler runs.
                    continue
        return total

    def prepare(self) -> None:
        """Create and verify the isolated bare cache under a cross-process lock."""
        with self._cache_lock():
            if not self.path.exists():
                self._run('init', '--bare', str(self.path))
            if self._run('rev-parse', '--is-bare-repository').strip() != b'true':
                raise GitInfrastructureError('Git cache is not a bare repository')

    def _object_type(self, name: str) -> str | None:
        """Distinguish a missing Git object from an operation failure."""
        result = self._run('cat-file', '--batch-check=%(objecttype)', input_bytes=f'{name}\n'.encode('ascii'))
        line = result.decode('ascii').strip()
        if line == f'{name} missing':
            return None
        if line not in {'commit', 'tree', 'blob', 'tag'}:
            raise GitInfrastructureError(f'unexpected Git object type: {line}')
        return line

    def has_commit(self, sha: str) -> bool:
        """Check whether a full SHA names an available commit object."""
        _require_sha(sha)
        return self._object_type(sha) == 'commit'

    def _fetch(self, reference: str, depth: int) -> None:
        """Fetch bounded object history without changing the frozen revision identity."""
        self._run('fetch', '--no-tags', '--no-recurse-submodules', '--depth=' + str(depth), self.clone_url, reference)

    def fetch_commit(self, sha: str, fetch_refs: list[str] | None = None) -> None:
        """Deliver an exact commit from a URL and neutral ref hints, never substitute a tip."""
        _require_sha(sha)
        refs = fetch_refs or []
        with self._cache_lock():
            if self.has_commit(sha):
                return
            try:
                self._fetch(sha, self.initial_depth)
            except GitReferenceUnavailable:
                pass
            if self.has_commit(sha):
                return
            depth = self.initial_depth
            while depth <= self.max_depth and refs:
                for reference in refs:
                    try:
                        self._fetch(reference, depth)
                    except GitReferenceUnavailable:
                        pass
                    if self.has_commit(sha):
                        return
                if depth == self.max_depth:
                    break
                depth = min(self.max_depth, depth * 2)
            raise RejectedChange(
                'source_revision_unavailable',
                'Frozen commit cannot be obtained within the configured fetch budget.',
                sha,
                'exact commit object',
            )

    def _complete_history(
        self, source: SourceRevisions, api_commits: list[str], fetch_refs: list[str]
    ) -> tuple[str, list[str]]:
        """Deepen until a complete source range matches the authoritative commit list."""
        depth = self.initial_depth
        while True:
            merge_bases = (
                self._run('merge-base', '--all', source.base_sha, source.head_sha).decode('ascii').splitlines()
            )
            if len(merge_bases) > 1:
                raise RejectedChange(
                    'unsupported_history',
                    'Frozen endpoints have multiple merge bases.',
                    [str(base) for base in merge_bases],
                    'one merge base',
                )
            if merge_bases:
                before = merge_bases[0]
                commits = (
                    self._run('rev-list', '--reverse', '--topo-order', before + '..' + source.head_sha)
                    .decode('ascii')
                    .splitlines()
                )
                if len(commits) == len(api_commits) and set(commits) == set(api_commits):
                    return before, commits
            if not (self.path / 'shallow').exists() or depth >= self.max_depth:
                if not merge_bases:
                    raise RejectedChange(
                        'missing_base',
                        'Frozen endpoints have no merge base within the history budget.',
                        [source.base_sha, source.head_sha],
                        'one merge base',
                    )
                raise RejectedChange(
                    'unsupported_history',
                    'Frozen Git range does not match the complete source commit list.',
                    [str(commit) for commit in api_commits],
                    'matching merge base and commit range',
                )
            depth = min(self.max_depth, depth * 2)
            with self._cache_lock():
                for sha in (source.base_sha, source.head_sha):
                    try:
                        self._fetch(sha, depth)
                    except GitReferenceUnavailable:
                        for reference in fetch_refs:
                            try:
                                self._fetch(reference, depth)
                            except GitReferenceUnavailable:
                                continue

    def verify_retrieval(self, source: SourceDescriptor, resolved: ResolvedRevisions) -> RetrievalReceipt:
        """Prove commit/tree availability from an empty cache using only exported inputs."""
        with tempfile.TemporaryDirectory(prefix='basket-retrieval-') as directory:
            cold = GitRepository(
                Path(directory) / 'objects',
                source.fetch_url,
                self.timeout_seconds,
                self._stop_event,
                initial_depth=self.initial_depth,
                max_depth=self.max_depth,
                max_transfer_bytes=self.max_transfer_bytes,
            )
            cold.prepare()
            for sha in (resolved.before, resolved.after):
                cold.fetch_commit(sha, source.fetch_refs)
            for revision, expected in [(resolved.before, resolved.before_tree), (resolved.after, resolved.after_tree)]:
                actual = cold._run('rev-parse', revision + '^{tree}').decode('ascii').strip()
                if actual != expected:
                    raise GitInfrastructureError('Cold-cache Git tree differs from the frozen source.')
                cold._run('ls-tree', '-r', revision)
            cold._run('fsck', '--full', '--strict', '--no-reflogs')
            return RetrievalReceipt(
                checked_at=utc_now(),
                source_sha256=source_hash(source),
                before=resolved.before,
                after=resolved.after,
                before_tree=resolved.before_tree,
                after_tree=resolved.after_tree,
            )

    def _commit(self, sha: str) -> tuple[list[str], str]:
        """Read a commit's parents and UTF-8 message from the saved object."""
        _require_sha(sha)
        if not self.has_commit(sha):
            raise RejectedChange('source_revision_unavailable', 'PR commit is unavailable', sha, 'commit object')
        raw = self._run('cat-file', 'commit', sha)
        header, separator, body = raw.partition(b'\n\n')
        if not separator:
            raise GitInfrastructureError(f'malformed commit object: {sha}')
        parents = [item[7:].decode('ascii') for item in header.splitlines() if item.startswith(b'parent ')]
        try:
            message = body.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise RejectedChange('non_utf8', 'commit message is not UTF-8', sha, 'UTF-8') from exc
        return parents, message

    def resolve_revisions(
        self, source: SourceRevisions, api_commits: list[str], fetch_refs: list[str] | None = None
    ) -> ResolvedRevisions:
        """Resolve the saved PR to one merge base and a verified commit range.

        The complete Git DAG must match the GitHub commit list. A target branch
        merged back into the PR branch is rejected, while matching side-branch
        merges are accepted.

        Args:
            source: Base and head SHAs captured during collection.
            api_commits: Complete commit SHAs reported by the hosting API.
            fetch_refs: Ordered full refs used only as object-delivery hints.

        Returns:
            Frozen before and after revisions plus the verified commit order.

        Raises:
            RejectedChange: If the revisions are missing, ambiguous, or unsupported.
            GitInfrastructureError: If Git cannot inspect the saved objects.

        """
        if len(api_commits) != source.commits_count or len(set(api_commits)) != len(api_commits):
            raise RejectedChange(
                'unsupported_history',
                'PR commit listing is incomplete or duplicated',
                len(api_commits),
                source.commits_count,
            )
        for sha in api_commits:
            _require_sha(sha)
        if source.head_sha not in api_commits:
            raise RejectedChange(
                'unsupported_history', 'saved head is absent from PR commits', source.head_sha, 'member of PR commits'
            )
        for sha in (source.base_sha, source.head_sha):
            if not self.has_commit(sha):
                raise RejectedChange(
                    'source_revision_unavailable', 'saved PR revision is unavailable', sha, 'commit object'
                )
        before, commits = self._complete_history(source, api_commits, fetch_refs or [])
        if source.expected_base_sha is not None and source.expected_base_sha != before:
            raise RejectedChange(
                'source_changed', 'API merge-base evidence differs from Git history.', before, source.expected_base_sha
            )
        for sha in (before, source.head_sha):
            if self._object_type(f'{sha}^{{tree}}') != 'tree':
                raise RejectedChange(
                    'source_revision_unavailable', 'required Git tree is unavailable', sha, 'commit tree'
                )
        for sha in commits:
            parents, _ = self._commit(sha)
            for side_parent in parents[1:]:
                common = self._run('merge-base', '--all', side_parent, source.base_sha).decode('ascii').splitlines()
                if common == [side_parent]:
                    raise RejectedChange(
                        'unsupported_history',
                        'target branch history was merged into the PR branch',
                        {'merge_commit': sha, 'merged_parent': side_parent},
                        'merged parent outside saved target branch history',
                    )
        return ResolvedRevisions(
            policy='change_merge_base_v1',
            before=before,
            after=source.head_sha,
            commits=commits,
            before_tree=self._run('rev-parse', before + '^{tree}').decode('ascii').strip(),
            after_tree=self._run('rev-parse', source.head_sha + '^{tree}').decode('ascii').strip(),
        )

    def read_changes(self, resolved: ResolvedRevisions) -> Changes:
        """Read the exact text patch and file statistics between frozen revisions.

        Renames are represented as deletion and addition. Binary content, LFS
        pointers, unsupported file modes, and malformed paths are rejected.

        Args:
            resolved: Verified before and after revisions and PR commits.

        Returns:
            Stable file metadata, UTF-8 patch, statistics, and commit messages.

        Raises:
            RejectedChange: If the change cannot be represented as supported text.
            GitInfrastructureError: If Git outputs inconsistent change metadata.

        """
        common = [
            '--no-ext-diff',
            '--no-textconv',
            '--no-renames',
            '--diff-algorithm=myers',
            '--no-color',
            '--no-relative',
            '--no-abbrev',
        ]
        raw = self._run('diff', *common, '--raw', '-z', resolved.before, resolved.after, '--')
        numstat = self._run('diff', *common, '--numstat', '-z', resolved.before, resolved.after, '--')
        patch_bytes = self._run(
            'diff', *common, '--full-index', '--unified=3', '--patch', resolved.before, resolved.after, '--'
        )
        entries = _raw_entries(raw)
        numbers = _numstat_entries(numstat)
        if set(numbers) != {path for path, *_ in entries}:
            raise GitInfrastructureError('Git raw and numstat paths disagree')
        if not entries:
            raise RejectedChange('empty_diff', 'PR has no changed files', 0, '> 0')
        files: list[ChangedFile] = []
        for path, status, before_mode, after_mode, before_blob, after_blob in entries:
            if status not in {'A', 'M', 'D'} or any(
                mode not in _MODES for mode in (before_mode, after_mode) if mode != '000000'
            ):
                raise RejectedChange(
                    'unsupported_file_type', 'changed path is not a regular file', path, 'regular file'
                )
            if before_blob == after_blob:
                raise RejectedChange('mode_only_change', 'path only changes mode', path, 'content change')
            additions, deletions = numbers[path]
            if additions is None or deletions is None:
                raise RejectedChange('binary_file', 'changed path is binary', path, 'UTF-8 text')
            for blob in (before_blob, after_blob):
                if blob == '0' * 40:
                    continue
                content = self._run('cat-file', 'blob', blob)
                if b'\x00' in content:
                    raise RejectedChange('binary_file', 'changed path contains NUL', path, 'UTF-8 text')
                try:
                    content.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise RejectedChange('non_utf8', 'changed path is not UTF-8', path, 'UTF-8 text') from exc
                if content.startswith(_LFS):
                    raise RejectedChange('lfs_file', 'changed path is an LFS pointer', path, 'ordinary text')
            files.append(
                ChangedFile(
                    path=path,
                    status=cast(
                        Literal['added', 'modified', 'deleted'], {'A': 'added', 'M': 'modified', 'D': 'deleted'}[status]
                    ),
                    additions=additions,
                    deletions=deletions,
                    before_blob=None if before_blob == '0' * 40 else before_blob,
                    after_blob=None if after_blob == '0' * 40 else after_blob,
                    before_mode=cast(
                        Literal['100644', '100755'] | None, None if before_mode == '000000' else before_mode
                    ),
                    after_mode=cast(Literal['100644', '100755'] | None, None if after_mode == '000000' else after_mode),
                )
            )
        try:
            patch = patch_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise RejectedChange('non_utf8', 'Git patch is not UTF-8', None, 'UTF-8 patch') from exc
        if not patch:
            raise RejectedChange('empty_diff', 'PR patch is empty', 0, '> 0')
        files.sort(key=lambda item: item.path)
        additions = sum(item.additions for item in files)
        deletions = sum(item.deletions for item in files)
        messages = [CommitMessage(sha=sha, message=self._commit(sha)[1]) for sha in resolved.commits]
        return Changes(
            files=files,
            stats=ChangeStats(
                files_count=len(files),
                additions=additions,
                deletions=deletions,
                changed_lines=additions + deletions,
                patch_chars=len(patch),
            ),
            diff=patch,
            commit_messages=messages,
        )


def _require_sha(value: str) -> None:
    """Reject values that are not full lowercase Git commit SHAs."""
    if _SHA.fullmatch(value) is None:
        raise ValueError('expected full lowercase Git SHA')


def _decode_path(raw: bytes) -> str:
    """Decode and validate a relative POSIX path from Git output."""
    try:
        path = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise RejectedChange('non_utf8', 'changed path is not UTF-8', None, 'UTF-8 path') from exc
    if not path or path.startswith('/') or '\\' in path or any(part in ('', '.', '..') for part in path.split('/')):
        raise RejectedChange('unsupported_file_type', 'changed path is not normalized', path, 'relative POSIX path')
    return path


def _raw_entries(raw: bytes) -> list[tuple[str, str, str, str, str, str]]:
    """Parse NUL-delimited raw Git diff entries without rename detection."""
    if not raw:
        return []
    chunks = raw.split(b'\x00')
    if chunks[-1] != b'':
        raise GitInfrastructureError('unterminated raw Git diff')
    result: list[tuple[str, str, str, str, str, str]] = []
    index = 0
    while index < len(chunks) - 1:
        header = chunks[index].split()
        if len(header) != 5 or not header[0].startswith(b':'):
            raise GitInfrastructureError('malformed raw Git diff')
        status = header[4].decode('ascii')
        if status not in {'A', 'M', 'D'}:
            raise RejectedChange('unsupported_file_type', 'unsupported Git change status', status, 'A, M or D')
        path = _decode_path(chunks[index + 1])
        result.append(
            (
                path,
                status,
                header[0][1:].decode('ascii'),
                header[1].decode('ascii'),
                header[2].decode('ascii'),
                header[3].decode('ascii'),
            )
        )
        index += 2
    if len({row[0] for row in result}) != len(result):
        raise GitInfrastructureError('duplicate path in raw Git diff')
    return result


def _numstat_entries(raw: bytes) -> dict[str, tuple[int | None, int | None]]:
    """Parse NUL-delimited Git line counts, preserving binary markers."""
    if not raw:
        return {}
    chunks = raw.split(b'\x00')
    if chunks[-1] != b'':
        raise GitInfrastructureError('unterminated numstat Git diff')
    result: dict[str, tuple[int | None, int | None]] = {}
    for chunk in chunks[:-1]:
        columns = chunk.split(b'\t', 2)
        if len(columns) != 3:
            raise GitInfrastructureError('malformed numstat Git diff')
        path = _decode_path(columns[2])
        if path in result:
            raise GitInfrastructureError('duplicate path in numstat Git diff')
        if columns[0] == columns[1] == b'-':
            result[path] = (None, None)
        else:
            try:
                result[path] = (int(columns[0]), int(columns[1]))
            except ValueError as exc:
                raise GitInfrastructureError('invalid numstat count') from exc
    return result
