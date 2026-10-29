"""A wheel installed outside the source tree retains independent contracts."""

import hashlib
import json
import shutil
import subprocess
import sys
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
    code = (
        'import sys;sys.path.insert(0,sys.argv[1]);from viewer.validation.contracts i'
        'mport validator;from viewer.presentation.metrics import aggregate;import viewe'
        'r;from pathlib import Path;assert Path(viewer.__file__).resolve() == '
        '(Path(sys.argv[1])/"viewer"/"__init__.py").resolve();assert validator("report"'
        ');assert aggregate([])["score"] is None;from importlib.resources '
        'import files;assert all(len(files("viewer").joinpath("static",nam'
        'e).read_bytes())>100 for name in ("index.html","app.js","style.cs'
        's"));print(viewer.__version__)'
    )
    code += ';assert validator("basket", 3)'
    result = subprocess.run(
        [sys.executable, '-I', '-c', code, str(tmp_path / 'installed')],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == '0.3.1'


def test_frozen_contract_fixtures_match_recorded_hashes() -> None:
    """Verify schema and data fixture content identities."""
    root = Path(__file__).parents[1]
    records = json.loads((root / 'tests/fixtures/contract-fixtures.json').read_bytes())
    for target, entry in records.items():
        assert hashlib.sha256((root / target).read_bytes()).hexdigest() == entry['sha256']
