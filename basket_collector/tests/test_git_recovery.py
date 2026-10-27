"""Reproduce native missing-history failures and bound recovery to the observed command contract."""

import subprocess
import zlib
from pathlib import Path

import pytest
from test_git import run, source

from basket_collector.models.sources import SourceDescriptor
from basket_collector.vcs.git import (
    GitInfrastructureError,
    GitReferenceUnavailable,
    GitRepository,
    GitStopRequested,
    GitTimeoutError,
    RejectedChange,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create owned native objects whose ancestors can be removed without touching a live cache."""
    path = tmp_path / 'source'
    path.mkdir()
    run(path, 'init', '-q', '--initial-branch=master')
    run(path, 'config', 'user.name', 'Test')
    run(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'first.py').write_text('one\ntwo\n', encoding='utf-8')
    run(path, 'add', '.')
    run(path, 'commit', '-qm', 'base')
    return path


def missing_ancestor_cache(repo: Path, tmp_path: Path) -> tuple[GitRepository, str, list[str]]:
    """Keep native endpoints in a ref-free cache while removing one owned loose ancestor."""
    base = run(repo, 'rev-parse', 'HEAD')
    commits = []
    for number in range(3):
        (repo / 'first.py').write_text(f'change {number}\n')
        run(repo, 'commit', '-qam', f'change {number}')
        commits.append(run(repo, 'rev-parse', 'HEAD'))
    cache = GitRepository(tmp_path / 'cache', repo.as_uri(), 10)
    cache.prepare()
    for sha in run(repo, 'rev-list', '--all').splitlines():
        raw = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'commit', sha])
        path = cache.path / 'objects' / sha[:2] / sha[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(f'commit {len(raw)}\0'.encode() + raw))
    missing = commits[0]
    (cache.path / 'objects' / missing[:2] / missing[2:]).unlink()
    native = subprocess.run(
        ['git', '--git-dir', str(cache.path), 'merge-base', '--all', base, commits[-1]],
        capture_output=True,
    )
    assert native.returncode == 1 and not native.stdout
    assert native.stderr.strip() == f'error: Could not read {missing}'.encode()
    assert cache.has_commit(base) and cache.has_commit(commits[-1])
    return cache, base, commits


def test_native_missing_ancestor_is_recovered_and_fully_verified(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One recovery followed by ordinary deepen preserves the original commit range and diff."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)
    fetched: list[tuple[str, int]] = []
    original = cache._fetch

    def fetch(reference: str, depth: int) -> None:
        """Observe native recovery without substituting the fetch implementation."""
        fetched.append((reference, depth))
        original(reference, depth)

    monkeypatch.setattr(cache, '_fetch', fetch)
    resolved = cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    assert (resolved.before, resolved.after, resolved.commits) == (base, commits[-1], commits)
    assert fetched.count((commits[0], cache.initial_depth)) == 1
    assert '+change 2' in cache.read_changes(resolved).diff
    warm_bytes = cache._received_bytes
    cache.max_transfer_bytes = warm_bytes + 1
    cold_bytes: list[int] = []

    def cold_fetch(cold: GitRepository, reference: str, depth: int) -> None:
        """Use the same full fixture remote while retaining cold's independent limit."""
        assert cold is not cache
        cold._run('fetch', '--no-tags', '--depth=' + str(depth), repo.as_uri(), reference)
        cold_bytes.append(cold._received_bytes)

    monkeypatch.setattr(GitRepository, '_fetch', cold_fetch)
    descriptor = SourceDescriptor(vcs='git', fetch_url='https://example.invalid/repo.git', fetch_refs=[])
    receipt = cache.verify_retrieval(descriptor, resolved)
    assert receipt.before == base and receipt.after == commits[-1]
    assert 0 < cold_bytes[-1] <= cache.max_transfer_bytes < warm_bytes + cold_bytes[-1]


def test_native_recovery_ref_failure_is_one_record_rejection(repo: Path, tmp_path: Path) -> None:
    """A real upload-pack not-our-ref reply does not stop unrelated records."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)
    empty_remote = tmp_path / 'empty-remote'
    empty_remote.mkdir()
    run(empty_remote, 'init', '--bare', '-q')
    cache.clone_url = empty_remote.as_uri()
    with pytest.raises(RejectedChange) as caught:
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    assert caught.value.code == 'source_history_unavailable'
    assert isinstance(caught.value.observed, dict)
    assert caught.value.observed['cause'] == 'git_merge_base_object_missing'
    assert caught.value.observed['missing_commit'] == commits[0]
    assert caught.value.observed['recovery_attempts'] == 1
    assert not cache._stop_event.is_set()


@pytest.mark.parametrize(
    'stderr,returncode,stdout,args',
    [
        (b'error: Could not read ' + b'a' * 40, 128, b'', ('merge-base', '--all', 'b' * 40, 'c' * 40)),
        (b'error: Could not read ' + b'a' * 40, 1, b'output', ('merge-base', '--all', 'b' * 40, 'c' * 40)),
        (b'error: Could not read invalid', 1, b'', ('merge-base', '--all', 'b' * 40, 'c' * 40)),
        (
            b'error: Could not read ' + b'a' * 40 + b'\nerror: Permission denied',
            1,
            b'',
            ('merge-base', '--all', 'b' * 40, 'c' * 40),
        ),
        (b'error: Could not read ' + b'a' * 40, 1, b'', ('rev-list', 'b' * 40, 'c' * 40)),
        (b'error: Could not read ' + b'a' * 40, 1, b'', ('merge-base', '--all', 'invalid', 'c' * 40)),
    ],
)
def test_missing_object_lookalikes_remain_infrastructure_errors(
    tmp_path: Path, stderr: bytes, returncode: int, stdout: bytes, args: tuple[str, ...]
) -> None:
    """Match the full native contract before creating recoverable history errors."""
    cache = GitRepository(tmp_path / 'cache', 'https://example.invalid/repo.git', 10)
    with pytest.raises(GitInfrastructureError) as caught:
        cache._raise_command_error(args, stderr, False, returncode=returncode, stdout=stdout)
    assert type(caught.value) is GitInfrastructureError


@pytest.mark.parametrize('different', [False, True])
def test_repeated_native_missing_ancestor_does_not_restart_recovery(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, different: bool
) -> None:
    """The second exact failure rejects only this record, even for a new missing SHA."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)
    calls: list[str] = []
    original = cache._run
    repeated = commits[1] if different else commits[0]

    def fetch(reference: str, _depth: int) -> None:
        """Deliver only the requested commit as a loose object to control the second fault."""
        calls.append(reference)
        raw = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'commit', reference])
        (cache.path / 'objects' / reference[:2] / reference[2:]).write_bytes(
            zlib.compress(f'commit {len(raw)}\0'.encode() + raw)
        )

    def run_with_second_missing(*args: str, input_bytes: bytes | None = None) -> bytes:
        """Remove an owned ancestor before the next real merge-base execution."""
        if args[:2] == ('merge-base', '--all') and calls:
            path = cache.path / 'objects' / repeated[:2] / repeated[2:]
            path.unlink(missing_ok=True)
        return original(*args, input_bytes=input_bytes)

    monkeypatch.setattr(cache, '_fetch', fetch)
    monkeypatch.setattr(cache, '_run', run_with_second_missing)
    with pytest.raises(RejectedChange) as caught:
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    assert calls == [commits[0]]
    assert isinstance(caught.value.observed, dict)
    assert caught.value.observed['repeated_missing_commit'] == repeated
    assert caught.value.observed['recovery_outcome'] == 'object_missing_after_recovery'


@pytest.mark.parametrize(
    'extra',
    [
        'HTTP 429',
        'HTTP 500',
        'Authentication failed',
        'TLS failure',
        'Permission denied',
        'No space left on device',
        'pack checksum mismatch',
        'unknown failure',
    ],
)
def test_mixed_recovery_ref_failure_remains_operation_error(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    """A substring match from generic ref fallbacks is never enough to reject recovery."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)
    calls: list[str] = []

    def fetch(reference: str, depth: int) -> None:
        """Supply the complete native message plus a forbidden infrastructure line."""
        calls.append(reference)
        stderr = (
            f'fatal: git upload-pack: not our ref {reference}\n'
            f'fatal: remote error: upload-pack: not our ref {reference}\n{extra}'
        ).encode()
        cache._raise_command_error(
            ('fetch', '--no-tags', '--no-recurse-submodules', f'--depth={depth}', cache.clone_url, reference),
            stderr,
            True,
            returncode=128,
        )

    monkeypatch.setattr(cache, '_fetch', fetch)
    with pytest.raises(GitInfrastructureError):
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    assert calls == [commits[0]]


@pytest.mark.parametrize('mode', ['wrong_exit', 'different_reference', 'stdout'])
def test_recovery_ref_evidence_requires_exit_reference_and_empty_stdout(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A ref refusal for another command outcome cannot justify rejecting this record."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)

    def fetch(reference: str, depth: int) -> None:
        """Alter one part of the observed native contract, keeping the rest exact."""
        reported = 'f' * 40 if mode == 'different_reference' else reference
        stderr = (
            f'fatal: git upload-pack: not our ref {reported}\n'
            f'fatal: remote error: upload-pack: not our ref {reported}'
        ).encode()
        cache._raise_command_error(
            ('fetch', '--no-tags', '--no-recurse-submodules', f'--depth={depth}', cache.clone_url, reference),
            stderr,
            True,
            returncode=1 if mode == 'wrong_exit' else 128,
            stdout=b'unexpected output' if mode == 'stdout' else b'',
        )

    monkeypatch.setattr(cache, '_fetch', fetch)
    with pytest.raises(GitReferenceUnavailable) as caught:
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    assert caught.value.confirmed_reference is None


@pytest.mark.parametrize('failure', [GitTimeoutError, GitStopRequested, OSError])
def test_recovery_infrastructure_failure_propagates(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """Timeout, shutdown and I/O faults propagate to the owning stage policy."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)

    def fetch(_reference: str, _depth: int) -> None:
        """Fail the single recovery attempt with a real infrastructure exception type."""
        raise failure('fixture recovery failed')

    monkeypatch.setattr(cache, '_fetch', fetch)
    with pytest.raises(failure):
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)


@pytest.mark.parametrize('kind', ['corrupt', 'unreadable', 'directory', 'pack'])
def test_existing_unreadable_history_object_does_not_trigger_recovery(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Native cat-file warnings must not turn a damaged object into deterministic absence."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)
    path = cache.path / 'objects' / commits[0][:2] / commits[0][2:]
    if kind == 'pack':
        directory = cache.path / 'objects' / 'pack'
        for suffix in ('.idx', '.pack'):
            (directory / ('pack-' + '0' * 40 + suffix)).write_bytes(b'corrupt pack index')
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'corrupt object')
        if kind == 'unreadable':
            path.chmod(0)

    def forbidden_fetch(_reference: str, _depth: int) -> None:
        """Ensure this infrastructure error never starts recovery."""
        pytest.fail('recovery started for an existing object')

    monkeypatch.setattr(cache, '_fetch', forbidden_fetch)
    try:
        with pytest.raises(GitInfrastructureError):
            cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    finally:
        if path.is_file():
            path.chmod(0o600)


def test_recovery_keeps_warm_budget_and_shutdown_between_commands(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One added fetch cannot reset prior traffic or launch merge-base after stop."""
    cache, base, commits = missing_ancestor_cache(repo, tmp_path)
    cache._received_bytes = cache.max_transfer_bytes
    with pytest.raises(RejectedChange) as caught:
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)
    assert caught.value.code == 'source_fetch_budget_exceeded'
    cache, base, commits = missing_ancestor_cache(repo, tmp_path / 'shutdown')
    original = cache._fetch

    def fetch_then_stop(reference: str, depth: int) -> None:
        """Deliver the ancestor, then request shutdown before the next subprocess."""
        original(reference, depth)
        cache._stop_event.set()

    monkeypatch.setattr(cache, '_fetch', fetch_then_stop)
    with pytest.raises(GitStopRequested):
        cache.resolve_revisions(source(base, commits[-1], len(commits)), commits)


@pytest.mark.parametrize('path', ['direct', 'fallback', 'history', 'history_fallback'])
def test_mixed_fetch_refusal_never_rejects_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """Mixed native stderr must reach record retries on every ordinary fetch path."""
    cache = GitRepository(tmp_path / 'cache', 'https://example.invalid/repo.git', 10)
    cache.prepare()
    (cache.path / 'shallow').write_text('a' * 40 + '\n')
    monkeypatch.setattr(cache, 'has_commit', lambda sha: False)
    monkeypatch.setattr(cache, '_run', lambda *args, **kwargs: b'')
    calls: list[str] = []

    def fetch(reference: str, depth: int) -> None:
        """Use the real classifier, allowing the first exact refusal only in fallback cases."""
        calls.append(reference)
        stderr = (
            f'fatal: git upload-pack: not our ref {reference}\n'
            f'fatal: remote error: upload-pack: not our ref {reference}'
        )
        if 'fallback' not in path or len(calls) > 1:
            stderr += '\nfatal: early EOF'
        cache._raise_command_error(
            ('fetch', '--no-tags', '--no-recurse-submodules', f'--depth={depth}', cache.clone_url, reference),
            stderr.encode(),
            True,
        )

    monkeypatch.setattr(cache, '_fetch', fetch)
    with pytest.raises(GitInfrastructureError):
        if path.startswith('history'):
            cache._complete_history(source('a' * 40, 'b' * 40, 1), ['b' * 40], ['refs/heads/main'])
        else:
            cache.fetch_commit('a' * 40, ['refs/heads/main'] if path == 'fallback' else [])
    assert len(calls) == (2 if 'fallback' in path else 1)
