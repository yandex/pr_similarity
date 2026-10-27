"""Tests for Git revision, diff, cache, and shutdown behavior."""

import fcntl
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import Popen as PopenClass
from threading import Event, Lock
from typing import Callable, cast

import pytest

from basket_collector.config import FilterConfig
from basket_collector.filters import check_local
from basket_collector.models.artifacts import SourceRevisions
from basket_collector.vcs.git import (
    GitAuthenticationError,
    GitInfrastructureError,
    GitReferenceUnavailable,
    GitRepository,
    GitStopRequested,
    GitTimeoutError,
    RejectedChange,
)


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\n  '])
@pytest.mark.parametrize('reference', ['a' * 40, 'refs/merge-requests/216935/head'])
def test_unshallow_object_missing_is_record_rejection(tmp_path: Path, ending: str, reference: str) -> None:
    """Preserve only normalized native evidence for one bounded fetch failure."""
    cache = GitRepository(tmp_path / 'cache', 'https://example.invalid/repo.git', 10)
    missing = 'a' * 40
    args = ('fetch', '--no-tags', '--depth=2', cache.clone_url, reference)
    with pytest.raises(RejectedChange) as error:
        cache._raise_command_error(args, f'  fatal: object not found: unshallow {missing}{ending}'.encode(), True)
    assert error.value.code == 'source_history_unavailable'
    assert error.value.observed == {
        'cause': 'git_unshallow_object_missing',
        'operation': 'fetch',
        'reference': reference,
        'depth': 2,
        'missing_commit': missing,
    }
    assert error.value.expected == 'retrievable frozen Git history'
    assert cache._stop_event.is_set() is False


@pytest.mark.parametrize(
    'args,stderr,is_fetch,expected',
    [
        (
            ('cat-file', 'commit', 'a' * 40),
            'fatal: object not found: unshallow ' + 'a' * 40,
            False,
            GitInfrastructureError,
        ),
        (
            ('fetch', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: object not found: unshallow ' + 'a' * 40,
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=0', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: object not found: unshallow ' + 'a' * 40,
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'refs/heads/../secret'),
            'fatal: object not found: unshallow ' + 'a' * 40,
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'https://user:secret@host/repo'),
            'fatal: object not found: unshallow ' + 'a' * 40,
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: object not found: unshallow invalid',
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: object not found: ' + 'a' * 40,
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: object not found: unshallow ' + 'a' * 40 + '\nTLS SENSITIVE_BODY',
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: object not found: unshallow ' + 'a' * 40 + '\nAuthentication failed SENSITIVE_BODY',
            True,
            GitAuthenticationError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: no space left on device SENSITIVE_BODY',
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: TLS failure SENSITIVE_BODY',
            True,
            GitInfrastructureError,
        ),
        (
            ('fetch', '--depth=2', 'https://example.invalid/repo.git', 'a' * 40),
            'fatal: not our ref ' + 'a' * 40,
            True,
            GitReferenceUnavailable,
        ),
    ],
)
def test_unshallow_lookalikes_preserve_infrastructure_policy(
    tmp_path: Path,
    args: tuple[str, ...],
    stderr: str,
    is_fetch: bool,
    expected: type[GitInfrastructureError],
) -> None:
    """Keep malformed or mixed native failures out of per-record rejections."""
    cache = GitRepository(tmp_path / 'cache', 'https://example.invalid/repo.git', 10)
    with pytest.raises(expected) as error:
        cache._raise_command_error(args, stderr.encode(), is_fetch)
    assert 'SENSITIVE_BODY' not in str(error.value)
    assert 'user:secret' not in str(error.value)


def run(repo: Path, *args: str) -> str:
    """Run Git in a fixture repository and return trimmed stdout."""
    return subprocess.check_output(['git', '-C', str(repo), *args]).decode('utf-8').strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a minimal committed Git repository for each test."""
    path = tmp_path / 'source'
    path.mkdir()
    run(path, 'init', '-q', '--initial-branch=master')
    run(path, 'config', 'user.name', 'Test')
    run(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'first.py').write_text('one\ntwo\n', encoding='utf-8')
    run(path, 'add', '.')
    run(path, 'commit', '-qm', 'base')
    return path


def collector(repo: Path, tmp_path: Path) -> GitRepository:
    """Prepare an isolated Git cache for the fixture repository."""
    cache = GitRepository(tmp_path / 'cache', str(repo), 10, initial_depth=64)
    cache.prepare()
    return cache


def source(base: str, head: str, count: int) -> SourceRevisions:
    """Describe the saved PR base, head, and commit count."""
    return SourceRevisions(
        base_sha=base,
        head_sha=head,
        merge_commit_sha=None,
        head_repository=None,
        commits_count=count,
        expected_base_sha=None,
        squash_commit_sha=None,
        diff_version_id=None,
        target_branch='develop',
    )


def test_linear_shuffled_commits_and_exact_blob_stats(repo: Path, tmp_path: Path) -> None:
    """Normalize shuffled API commits and preserve exact diff statistics."""
    base = run(repo, 'rev-parse', 'HEAD')
    (repo / 'first.py').write_text('one\nthree\n', encoding='utf-8')
    run(repo, 'commit', '-qam', 'first change')
    first = run(repo, 'rev-parse', 'HEAD')
    (repo / 'folder').mkdir()
    (repo / 'folder' / 'space ü\nname.ts').write_text('new\n', encoding='utf-8')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'second change')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    resolved = cache.resolve_revisions(source(base, head, 2), [head, first])
    assert (resolved.before, resolved.after, resolved.commits) == (base, head, [first, head])
    changes = cache.read_changes(resolved)
    assert [item.path for item in changes.files] == ['first.py', 'folder/space ü\nname.ts']
    assert changes.stats.files_count == 2
    assert changes.stats.additions == 2
    assert changes.stats.deletions == 1
    assert changes.stats.patch_chars == len(changes.diff)
    assert [item.message.strip() for item in changes.commit_messages] == ['first change', 'second change']


def test_rename_is_delete_and_add(repo: Path, tmp_path: Path) -> None:
    """Represent a Git rename as a deletion and an addition."""
    base = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'mv', 'first.py', 'renamed.py')
    run(repo, 'commit', '-qm', 'rename')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    changes = cache.read_changes(cache.resolve_revisions(source(base, head, 1), [head]))
    assert [(item.path, item.status) for item in changes.files] == [('first.py', 'deleted'), ('renamed.py', 'added')]
    assert (changes.stats.additions, changes.stats.deletions) == (2, 2)


def test_diff_options_environment_does_not_change_patch_or_size_filter(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve three context lines and size decisions despite inherited diff options."""
    monkeypatch.delenv('GIT_DIFF_OPTS', raising=False)
    (repo / 'first.py').write_text('one\ntwo\nthree\nfour\nfive\nsix\nseven\n', encoding='utf-8')
    run(repo, 'commit', '-qam', 'context fixture')
    base = run(repo, 'rev-parse', 'HEAD')
    (repo / 'first.py').write_text('one\ntwo\nthree\nchanged\nfive\nsix\nseven\n', encoding='utf-8')
    run(repo, 'commit', '-qam', 'middle change')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    resolved = cache.resolve_revisions(source(base, head, 1), [head])
    baseline = cache.read_changes(resolved)
    config = FilterConfig.model_validate({'size': {'enabled': True, 'min_chars': baseline.stats.patch_chars}})
    assert check_local(baseline, config) is None
    assert '@@ -1,7 +1,7 @@' in baseline.diff

    monkeypatch.setenv('GIT_DIFF_OPTS', '--unified=0')
    inherited = cache.read_changes(resolved)
    assert inherited.diff == baseline.diff
    assert inherited.stats == baseline.stats
    assert check_local(inherited, config) is None


@pytest.mark.parametrize(
    'kind,expected',
    [
        ('binary', 'binary_file'),
        ('non_utf8', 'non_utf8'),
        ('lfs', 'lfs_file'),
        ('symlink', 'unsupported_file_type'),
        ('mode', 'mode_only_change'),
    ],
)
def test_unsupported_changed_content(repo: Path, tmp_path: Path, kind: str, expected: str) -> None:
    """Reject content and file types outside the basket contract."""
    base = run(repo, 'rev-parse', 'HEAD')
    target = repo / 'artifact'
    if kind == 'binary':
        target.write_bytes(b'hello\x00world')
    elif kind == 'non_utf8':
        target.write_bytes(b'hello\xff\n')
    elif kind == 'lfs':
        target.write_bytes(b'version https://git-lfs.github.com/spec/v1\noid sha256:' + b'0' * 64 + b'\nsize 2\n')
    elif kind == 'symlink':
        target.symlink_to('first.py')
    else:
        target = repo / 'first.py'
        target.chmod(0o755)
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', kind)
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    resolved = cache.resolve_revisions(source(base, head, 1), [head])
    with pytest.raises(RejectedChange) as error:
        cache.read_changes(resolved)
    assert error.value.code == expected


def test_merge_in_pr_dag_includes_side_changes_in_exact_diff(repo: Path, tmp_path: Path) -> None:
    """Include side-branch commits and changes from a PR merge DAG."""
    base = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-qb', 'side')
    (repo / 'side.py').write_text('side\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'side')
    side = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-q', 'master')
    (repo / 'main.py').write_text('main\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'main')
    main = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'merge', '-q', '--no-ff', 'side', '-m', 'merge')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    resolved = cache.resolve_revisions(source(base, head, 3), [head, side, main])
    assert (resolved.policy, resolved.before, resolved.after) == ('change_merge_base_v1', base, head)
    assert resolved.commits == run(repo, 'rev-list', '--reverse', '--topo-order', f'{base}..{head}').splitlines()
    changes = cache.read_changes(resolved)
    assert [item.path for item in changes.files] == ['main.py', 'side.py']
    assert set(changes.diff.splitlines()) >= {'+main', '+side'}
    assert [item.message.strip() for item in changes.commit_messages] == [
        run(repo, 'show', '-s', '--format=%B', sha) for sha in resolved.commits
    ]


def test_merge_from_saved_target_branch_history_is_unsupported(repo: Path, tmp_path: Path) -> None:
    """Reject a PR that merged the saved target branch into its history."""
    root = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-qb', 'change_request')
    (repo / 'pr.py').write_text('pr\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'PR change')
    pr_commit = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-q', 'master')
    (repo / 'target.py').write_text('target\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'target change')
    merged_target = run(repo, 'rev-parse', 'HEAD')
    (repo / 'later.py').write_text('later\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'later target change')
    saved_base = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-q', 'change_request')
    run(repo, 'merge', '-q', '--no-ff', merged_target, '-m', 'merge target into PR')
    head = run(repo, 'rev-parse', 'HEAD')
    assert run(repo, 'merge-base', saved_base, head) == merged_target
    assert root != merged_target
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    cache.fetch_commit(saved_base)
    with pytest.raises(RejectedChange) as error:
        cache.resolve_revisions(source(saved_base, head, 2), [head, pr_commit])
    assert error.value.code == 'unsupported_history'
    assert error.value.observed == {'merge_commit': head, 'merged_parent': merged_target}


def test_merge_of_unrelated_side_branch_is_accepted(repo: Path, tmp_path: Path) -> None:
    """Accept a merged side branch when the full PR DAG matches API commits."""
    base = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-qb', 'change_request')
    (repo / 'pr.py').write_text('pr\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'PR change')
    pr_commit = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '--orphan', 'unrelated_side')
    run(repo, 'rm', '-qrf', '.')
    (repo / 'side.py').write_text('side\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'unrelated side change')
    side = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-q', 'change_request')
    run(repo, 'merge', '-q', '--no-ff', '--allow-unrelated-histories', 'unrelated_side', '-m', 'merge unrelated side')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    resolved = cache.resolve_revisions(source(base, head, 3), [side, head, pr_commit])
    assert (resolved.before, resolved.after) == (base, head)
    assert set(resolved.commits) == {pr_commit, side, head}
    assert [item.path for item in cache.read_changes(resolved).files] == ['pr.py', 'side.py']


def test_incomplete_or_foreign_api_commits_are_rejected(repo: Path, tmp_path: Path) -> None:
    """Reject missing, duplicate, or foreign API commit lists."""
    base = run(repo, 'rev-parse', 'HEAD')
    (repo / 'first.py').write_text('first\n')
    run(repo, 'commit', '-qam', 'first')
    first = run(repo, 'rev-parse', 'HEAD')
    (repo / 'first.py').write_text('second\n')
    run(repo, 'commit', '-qam', 'second')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    for commits in ([head], [head, base], [head, head]):
        with pytest.raises(RejectedChange) as error:
            cache.resolve_revisions(source(base, head, 2), commits)
        assert error.value.code == 'unsupported_history'
    assert cache.resolve_revisions(source(base, head, 2), [head, first]).before == base


def test_unrelated_saved_base_and_head_have_no_merge_base(repo: Path, tmp_path: Path) -> None:
    """Reject histories without a common merge base."""
    base = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '--orphan', 'unrelated')
    run(repo, 'rm', '-qrf', '.')
    (repo / 'other.py').write_text('other\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'unrelated')
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    cache.fetch_commit(base)
    with pytest.raises(RejectedChange) as error:
        cache.resolve_revisions(source(base, head, 1), [head])
    assert error.value.code == 'missing_base'


def test_criss_cross_history_has_multiple_merge_bases(repo: Path, tmp_path: Path) -> None:
    """Reject histories with ambiguous merge-base candidates."""
    root = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-qb', 'left')
    (repo / 'left.py').write_text('left\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'left')
    left = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-qb', 'right', root)
    (repo / 'right.py').write_text('right\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'right')
    right = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-q', 'left')
    run(repo, 'merge', '-q', '--no-ff', right, '-m', 'left merge')
    base = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'checkout', '-q', 'right')
    run(repo, 'merge', '-q', '--no-ff', left, '-m', 'right merge')
    head = run(repo, 'rev-parse', 'HEAD')
    assert set(run(repo, 'merge-base', '--all', base, head).splitlines()) == {left, right}
    cache = collector(repo, tmp_path)
    cache.fetch_commit(base)
    cache.fetch_commit(head)
    with pytest.raises(RejectedChange) as error:
        cache.resolve_revisions(source(base, head, 1), [head])
    assert error.value.code == 'unsupported_history'


@pytest.mark.parametrize('two_instances', [False, True])
def test_parallel_fallback_fetch_keeps_each_fetch_head(repo: Path, tmp_path: Path, two_instances: bool) -> None:
    """Serialize fallback ref fetches so each commit resolves correctly."""
    (repo / 'first.py').write_text('one\nchanged\n')
    run(repo, 'commit', '-qam', 'first PR')
    first = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'update-ref', 'refs/pull/1/head', first)
    (repo / 'second.py').write_text('second\n')
    run(repo, 'add', '.')
    run(repo, 'commit', '-qm', 'second PR')
    second = run(repo, 'rev-parse', 'HEAD')
    run(repo, 'update-ref', 'refs/pull/2/head', second)
    first_fetched = Event()
    release_first = Event()
    second_fetched = Event()

    class FallbackCache(GitRepository):
        """Simulate a remote that requires fallback PR refs."""

        def __init__(self) -> None:
            """Configure the shared fixture cache for fallback fetches."""
            super().__init__(tmp_path / 'cache', str(repo), 10)

        def _run(self, *args: str, input_bytes: bytes | None = None) -> bytes:
            """Force fallback fetches and observe their serialized completion."""
            if args[0] == 'fetch' and args[-1] in {first, second}:
                raise GitReferenceUnavailable('server requires ref', confirmed_reference=args[-1])
            result = super()._run(*args, input_bytes=input_bytes)
            if args[0] == 'fetch' and args[-1] == 'refs/pull/1/head':
                first_fetched.set()
                assert release_first.wait(5)
            if args[0] == 'fetch' and args[-1] == 'refs/pull/2/head':
                second_fetched.set()
            return result

    cache = FallbackCache()
    cache.prepare()
    other_cache = FallbackCache() if two_instances else cache
    if two_instances:
        other_cache.prepare()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(cache.fetch_commit, first, ['refs/pull/1/head'])
        assert first_fetched.wait(5)
        second_future = pool.submit(other_cache.fetch_commit, second, ['refs/pull/2/head'])
        try:
            assert not second_fetched.wait(0.2)
        finally:
            release_first.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)
    assert cache.has_commit(first) and cache.has_commit(second)


def test_cat_file_timeout_is_stage_error_not_pr_rejection(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Propagate a Git cat-file timeout as a stage error."""
    head = run(repo, 'rev-parse', 'HEAD')
    cache = collector(repo, tmp_path)
    cache.fetch_commit(head)
    original = cache._run

    def timed_out(*args: str, input_bytes: bytes | None = None) -> bytes:
        """Raise a timeout only for Git cat-file commands."""
        if args[0] == 'cat-file':
            raise GitTimeoutError('Git timed out: cat-file')
        return original(*args, input_bytes=input_bytes)

    monkeypatch.setattr(cache, '_run', timed_out)
    with pytest.raises(GitTimeoutError):
        cache.has_commit(head)
    with pytest.raises(GitTimeoutError):
        cache._commit(head)
    with pytest.raises(GitTimeoutError):
        cache.fetch_commit(head)


@pytest.mark.parametrize('two_instances', [False, True])
def test_shutdown_interrupts_cache_lock_wait_without_starting_git(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    two_instances: bool,
) -> None:
    """Stop a waiter for either lock type before starting Git."""
    stop = Event()
    cache = GitRepository(tmp_path / 'cache', str(repo), 10, stop_event=stop)
    holder = GitRepository(tmp_path / 'cache', str(repo), 10) if two_instances else cache
    lock_attempted = Event()

    class ObservedLock:
        """Expose lock acquisition so the test can stop a waiting worker."""

        def __init__(self) -> None:
            """Create the observed in-process lock."""
            self.lock = Lock()

        def acquire(self, timeout: float) -> bool:
            """Signal a lock attempt before waiting for acquisition."""
            lock_attempted.set()
            return self.lock.acquire(timeout=timeout)

        def release(self) -> None:
            """Release the observed in-process lock."""
            self.lock.release()

    if not two_instances:
        monkeypatch.setattr(cache, '_fetch_lock', ObservedLock())

    def forbidden_popen(*args: object, **kwargs: object) -> None:
        """Fail if shutdown starts any Git subprocess."""
        raise AssertionError('Git subprocess started after shutdown')

    monkeypatch.setattr(subprocess, 'Popen', forbidden_popen)
    original_flock = fcntl.flock

    def observed_flock(fd: int, operation: int) -> None:
        """Signal a blocked file-lock attempt while preserving flock behavior."""
        try:
            original_flock(fd, operation)
        except BlockingIOError:
            lock_attempted.set()
            raise

    with ThreadPoolExecutor(max_workers=1) as pool:
        with holder._cache_lock():
            lock_attempted.clear()
            if two_instances:
                monkeypatch.setattr(fcntl, 'flock', observed_flock)
            future = pool.submit(cache.prepare)
            assert lock_attempted.wait(5)
            assert not future.done()
            stop.set()
            with pytest.raises(GitStopRequested):
                future.result(timeout=2)
    assert cache._fetch_lock.acquire(timeout=0.1)
    cache._fetch_lock.release()


def test_shutdown_after_inflight_git_prevents_next_subprocess(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow an active Git command to finish but prevent the next command."""
    stop = Event()
    cache = GitRepository(tmp_path / 'cache', str(repo), 10, stop_event=stop)
    original_run = cache._run
    original_popen = subprocess.Popen
    started: list[list[str]] = []

    def tracking_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        """Record Git subprocesses started during preparation."""
        command = args[0]
        assert isinstance(command, list)
        started.append(command)
        return cast(Callable[..., PopenClass[bytes]], original_popen)(*args, **kwargs)

    def stop_after_init(*args: str, input_bytes: bytes | None = None) -> bytes:
        """Request shutdown immediately after Git init completes."""
        result = original_run(*args, input_bytes=input_bytes)
        if args[0] == 'init':
            stop.set()
        return result

    monkeypatch.setattr(subprocess, 'Popen', tracking_popen)
    monkeypatch.setattr(cache, '_run', stop_after_init)
    with pytest.raises(GitStopRequested):
        cache.prepare()
    assert [command[5] for command in started] == ['init']


def test_shutdown_after_failed_sha_fetch_does_not_start_fallback(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip a fallback fetch after shutdown follows a failed SHA fetch."""
    stop = Event()
    cache = GitRepository(tmp_path / 'cache', str(repo), 10, stop_event=stop)
    cache.prepare()
    original_run = cache._run
    called: list[str] = []

    def failed_primary(*args: str, input_bytes: bytes | None = None) -> bytes:
        """Fail the primary fetch and request shutdown before fallback."""
        if args[0] != 'fetch':
            return original_run(*args, input_bytes=input_bytes)
        called.append(args[-1])
        stop.set()
        raise GitReferenceUnavailable('server requires ref', confirmed_reference=args[-1])

    monkeypatch.setattr(cache, '_run', failed_primary)
    with pytest.raises(GitStopRequested):
        cache.fetch_commit('0' * 40, ['refs/pull/1/head'])
    assert called == ['0' * 40]
