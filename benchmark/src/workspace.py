"""Fresh before-only workspaces and Git diff plus nonignored untracked files."""

import stat
from dataclasses import dataclass
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.excludes import compile_exclude, resolve_exclude
from benchmark.git import run_git, safe_path
from benchmark.vcs import VCS

_REGULAR_MODES = frozenset({'100644', '100755'})
_LFS = b'version https://git-lfs.github.com/spec/v1\n'


@dataclass(frozen=True)
class FileState:
    """Absence is distinct from an existing file whose text is empty."""

    exists: bool
    text: str
    mode: str | None


@dataclass(frozen=True)
class Workspace:
    """Describe a fresh before tree and its exported working directory."""

    root: Path
    cwd: Path
    relative_cwd: str
    baseline: str
    cache: VCS
    before: str


@dataclass(frozen=True)
class CapturedChanges:
    """Pair raw Git diff with selected safe file states and exclusions."""

    states: dict[str, FileState]
    diff: bytes
    excluded: dict[str, list[str]]


def _decode(data: bytes) -> str:
    """Decode supported source text without treating binary data as absence."""
    if b'\0' in data or data.startswith(_LFS):
        raise BenchmarkError(
            'unsupported_change', 'A scored change is binary or an LFS pointer; fix the setup or excludes.', exit_code=1
        )
    try:
        return data.decode('utf-8')
    except UnicodeError as exc:
        raise BenchmarkError(
            'unsupported_change', 'A scored change is not UTF-8; fix the setup or excludes.', exit_code=1
        ) from exc


def revision_states(cache: VCS, revision: str, paths: list[str]) -> dict[str, FileState]:
    """Read exact reference states for the selected Git revision paths."""
    tree = cache.tree(revision)
    result = {}
    for path in paths:
        safe_path(path)
        entry = tree.get(path)
        if entry is None:
            result[path] = FileState(False, '', None)
        elif entry.mode not in _REGULAR_MODES:
            raise BenchmarkError('unsupported_change', 'A scored Git file is not a regular source file.', exit_code=1)
        else:
            result[path] = FileState(True, _decode(cache.blob(entry)), entry.mode)
    return result


def prepare_workspace(cache: VCS, before: str, root: Path, relative_cwd: str) -> Workspace:
    """Export the complete before tree and create an unrelated local root commit."""
    safe_path(relative_cwd, allow_root=True)
    if root.exists():
        raise BenchmarkError('workspace_exists', 'A fresh attempt needs a new empty workspace path.', exit_code=1)
    root.mkdir(parents=True, mode=0o700)
    root = root.resolve()
    links = []
    for path, entry in cache.tree(before).items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry.mode not in _REGULAR_MODES | {'120000'}:
            raise BenchmarkError(
                'unsupported_snapshot', 'This revision contains a Git submodule or unsupported file mode.', exit_code=1
            )
        content = cache.blob(entry)
        if content.startswith(_LFS):
            raise BenchmarkError(
                'unsupported_snapshot', 'This revision requires Git LFS contents, which v1 does not fetch.', exit_code=1
            )
        if entry.mode == '120000':
            links.append((destination, content.decode('utf-8')))
        else:
            destination.write_bytes(content)
            destination.chmod(0o755 if entry.mode == '100755' else 0o644)
    for destination, target in links:
        destination.symlink_to(target)
    for destination, _target in links:
        try:
            resolved = destination.resolve()
        except (RuntimeError, OSError) as exc:
            raise BenchmarkError(
                'unsafe_snapshot', 'The source snapshot contains an unsafe symlink.', exit_code=1
            ) from exc
        if not resolved.is_relative_to(root) or '.git' in resolved.relative_to(root).parts:
            raise BenchmarkError(
                'unsafe_snapshot', 'A source symlink points outside the snapshot or into Git metadata.', exit_code=1
            )
    run_git(['init', '-q', str(root)], timeout=cache.timeout, stop=cache.stop)
    run_git(['config', 'core.autocrlf', 'false'], cwd=root)
    # Prevent repository attributes from transforming source bytes during capture.
    (root / '.git/info/attributes').write_text('* -filter -text -ident -working-tree-encoding\n')
    run_git(['add', '--force', '--all', '--', '.'], cwd=root)
    run_git(['commit', '--quiet', '--allow-empty', '-m', 'Benchmark before snapshot'], cwd=root)
    baseline = run_git(['rev-parse', 'HEAD'], cwd=root).decode().strip()
    cwd = root if relative_cwd == '.' else root / relative_cwd
    for ancestor in (cwd, *cwd.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise BenchmarkError('unsafe_workspace', 'Basket workspace cannot traverse a symlink.', exit_code=1)
    if cwd.exists() and not cwd.is_dir():
        raise BenchmarkError('unsafe_workspace', 'Basket workspace names a file instead of a directory.', exit_code=1)
    cwd.mkdir(parents=True, exist_ok=True)
    return Workspace(root, cwd, relative_cwd, baseline, cache, before)


def read_working_state(root: Path, path: str, *, directory_is_absent: bool = False) -> FileState:
    """Read a regular source file without following symlink escapes."""
    safe_path(path)
    candidate = root / path
    for ancestor in (candidate, *candidate.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise BenchmarkError('unsupported_change', 'A scored file or parent directory is a symlink.', exit_code=1)
    try:
        metadata = candidate.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return FileState(False, '', None)
    if directory_is_absent and stat.S_ISDIR(metadata.st_mode):
        if (candidate / '.git').exists() or (candidate / '.git').is_symlink():
            raise BenchmarkError('unsupported_change', 'A scored path became a nested Git repository.', exit_code=1)
        return FileState(False, '', None)
    if not stat.S_ISREG(metadata.st_mode):
        raise BenchmarkError('unsupported_change', 'A scored change is not a regular file.', exit_code=1)
    mode = '100755' if metadata.st_mode & stat.S_IXUSR else '100644'
    return FileState(True, _decode(candidate.read_bytes()), mode)


def capture_changes(workspace: Workspace, rules: list[str]) -> CapturedChanges:
    """Exclusions affect only agent changes, never the human reference."""
    tracked = run_git(
        ['diff', '--no-renames', '--no-ext-diff', '--no-textconv', '--name-only', '-z', workspace.baseline, '--'],
        cwd=workspace.root,
    )
    untracked = run_git(['ls-files', '--others', '--exclude-standard', '-z'], cwd=workspace.root)
    try:
        tracked_paths = {safe_path(p.decode('utf-8')) for p in tracked.split(b'\0') if p}
        new_paths = {safe_path(p.decode('utf-8').removesuffix('/')) for p in untracked.split(b'\0') if p}
    except UnicodeError as exc:
        raise BenchmarkError('unsupported_change', 'A changed path is not UTF-8.', exit_code=1) from exc
    matchers = [(rule, compile_exclude(resolve_exclude(rule, workspace.relative_cwd))) for rule in rules]
    selected = []
    excluded = {}
    for path in sorted(tracked_paths | new_paths):
        hits = [rule for rule, matcher in matchers if matcher.match(path)]
        if hits:
            excluded[path] = hits
        else:
            selected.append(path)
    chunks = []
    for path in selected:
        if path in tracked_paths:
            chunk = run_git(
                ['diff', '--no-renames', '--no-ext-diff', '--no-textconv', '--binary', workspace.baseline, '--', path],
                cwd=workspace.root,
            )
        else:
            chunk = run_git(
                ['diff', '--no-index', '--no-ext-diff', '--no-textconv', '--binary', '--', '/dev/null', path],
                cwd=workspace.root,
                accepted=(0, 1),
            )
        chunks.append(chunk)
    diff = b''.join(chunks)
    states = {}
    before_tree = workspace.cache.tree(workspace.before)
    for path in selected:
        try:
            original = before_tree.get(path)
            states[path] = read_working_state(
                workspace.root, path, directory_is_absent=original is not None and original.mode in _REGULAR_MODES
            )
        except BenchmarkError as error:
            error.paths = [path]
            error.captured_diff = diff
            error.files_count = len(selected)
            raise
    return CapturedChanges(states, diff, excluded)
