"""A wheel installed outside the source tree retains independent contracts."""

import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from zipfile import ZipFile


def test_wheel_installed_without_source(tmp_path: Path) -> None:
    """Verify wheel installed without source."""
    root = Path(__file__).parents[1]
    project = tmp_path / 'project'
    project.mkdir()
    for name in ('pyproject.toml', 'README.md'):
        shutil.copy2(root / name, project / name)
    shutil.copytree(root / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    subprocess.run(
        [sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', str(tmp_path / 'dist')],
        cwd=project,
        check=True,
        capture_output=True,
    )
    wheel = next((tmp_path / 'dist').glob('*.whl'))
    expected = {
        'viewer/' + path.relative_to(project / 'src').as_posix()
        for path in (project / 'src').rglob('*')
        if path.is_file() and path.suffix in ('.py', '.json')
    }
    with ZipFile(wheel) as archive:
        packaged = {
            name for name in archive.namelist() if name.startswith('viewer/') and name.endswith(('.py', '.json'))
        }
    assert packaged == expected
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--no-deps', '--target', str(tmp_path / 'installed'), str(wheel)],
        check=True,
        capture_output=True,
    )
    code = '''
"""Verify contracts, resources and import origin of the isolated viewer wheel."""

import sys
from importlib import import_module
from importlib.resources import files
from pathlib import Path

# This isolated subprocess resolves only the installed wheel under test.
sys.path.insert(0, sys.argv[1])
viewer = import_module('viewer')
validator = import_module('viewer.validation.contracts').validator
aggregate = import_module('viewer.presentation.metrics').aggregate
assert viewer.__file__ is not None
assert Path(viewer.__file__).resolve() == (Path(sys.argv[1]) / 'viewer' / '__init__.py').resolve()
assert validator('report')
assert aggregate([])['score'] is None
assert all(
    len(files('viewer').joinpath('static', name).read_bytes()) > 100 for name in ('index.html', 'app.js', 'style.css')
)
print(viewer.__version__)
assert validator('basket', 3)
'''
    result = subprocess.run(
        [sys.executable, '-I', '-c', code, str(tmp_path / 'installed')],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']


def test_frozen_contract_fixtures_match_recorded_hashes() -> None:
    """Verify schema and data fixture content identities."""
    root = Path(__file__).parents[1]
    records = json.loads((root / 'tests/fixtures/contract-fixtures.json').read_bytes())
    for target, entry in records.items():
        assert hashlib.sha256((root / target).read_bytes()).hexdigest() == entry['sha256']
