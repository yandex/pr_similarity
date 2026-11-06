"""Package the viewer-owned run interface for independent offline benchmark use."""

import argparse
import ast
from pathlib import Path

ASSETS = ('app.js', 'config.js', 'style.css', 'index.html', 'favicon.svg')


def expected_files(root: Path) -> dict[Path, bytes]:
    """Keep renderer bytes identical and adapt only pure metric module imports."""
    result = {
        root / 'benchmark/src/presentation/run_page_assets' / name: (root / 'viewer/src/static' / name).read_bytes()
        for name in ASSETS
    }
    metrics = (root / 'viewer/src/presentation/metrics.py').read_text()
    metrics = metrics.replace(
        'from viewer.errors import ViewerError',
        'from benchmark.errors import BenchmarkError as ViewerError',
    )
    metrics = metrics.replace('from viewer.', 'from benchmark.')
    result[root / 'benchmark/src/presentation/run_metrics.py'] = metrics.encode()
    result[root / 'benchmark/src/presentation/file_views.py'] = (
        root / 'viewer/src/presentation/file_views.py'
    ).read_bytes()
    config = (root / 'viewer/src/config.py').read_text()
    limits = next(node for node in ast.parse(config).body if isinstance(node, ast.ClassDef) and node.name == 'Limits')
    names = {'diff_bytes', 'diff_lines', 'config_display_bytes'}
    defaults: dict[str, str] = {}
    for node in limits.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in names or node.value is None:
            continue
        value = ast.get_source_segment(config, node.value)
        if value is not None:
            defaults[node.target.id] = value
    if set(defaults) != names:
        raise ValueError('Viewer display defaults are incomplete')
    content = '"""Viewer-owned display defaults for standalone HTML."""\n\n'
    content += ''.join(name.upper() + ' = ' + defaults[name] + '\n' for name in sorted(defaults))
    result[root / 'benchmark/src/presentation/display_limits.py'] = content.encode()
    return result


def sync(root: Path, *, check: bool) -> None:
    """Reject drift or explicitly refresh the packaged renderer snapshot."""
    for path, content in expected_files(root).items():
        if check:
            if not path.is_file() or path.read_bytes() != content:
                raise ValueError('Run-page copy differs from viewer: ' + str(path.relative_to(root)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def main() -> None:
    """Parse CLI options and synchronize or validate packaged resources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    sync(Path(__file__).resolve().parents[1], check=args.check)


if __name__ == '__main__':
    main()
