"""Real local Git histories test snapshot/capture without network or an agent."""

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from benchmark.errors import BenchmarkError
from benchmark.git import GitCache
from benchmark.storage import read_json, write_json
from benchmark.triplet_paths import triplet_path
from benchmark.triplets import load_triplets, save_triplets
from benchmark.workspace import capture_changes, prepare_workspace


def git(path: Path, *args: str) -> str:
    """Run fixture Git with isolated user configuration and deterministic author identity."""
    env = os.environ.copy()
    env.update(
        GIT_CONFIG_NOSYSTEM='1',
        GIT_CONFIG_GLOBAL='/dev/null',
        GIT_AUTHOR_NAME='Test',
        GIT_AUTHOR_EMAIL='test@example.invalid',
        GIT_COMMITTER_NAME='Test',
        GIT_COMMITTER_EMAIL='test@example.invalid',
    )
    return subprocess.check_output(['git', '-C', str(path), *args], env=env, stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[GitCache, str, str]:
    """Create before/reference Git revisions and a populated object cache."""
    repository = tmp_path / 'original'
    repository.mkdir()
    git(repository, 'init', '-q')
    (repository / 'src').mkdir()
    (repository / 'src/value.py').write_text('value = 1\n')
    (repository / '.gitignore').write_text('*.local\n')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'before')
    before = git(repository, 'rev-parse', 'HEAD')
    (repository / 'src/value.py').write_text('value = 2\n')
    (repository / 'src/empty.py').write_text('')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'reference')
    after = git(repository, 'rev-parse', 'HEAD')
    cache = GitCache(tmp_path / 'cache', 'https://github.com/owner/repo.git')
    cache.path.parent.mkdir(parents=True)
    subprocess.run(['git', 'clone', '--bare', '-q', str(repository), str(cache.path)], check=True)
    return cache, before, after


def test_source_contains_only_before_with_fresh_root(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify source contains only before with fresh root."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', 'src')
    assert snapshot.cwd == snapshot.root / 'src'
    assert (snapshot.cwd / 'value.py').read_text() == 'value = 1\n'
    assert not (snapshot.cwd / 'empty.py').exists()
    assert git(snapshot.root, 'rev-list', '--count', 'HEAD') == '1'
    assert git(snapshot.root, 'remote') == ''
    assert (
        subprocess.run(
            ['git', '-C', str(snapshot.root), 'cat-file', '-e', after], capture_output=True, check=False
        ).returncode
        != 0
    )


def test_new_cwd_does_not_copy_reference(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify new cwd does not copy reference."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', 'new/deep')
    assert snapshot.cwd.is_dir()
    assert list(snapshot.cwd.iterdir()) == []


def test_capture_tracked_untracked_empty_and_ignore(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify capture tracked untracked empty and ignore."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', 'src')
    (snapshot.cwd / 'value.py').write_text('value = 2\n')
    (snapshot.cwd / 'new.py').write_text('new = True\n')
    (snapshot.cwd / 'empty.py').write_text('')
    (snapshot.cwd / 'secret.local').write_text('must not be captured\n')
    result = capture_changes(snapshot, [])
    assert set(result.states) == {'src/value.py', 'src/new.py', 'src/empty.py'}
    assert result.states['src/empty.py'].exists
    assert b'new file mode' in result.diff and b'empty.py' in result.diff
    assert b'secret.local' not in result.diff


def test_capture_uses_baseline_after_agent_commits(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify capture uses baseline after agent commits."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    (snapshot.root / 'src/value.py').write_text('value = 3\n')
    git(snapshot.root, 'add', '.')
    git(snapshot.root, 'commit', '-qm', 'agent must not commit')
    assert set(capture_changes(snapshot, []).states) == {'src/value.py'}


def test_excluded_reference_stays_missed(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify excluded reference stays missed."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', 'src')
    (snapshot.cwd / 'value.py').write_text('value = 2\n')
    captured = capture_changes(snapshot, ['value.py'])
    assert not captured.states
    assert captured.excluded == {'src/value.py': ['value.py']}
    index = save_triplets(tmp_path / 'triplets', cache, before, after, captured, 'owner/repo#1', 'attempt')
    triplets = load_triplets(tmp_path / 'triplets')
    assert index['entries'][1]['path'] == 'src/value.py'
    assert triplets['src/value.py']['human'].text == 'value = 2\n'
    assert triplets['src/value.py']['ai'].text == 'value = 1\n'
    assert not triplets['src/empty.py']['base'].exists
    assert triplets['src/empty.py']['human'].exists and triplets['src/empty.py']['human'].text == ''


@pytest.mark.parametrize('content', [b'\x00binary', b'\xffnot-utf8'])
def test_unsupported_changes_fail_after_exclusions(
    history: tuple[GitCache, str, str], tmp_path: Path, content: bytes
) -> None:
    """Verify unsupported changes fail after exclusions."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    (snapshot.root / 'bad.py').write_bytes(content)
    with pytest.raises(BenchmarkError) as caught:
        capture_changes(snapshot, [])
    assert caught.value.code == 'unsupported_change'
    assert caught.value.paths == ['bad.py']
    assert caught.value.files_count == 1
    assert caught.value.captured_diff is not None
    assert b'bad.py' in caught.value.captured_diff
    assert not capture_changes(snapshot, ['bad.py']).states


def test_symlink_not_followed(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify symlink not followed."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    outside = tmp_path / 'outside'
    outside.write_text('private')
    (snapshot.root / 'linked.py').symlink_to(outside)
    with pytest.raises(BenchmarkError) as caught:
        capture_changes(snapshot, [])
    assert caught.value.code == 'unsupported_change'
    assert not capture_changes(snapshot, ['linked.py']).states


def test_symlink_parent_escape_rejected(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify symlink parent escape rejected."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    (snapshot.root / 'src/value.py').unlink()
    (snapshot.root / 'src').rmdir()
    (snapshot.root / 'src').symlink_to(tmp_path)
    with pytest.raises(BenchmarkError):
        capture_changes(snapshot, [])


def test_artifact_corruption_is_not_deletion(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify artifact corruption is not deletion."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    directory = tmp_path / 'triplets'
    index = save_triplets(directory, cache, before, after, capture_changes(snapshot, []), 'owner/repo#1', 'attempt')
    entry = next(item for item in index['entries'] if item['path'] == 'src/empty.py')
    (directory / entry['files']['human']['path']).write_text('corrupted')
    with pytest.raises(BenchmarkError):
        load_triplets(directory)


def test_deletion_rename_and_paths_with_spaces(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify deletion rename and paths with spaces."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    (snapshot.root / 'src/value.py').rename(snapshot.root / 'src/renamed file.py')
    result = capture_changes(snapshot, [])
    assert set(result.states) == {'src/value.py', 'src/renamed file.py'}
    assert not result.states['src/value.py'].exists
    assert result.states['src/renamed file.py'].text == 'value = 1\n'
    assert b'renamed file.py' in result.diff and b'rename from' not in result.diff


def test_independent_attempts_and_git_env_override(
    history: tuple[GitCache, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify independent attempts and git env override."""
    cache, before, _after = history
    one = prepare_workspace(cache, before, tmp_path / 'first/source', 'src')
    two = prepare_workspace(cache, before, tmp_path / 'second/source', 'src')
    (one.cwd / 'value.py').write_text('value = 5\n')
    monkeypatch.setenv('GIT_DIFF_OPTS', '--stat')
    monkeypatch.setenv('GIT_EXTERNAL_DIFF', 'does-not-exist')
    assert b'value = 5' in capture_changes(one, []).diff
    assert not capture_changes(two, []).diff


def test_lfs_snapshot_is_rejected(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify lfs snapshot is rejected."""
    cache, _before, _after = history
    repository = tmp_path / 'lfs'
    repository.mkdir()
    git(repository, 'init', '-q')
    (repository / 'data').write_text('version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1\n')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'lfs')
    revision = git(repository, 'rev-parse', 'HEAD')
    subprocess.run(['git', '--git-dir', str(cache.path), 'fetch', '-q', str(repository), revision], check=True)
    with pytest.raises(BenchmarkError) as caught:
        prepare_workspace(cache, revision, tmp_path / 'attempt/source', '.')
    assert caught.value.code == 'unsupported_snapshot'


def test_missing_exists_cannot_be_reconstructed(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify missing exists cannot be reconstructed."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    directory = tmp_path / 'triplets'
    save_triplets(directory, cache, before, after, capture_changes(snapshot, []), 'owner/repo#1', 'attempt')
    index = read_json(directory / 'index.json')
    del index['entries'][0]['exists']
    write_json(directory / 'index.json', index)
    with pytest.raises(BenchmarkError):
        load_triplets(directory)


def test_gitlink_snapshot_is_rejected(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify gitlink snapshot is rejected."""
    cache, before, _after = history
    repository = tmp_path / 'gitlink'
    repository.mkdir()
    git(repository, 'init', '-q')
    git(repository, 'update-index', '--add', '--cacheinfo', f'160000,{before},submodule')
    git(repository, 'commit', '-qm', 'submodule')
    revision = git(repository, 'rev-parse', 'HEAD')
    subprocess.run(['git', '--git-dir', str(cache.path), 'fetch', '-q', str(repository), revision], check=True)
    with pytest.raises(BenchmarkError) as caught:
        prepare_workspace(cache, revision, tmp_path / 'attempt/source', '.')
    assert caught.value.code == 'unsupported_snapshot'


def test_unsafe_before_symlink_is_rejected(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify unsafe before symlink is rejected."""
    cache, _before, _after = history
    repository = tmp_path / 'unsafe'
    repository.mkdir()
    git(repository, 'init', '-q')
    (repository / 'escape').symlink_to('/etc')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'unsafe link')
    revision = git(repository, 'rev-parse', 'HEAD')
    subprocess.run(['git', '--git-dir', str(cache.path), 'fetch', '-q', str(repository), revision], check=True)
    with pytest.raises(BenchmarkError) as caught:
        prepare_workspace(cache, revision, tmp_path / 'attempt/source', '.')
    assert caught.value.code == 'unsafe_snapshot'


def test_cache_ensure_reuses_exact_objects_without_network(history: tuple[GitCache, str, str]) -> None:
    """Verify cache ensure reuses exact objects without network."""
    cache, before, after = history

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cache.ensure, [before, after]) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    assert set(cache.tree(before)) == {'.gitignore', 'src/value.py'}


def test_cache_cancellation_preserves_interrupt(history: tuple[GitCache, str, str]) -> None:
    """Verify cache cancellation preserves interrupt."""
    cache, before, _after = history

    stop = threading.Event()
    stop.set()
    cache.stop = stop
    with pytest.raises(BenchmarkError) as caught:
        cache.ensure([before])
    assert caught.value.code == 'operation_cancelled'


def test_reference_receipts_are_verified(history: tuple[GitCache, str, str]) -> None:
    """Verify reference receipts are verified."""
    cache, before, after = history
    trees = {'before': cache.tree(before), 'after': cache.tree(after)}
    changes = []
    for path in sorted(trees['before'].keys() | trees['after'].keys()):
        if trees['before'].get(path) == trees['after'].get(path):
            continue
        item: dict[str, str | None] = {'path': path}
        for role, tree in trees.items():
            entry = tree.get(path)
            item[role + '_mode'] = None if entry is None else entry.mode
            item[role + '_blob'] = None if entry is None else entry.oid
        changes.append(item)
    row = {
        'revisions': {
            'before': before,
            'after': after,
            'before_tree': cache.run('rev-parse', before + '^{tree}').decode().strip(),
            'after_tree': cache.run('rev-parse', after + '^{tree}').decode().strip(),
        },
        'changes': {'files': changes},
    }
    cache.verify_reference(row)
    changes[0]['after_blob'] = 'f' * 40
    with pytest.raises(BenchmarkError) as caught:
        cache.verify_reference(row)
    assert caught.value.code == 'reference_mismatch'


def test_nested_git_directory_can_be_explicitly_excluded(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """Verify nested git directory can be explicitly excluded."""
    cache, before, _after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'attempt/source', '.')
    vendor = snapshot.root / 'vendor'
    vendor.mkdir()
    git(vendor, 'init', '-q')
    (vendor / 'dep.py').write_text('dependency = True\n')
    git(vendor, 'add', '.')
    git(vendor, 'commit', '-qm', 'dependency')
    with pytest.raises(BenchmarkError) as caught:
        capture_changes(snapshot, [])
    assert caught.value.code == 'unsupported_change'
    excluded = capture_changes(snapshot, ['vendor/'])
    assert excluded.states == {} and excluded.excluded == {'vendor': ['vendor/']}


def test_triplets_keep_adjacent_variants_and_distinguish_absent_empty(
    history: tuple[GitCache, str, str], tmp_path: Path
) -> None:
    """An editor can find original paths without treating absence as an empty file."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'source', '.')
    directory = tmp_path / 'triplets'
    index = save_triplets(
        directory,
        cache,
        before,
        after,
        capture_changes(snapshot, []),
        'task',
        'attempt',
        run_id='11111111-1111-4111-8111-111111111111',
    )
    entry = next(item for item in index['entries'] if item['path'] == 'src/value.py')
    base = directory / entry['files']['base']['path']
    human = directory / entry['files']['human']['path']
    agent = directory / entry['files']['ai']['path']
    assert base.parent == human.parent == agent.parent
    assert base.name == 'value_base.py' and human.name == 'value_human.py' and agent.name == 'value_agent.py'
    assert base.read_text() == 'value = 1\n' and human.read_text() == 'value = 2\n'
    empty = next(entry for entry in index['entries'] if entry['path'] == 'src/empty.py')
    assert (
        empty['files']['base'] is None
        and not (directory / triplet_path(empty['number'], 'src/empty.py', 'base')).exists()
    )
    assert empty['exists']['human'] and (directory / empty['files']['human']['path']).read_bytes() == b''
    assert index['run_id'] == '11111111-1111-4111-8111-111111111111'


def test_capture_and_triplets_support_file_becoming_directory(
    history: tuple[GitCache, str, str], tmp_path: Path
) -> None:
    """Separate triplet namespaces preserve a deleted file and a new child without path collisions."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'source', '.')
    file = snapshot.root / 'src/value.py'
    file.unlink()
    file.mkdir()
    (file / 'nested.py').write_text('value = 3\n')
    captured = capture_changes(snapshot, [])
    assert not captured.states['src/value.py'].exists
    directory = tmp_path / 'triplets'
    index = save_triplets(directory, cache, before, after, captured, 'task', 'attempt')
    states = load_triplets(directory)
    assert states['src/value.py']['base'].exists and not states['src/value.py']['ai'].exists
    assert not states['src/value.py/nested.py']['base'].exists
    child = next(entry for entry in index['entries'] if entry['path'] == 'src/value.py/nested.py')
    assert (directory / child['files']['ai']['path']).read_text() == 'value = 3\n'


def test_absent_triplet_rejects_fabricated_source_bytes(history: tuple[GitCache, str, str], tmp_path: Path) -> None:
    """An absent base state cannot silently hide a file beside its null reference."""
    cache, before, after = history
    snapshot = prepare_workspace(cache, before, tmp_path / 'source', '.')
    captured = capture_changes(snapshot, [])
    directory = tmp_path / 'triplets'
    index = save_triplets(directory, cache, before, after, captured, 'task', 'attempt')
    entry = next(item for item in index['entries'] if item['path'] == 'src/empty.py')
    target = directory / triplet_path(entry['number'], entry['path'], 'base')
    assert not target.exists()
    target.write_bytes(b'')
    with pytest.raises(BenchmarkError) as caught:
        load_triplets(directory)
    assert caught.value.code == 'invalid_triplets'
