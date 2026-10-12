"""Native Git source resolution keeps frozen identity and verifies a cold-cache reader."""

import subprocess
from pathlib import Path

import pytest
from basket_collector.contracts import SourceRevisions
from basket_collector.git import GitRepository
from basket_collector.sources import SourceDescriptor, source_hash


def git(directory: Path, *arguments: str) -> str:
    """Run one deterministic native fixture operation without network access."""
    return subprocess.check_output(['git', '-C', str(directory), *arguments], text=True).strip()


def test_native_exact_revisions_and_cold_retrieval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deepen shallow history and prove both frozen trees from declared source inputs."""
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    git(upstream, 'init', '-q', '--initial-branch=main')
    git(upstream, 'config', 'user.name', 'Fixture')
    git(upstream, 'config', 'user.email', 'fixture@example.invalid')
    (upstream / 'source.py').write_text('before\n')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-qm', 'before')
    before = git(upstream, 'rev-parse', 'HEAD')
    (upstream / 'source.py').write_text('after\n')
    git(upstream, 'commit', '-qam', 'after')
    after = git(upstream, 'rev-parse', 'HEAD')
    git(upstream, 'update-ref', 'refs/merge-requests/42/head', after)
    descriptor = SourceDescriptor(
        vcs='git',
        fetch_url='https://fixture.example.org/group/repo.git',
        fetch_refs=['refs/merge-requests/42/head', 'refs/heads/main'],
    )
    calls = []

    def fetch(repository: GitRepository, reference: str, depth: int) -> None:
        """Route the declared synthetic endpoint to an owned native local repository."""
        assert repository.clone_url == descriptor.fetch_url
        calls.append((str(repository.path), reference, depth))
        repository._run('fetch', '--no-tags', '--depth=' + str(depth), str(upstream), reference)

    monkeypatch.setattr(GitRepository, '_fetch', fetch)
    cache = GitRepository(tmp_path / 'cache', descriptor.fetch_url, 10, max_depth=8)
    cache.prepare()
    cache.fetch_commit(after, descriptor.fetch_refs)
    cache.fetch_commit(before, descriptor.fetch_refs)
    source = SourceRevisions(
        base_sha=before,
        head_sha=after,
        expected_base_sha=before,
        merge_commit_sha=None,
        squash_commit_sha=None,
        head_repository=None,
        commits_count=1,
        target_branch='main',
        diff_version_id=None,
    )
    resolved = cache.resolve_revisions(source, [after])
    assert resolved.before == before and resolved.after == after
    assert resolved.before_tree == git(upstream, 'rev-parse', before + '^{tree}')
    assert resolved.after_tree == git(upstream, 'rev-parse', after + '^{tree}')
    assert any(depth > 1 for _, _, depth in calls)
    receipt = cache.verify_retrieval(descriptor, resolved)
    assert receipt.source_sha256 == source_hash(descriptor)
    assert receipt.before_tree == resolved.before_tree and receipt.after_tree == resolved.after_tree
    cold_paths = {path for path, _, _ in calls if path != str(cache.path)}
    assert cold_paths and all(not Path(path).exists() for path in cold_paths)
    changes = cache.read_changes(resolved)
    assert changes.stats.files_count == 1
    assert changes.files[0].path == 'source.py'
