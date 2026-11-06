"""Exercise a built wheel from outside the source checkout."""

import os
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path


def test_wheel_includes_schemas_and_instruction_resource(tmp_path: Path) -> None:
    """Verify wheel includes schemas and instruction resource."""
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / 'project'
    project.mkdir()
    for name in ('pyproject.toml', 'README.md'):
        shutil.copy2(root / name, project / name)
    shutil.copytree(root / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'pip',
            'wheel',
            '--no-build-isolation',
            '--no-deps',
            '--wheel-dir',
            str(tmp_path),
            str(project),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'Successfully built' in result.stdout
    wheel = next(tmp_path.glob('*.whl'))
    installed = tmp_path / 'installed'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(installed)], check=True, capture_output=True)
    python = installed / 'bin/python'
    site = installed / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    # Reuse installed dependencies, while excluding the editable source package and its import hook.
    for dependency in Path(sysconfig.get_path('purelib')).iterdir():
        if dependency.name.startswith(('benchmark', 'pr_similarity_os_benchmark', '__editable__')):
            continue
        if not (site / dependency.name).exists():
            (site / dependency.name).symlink_to(dependency)
    subprocess.run(
        [sys.executable, '-m', 'pip', '--python', str(python), 'install', '--no-index', '--no-deps', str(wheel)],
        capture_output=True,
        check=True,
    )
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    code = '''
"""Verify contracts and resources from the isolated installed benchmark wheel."""

import sys
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import benchmark
from benchmark.__main__ import main
from benchmark.config import load_config
from benchmark.presentation.run_page import render
from benchmark.validation.contracts import document_validator

for name in ('config', 'report', 'manifest', 'usage', 'pricing', 'basket', 'run', 'state'):
    document_validator(name)
for basket_version in (1, 2, 3):
    document_validator('basket', basket_version)
assert load_config(Path(sys.argv[2])).identity().instructions.protocol == 'benchmark_task_v1'
assert version('pr-similarity-os-benchmark') == sys.argv[3]
assert callable(render)
for name in ('app.js', 'config.js', 'style.css', 'index.html', 'offline.js', 'favicon.svg'):
    assert len(files('benchmark').joinpath('presentation/run_page_assets', name).read_bytes()) > 100
document_validator('state', 3)
assert not files('benchmark').joinpath('run_store.py').is_file()
assert files('benchmark').joinpath('persistence/run_store.py').is_file()
assert files('benchmark').joinpath('models/report.py').is_file()
assert Path(benchmark.__file__).resolve().is_relative_to(Path(sys.argv[1]))
sys.argv = ['benchmark', 'rescore']
assert main() == 2
'''
    config = tmp_path / 'config.yaml'
    config.write_text(
        'schema_version: 1\nagent: {name: wheel, runtime: "opencode:v1.18.33", '
        'config: {opencode_json: {model: example/model}}}\n'
    )
    completed = subprocess.run(
        [
            str(python),
            '-I',
            '-c',
            code,
            str(installed),
            str(config),
            tomllib.loads((root / 'pyproject.toml').read_text())['project']['version'],
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode('utf-8', errors='replace')
