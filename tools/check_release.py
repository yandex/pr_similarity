"""Verify the explicit portable source inventory without VCS, network or credentials."""

import argparse
import hashlib
import json
import stat
import tomllib
from pathlib import Path

MODULES = ('basket_collector', 'benchmark', 'viewer')
ROOT_FILES = ('README.md', 'AGENTS.md', 'CLAUDE.md', 'CHANGELOG.md', 'LICENSE',
              'CONTRIBUTING.md', 'NOTICES', '.gitignore', '.github/workflows/ci.yml')
SKILLS = ('prsim-build-basket', 'prsim-run-benchmark', 'prsim-analyze-run', 'prsim-publish-run')
MODULE_FILES = ('README.md', 'AGENTS.md', 'CLAUDE.md', 'Makefile', 'pyproject.toml',
                '.gitignore', '.env.example', 'requirements.lock',
                'requirements-build.lock', 'config.yaml', 'config.smoke-live.yaml',
                'config.example.json', 'config.mock.example.json')
SOURCE_DIRS = ('src', 'tests', 'examples', 'prompts', 'ops')
TRANSIENT_DIRS = frozenset({'.venv', '__pycache__', '.pytest_cache', '.ruff_cache',
                          'build', 'dist', '*.egg-info'})
PRIVATE_NAMES = frozenset({'.env', 'auth.json', 'output', 'runtime', 'cache', 'cache.lock', 'runs', 'artifacts'})
MANIFEST = 'release-manifest.json'


def private_path(path: str) -> bool:
    """Identify operator state, while preserving the public .env.example template."""
    parts = Path(path).parts
    return any(part in PRIVATE_NAMES or part.startswith('config.local.')
               or part in TRANSIENT_DIRS or part.endswith('.egg-info') for part in parts)


def regular_file(root: Path, relative: str) -> Path:
    """Reject links at every level before reading a selected source file."""
    path = Path(relative)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        raise ValueError('Unsafe release path: ' + relative)
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('Release contains a symlink: ' + relative)
    if not stat.S_ISREG(target.stat().st_mode):
        raise ValueError('Release file is not regular: ' + relative)
    return target


def source_inventory(root: Path) -> list[str]:
    """Select named public files and bounded owning-module subtrees only."""
    if not all((root / module).is_dir() for module in MODULES):
        raise ValueError('Expected the PR Similarity repository root.')
    selected = list(ROOT_FILES)
    selected += ['.agents/skills/' + skill + '/SKILL.md' for skill in SKILLS]
    selected += ['tools/check_release.py', 'tools/prepare_release.py', 'tools/tests/test_release_export.py']
    for module in MODULES:
        for name in MODULE_FILES:
            path = root / module / name
            if path.exists() or path.is_symlink():
                selected.append(module + '/' + name)
        for name in SOURCE_DIRS:
            directory = root / module / name
            if directory.is_symlink():
                raise ValueError('Release source directory is a symlink.')
            if not directory.exists():
                continue
            for path in sorted(directory.rglob('*')):
                relative = path.relative_to(root).as_posix()
                if any(part in TRANSIENT_DIRS or part.endswith('.egg-info') for part in path.parts):
                    continue
                if path.is_symlink():
                    raise ValueError('Release source contains a symlink: ' + relative)
                if path.is_dir():
                    continue
                if private_path(relative):
                    raise ValueError('Private file inside a release source subtree: ' + relative)
                selected.append(relative)
    result = sorted(set(selected))
    for relative in result:
        regular_file(root, relative)
    return result


def file_record(root: Path, relative: str) -> dict:
    path = regular_file(root, relative)
    content = path.read_bytes()
    return {'path': relative, 'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(),
            'executable': bool(path.stat().st_mode & stat.S_IXUSR)}


def check_release(root: Path) -> dict:
    """Check manifest coverage, legal files, module metadata and mirrored contracts."""
    root = root.resolve()
    manifest_path = regular_file(root, MANIFEST)
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get('manifest_version') != 1 or not isinstance(manifest.get('files'), list):
        raise ValueError('Invalid release manifest.')
    expected = source_inventory(root)
    records = [file_record(root, relative) for relative in expected]
    if manifest['files'] != records:
        raise ValueError('Release files differ from the recorded inventory.')
    actual = []
    for path in root.rglob('*'):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or private_path(relative):
            raise ValueError('Private or linked release content: ' + relative)
        if not path.is_dir():
            regular_file(root, relative)
            actual.append(relative)
    if sorted(actual) != sorted([*expected, MANIFEST]):
        raise ValueError('Release contains unregistered files.')
    versions = set()
    for module in MODULES:
        metadata = tomllib.loads((root / module / 'pyproject.toml').read_text())
        versions.add(metadata['project']['version'])
        if metadata['project'].get('license') != 'Apache-2.0':
            raise ValueError('Module license metadata differs: ' + module)
    if len(versions) != 1:
        raise ValueError('Module release versions disagree.')
    for family in ('run', 'report', 'state', 'usage'):
        source = root / 'benchmark/src/schemas' / ('benchmark-' + family + '-v1.schema.json')
        target = root / 'viewer/src/schemas' / (family + '-v1.json')
        if source.read_bytes() != target.read_bytes():
            raise ValueError('Producer/consumer schema differs: ' + family)
    vendor = root / 'benchmark/examples/skills/test-driven-development'
    upstream = json.loads((vendor / 'UPSTREAM.json').read_bytes())
    for entry in upstream['files']:
        if hashlib.sha256(regular_file(vendor, entry['path']).read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Vendored input differs from its provenance: ' + entry['path'])
    return {'version': versions.pop(), 'files': len(records), 'size_bytes': sum(r['size_bytes'] for r in records),
            'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(check_release(args.directory), indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
