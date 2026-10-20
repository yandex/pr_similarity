"""Exercise a built wheel from outside the source checkout."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path


def test_wheel_includes_schemas_and_instruction_resource(tmp_path: Path) -> None:
    """Verify wheel includes schemas and instruction resource."""
    root = Path(__file__).resolve().parents[1]
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
            str(root),
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
import sys
from pathlib import Path
from benchmark.config import load_config
from benchmark.contracts import document_validator
for name in ('config', 'report', 'manifest', 'usage', 'pricing', 'basket', 'run', 'state'):
    document_validator(name)
document_validator('basket', 2)
assert load_config(Path(sys.argv[2])).identity()['instructions']['protocol'] == 'benchmark_task_v1'
import benchmark
assert Path(benchmark.__file__).resolve().is_relative_to(Path(sys.argv[1]))
from benchmark.__main__ import main
sys.argv = ['benchmark', 'rescore']
assert main() == 2
'''
    config = tmp_path / 'config.yaml'
    config.write_text(
        'schema_version: 1\nagent: {name: wheel, runtime: "opencode:v1.18.33", '
        'config: {opencode_json: {model: example/model}}}\n'
    )
    subprocess.run(
        [str(python), '-I', '-c', code, str(installed), str(config)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=True,
    )
