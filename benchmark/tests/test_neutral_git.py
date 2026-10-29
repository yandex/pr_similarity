"""Frozen source inputs drive the independent native Git adapter without hosting APIs."""

import re
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from test_basket import payload

from benchmark.errors import BenchmarkError
from benchmark.metrics.scoring import score_pr
from benchmark.models.basket import BasketRecord
from benchmark.persistence import storage
from benchmark.persistence.storage import sha256, write_json
from benchmark.persistence.triplets import load_triplets, save_triplets
from benchmark.validation.basket import load_basket
from benchmark.vcs import git as module
from benchmark.vcs.git import GitCache, git_environment
from benchmark.vcs.workspace import capture_changes, prepare_workspace


def test_concurrent_ensure_waits_beyond_git_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A delayed repository queue succeeds once, without spending a Git-command deadline."""
    source, row = frozen_source(tmp_path, 'github')
    route_objects(monkeypatch, source, row.source.fetch_url, require_hints=False)
    stop, blocked, release = Event(), Event(), Event()
    caches = [GitCache(tmp_path / 'cache', row.source.fetch_url, timeout=10, stop=stop) for _ in range(3)]
    native = storage.fcntl.flock
    clock = [0.0]

    def flock(fd: int, operation: int) -> None:
        """Advance the clock past the Git timeout while holding contending callers."""
        try:
            native(fd, operation)
        except BlockingIOError:
            clock[0] = 1000.0
            blocked.set()
            assert release.wait(5)
            raise

    monkeypatch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep))
    monkeypatch.setattr(storage.fcntl, 'flock', flock)

    def ensure(cache: GitCache) -> None:
        """Fetch and verify the frozen revisions through the shared repository cache."""
        cache.ensure([row.revisions.before, row.revisions.after], fetch_refs=row.source.fetch_refs)
        cache.verify_reference(row)

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            with caches[0].lock():
                futures = [pool.submit(ensure, cache) for cache in caches[1:]]
                assert blocked.wait(5)
                assert all(not future.done() for future in futures)
            release.set()
            for future in futures:
                future.result(timeout=10)
        finally:
            stop.set()
            release.set()


def git(directory: Path, *arguments: str) -> str:
    """Create and inspect only the owned native fixture repository."""
    return subprocess.check_output(['git', '-C', str(directory), *arguments], env=git_environment()).decode().strip()


def frozen_source(tmp_path: Path, provider: str) -> tuple[Path, BasketRecord]:
    """Freeze an actual before/head pair and a newer tip that must never replace it."""
    repository = tmp_path / 'upstream'
    repository.mkdir()
    git(repository, 'init', '-q', '--initial-branch=main')
    (repository / 'src').mkdir()
    (repository / 'src/value.py').write_text('value = 1\n', encoding='utf-8')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'before')
    before = git(repository, 'rev-parse', 'HEAD')
    (repository / 'src/value.py').write_text('value = 2\n', encoding='utf-8')
    (repository / 'src/empty.py').write_text('', encoding='utf-8')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'reference')
    after = git(repository, 'rev-parse', 'HEAD')
    (repository / 'src/value.py').write_text('value = 3\n', encoding='utf-8')
    git(repository, 'commit', '-qam', 'newer unrelated tip')
    document = payload(provider)
    row = document['tasks'][0]
    row['workspace'] = 'src'
    row['source']['fetch_refs'] = ['refs/heads/deleted-hint', 'refs/heads/main']
    row['revisions'].update(
        before=before,
        after=after,
        before_tree=git(repository, 'rev-parse', before + '^{tree}'),
        after_tree=git(repository, 'rev-parse', after + '^{tree}'),
    )
    evidence = row['provenance']['source_revisions']
    evidence.update(base_sha=before, head_sha=after, expected_base_sha=before if provider == 'gitlab' else None)
    row['provenance']['retrieval'].update(
        **{key: row['revisions'][key] for key in ('before', 'after', 'before_tree', 'after_tree')},
        source_sha256=sha256(row['source']),
    )
    entries = []
    for name in ('src/empty.py', 'src/value.py'):
        added = name.endswith('empty.py')
        entries.append(
            {
                'path': name,
                'status': 'added' if added else 'modified',
                'additions': 0 if added else 1,
                'deletions': 0 if added else 1,
                'before_blob': None if added else git(repository, 'rev-parse', before + ':' + name),
                'after_blob': git(repository, 'rev-parse', after + ':' + name),
                'before_mode': None if added else '100644',
                'after_mode': '100644',
            }
        )
    row['changes'] = {
        'files': entries,
        'stats': {'files_count': 2, 'additions': 1, 'deletions': 1, 'changed_lines': 2, 'patch_chars': 0},
    }
    path = tmp_path / 'basket.json'
    write_json(path, document)
    return repository, load_basket(path).rows[0]


def route_objects(
    monkeypatch: pytest.MonkeyPatch, source: Path, fetch_url: str, *, require_hints: bool = True
) -> list[tuple[str, int]]:
    """Keep the production timeout/budget/credential path while replacing transport only."""
    native = module.run_git
    calls: list[tuple[str, int]] = []

    def local(
        arguments: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 120,
        stop: Event | None = None,
        accepted: tuple[int, ...] = (0,),
        input_bytes: bytes | None = None,
        host_fetch: bool = False,
        monitor: Callable[[], None] | None = None,
    ) -> bytes:
        """Deliver real packs locally and simulate a server that does not serve raw SHA wants."""
        if 'fetch' in arguments:
            assert fetch_url in arguments
            reference = arguments[-1]
            depth = int(next(item.split('=', 1)[1] for item in arguments if item.startswith('--depth=')))
            calls.append((reference, depth))
            if require_hints and re.fullmatch('[0-9a-f]{40}', reference):
                raise BenchmarkError('git_reference_unavailable', 'Fixture requires declared ref hints.', exit_code=1)
            arguments = [str(source) if item == fetch_url else item for item in arguments]
        return native(
            arguments,
            cwd=cwd,
            timeout=timeout,
            stop=stop,
            accepted=accepted,
            input_bytes=input_bytes,
            host_fetch=host_fetch,
            monitor=monitor,
        )

    monkeypatch.setattr(module, 'run_git', local)
    return calls


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_exact_historical_source_workspace_diff_and_triplets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Deepen declared hints while keeping a newer ref tip outside the agent snapshot."""
    source, row = frozen_source(tmp_path, provider)
    calls = route_objects(monkeypatch, source, row.source.fetch_url)
    cache = GitCache(tmp_path / 'cache', row.source.fetch_url, max_depth=8)
    cache.ensure([row.revisions.before, row.revisions.after], fetch_refs=row.source.fetch_refs)
    cache.verify_reference(row)
    assert any(depth > 1 for _reference, depth in calls)
    assert calls[0][0] == row.revisions.before
    workspace = prepare_workspace(cache, row.revisions.before, tmp_path / 'workspace', row.workspace)
    assert (workspace.cwd / 'value.py').read_text() == 'value = 1\n'
    assert not (workspace.cwd / 'empty.py').exists()
    assert git(workspace.root, 'rev-list', '--count', 'HEAD') == '1'
    assert git(workspace.root, 'remote') == ''
    (workspace.cwd / 'value.py').write_text('value = 2\n', encoding='utf-8')
    (workspace.cwd / 'empty.py').write_text('', encoding='utf-8')
    captured = capture_changes(workspace, [])
    assert set(captured.states) == {'src/value.py', 'src/empty.py'}
    directory = tmp_path / 'triplets'
    save_triplets(directory, cache, row.revisions.before, row.revisions.after, captured, row.id, 'fixture-attempt')
    triplets = load_triplets(directory)
    assert triplets['src/value.py'].human.text == 'value = 2\n'
    assert not triplets['src/empty.py'].base.exists and triplets['src/empty.py'].human.exists
    scored = score_pr(triplets)
    assert scored.counts is not None
    assert scored.counts.matched_files == 2
    assert scored.counts.missed_files == scored.counts.extra_files == 0


def test_unavailable_object_never_becomes_current_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ref delivery failure remains infrastructure failure instead of changing the reference."""
    source, row = frozen_source(tmp_path, 'gitlab')
    route_objects(monkeypatch, source, row.source.fetch_url)
    cache = GitCache(tmp_path / 'cache', row.source.fetch_url, max_depth=4)
    with pytest.raises(BenchmarkError) as caught:
        cache.ensure(['f' * 40], fetch_refs=row.source.fetch_refs)
    assert caught.value.code == 'source_revision_unavailable'


def test_real_object_growth_has_a_finite_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The production monitor rejects an actual pack before workspace preparation."""
    source, row = frozen_source(tmp_path, 'github')
    route_objects(monkeypatch, source, row.source.fetch_url, require_hints=False)
    cache = GitCache(tmp_path / 'cache', row.source.fetch_url, max_transfer_bytes=1)
    with pytest.raises(BenchmarkError) as caught:
        cache.ensure([row.revisions.before], fetch_refs=row.source.fetch_refs)
    assert caught.value.code == 'git_fetch_budget_exceeded'


def test_case_sensitive_fetch_paths_have_separate_caches(tmp_path: Path) -> None:
    """Self-hosted subgroup casing cannot be collapsed by a GitHub-specific cache key."""
    upper = GitCache(tmp_path, 'https://git.example/Group/Sub/project.git')
    lower = GitCache(tmp_path, 'https://git.example/group/Sub/project.git')
    assert upper.path != lower.path
