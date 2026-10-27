"""Real sandbox Python isolation and persistent per-attempt package installation."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import benchmark.execution.python_runtime as python_runtime
from benchmark.errors import BenchmarkError
from benchmark.execution.bin_projection import BinProjection
from benchmark.execution.python_runtime import PythonRuntime, prepare_python
from benchmark.execution.sandbox import Sandbox
from benchmark.models.runtime import PythonFile, PythonReceipt


@pytest.fixture(scope='module')
def base_python(tmp_path_factory: pytest.TempPathFactory) -> PythonRuntime:
    """Share only the immutable Python base across isolation tests."""
    return prepare_python(tmp_path_factory.mktemp('python-base'))


@pytest.fixture
def python_sandbox(tmp_path: Path, base_python: PythonRuntime) -> Sandbox:
    """Prepare the installed benchmark interpreter in one private sandbox."""
    source = tmp_path / 'source'
    source.mkdir()
    runtime = base_python
    sandbox = Sandbox(
        '/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'launch', Path('/usr/bin/true'), runtime
    )
    sandbox.prepare_python()
    return sandbox


@pytest.mark.parametrize(
    'executable',
    [
        'python',
        'python3',
        'python3.12',
        '/usr/bin/python',
        '/usr/bin/python3',
        '/usr/bin/python3.12',
        '/bin/python',
        '/bin/python3',
        '/bin/python3.12',
        '/usr/local/bin/python',
        '/usr/local/bin/python3',
        '/usr/local/bin/python3.12',
    ],
)
def test_python_entrypoints_use_attempt_venv(python_sandbox: Sandbox, executable: str) -> None:
    """Every standard entrypoint selects the private prefix and working extensions."""
    result = subprocess.run(
        python_sandbox.command(
            [
                executable,
                '-c',
                'import sys,json,ssl,sqlite3,lzma,bz2,zlib; '
                'print(json.dumps([list(sys.version_info[:2]),sys.prefix,sys.executable]))',
            ]
        ),
        env=python_sandbox.environment({}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    version, prefix, executable_path = json.loads(result.stdout)
    assert version == [3, 12]
    assert prefix == '/sandbox-home/.venv'
    assert executable_path.startswith('/sandbox-home/.venv/bin/python')


@pytest.mark.parametrize('shebang', ['#!/usr/bin/python3', '#!/bin/python3.12', '#!/usr/bin/env python3'])
def test_shebang_and_pip_share_environment(python_sandbox: Sandbox, shebang: str) -> None:
    """A direct shebang and pip both belong to the attempt environment."""
    script = python_sandbox.source / 'probe'
    script.write_text(shebang + '\nimport sys\nprint(sys.prefix)\n')
    script.chmod(0o700)
    result = subprocess.run(
        python_sandbox.command(['/repo/probe']),
        env=python_sandbox.environment({}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.strip() == '/sandbox-home/.venv'
    result = subprocess.run(
        python_sandbox.command(['python', '-m', 'pip', '--version']),
        env=python_sandbox.environment({}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert '/sandbox-home/.venv/lib/python3.12/site-packages/pip' in result.stdout


@pytest.mark.parametrize('compiler', ['gcc', 'g++'])
def test_system_compilers_keep_relative_tool_lookup(python_sandbox: Sandbox, compiler: str) -> None:
    """Real compiler drivers find their helpers and libraries through their original paths."""
    source = python_sandbox.source / 'main.c'
    source.write_text('int main(void) { return 0; }\n')
    for command in ([compiler, 'main.c', '-o', 'main'], ['./main']):
        result = subprocess.run(
            python_sandbox.command(command),
            env=python_sandbox.environment({}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('name', ['PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONNOUSERSITE', 'VIRTUAL_ENV'])
def test_python_environment_controls_are_reserved(python_sandbox: Sandbox, name: str) -> None:
    """Configuration cannot reintroduce foreign packages or replace the venv."""
    with pytest.raises(BenchmarkError, match='Bindings must not replace'):
        python_sandbox.environment({name: '/foreign'})


def test_host_packages_and_other_versions_are_unavailable(python_sandbox: Sandbox) -> None:
    """Installed benchmark dependencies and old standard interpreters stay out of the baseline."""
    result = subprocess.run(
        python_sandbox.command(
            ['python', '-c', 'import importlib.util; assert importlib.util.find_spec("pydantic") is None']
        ),
        env=python_sandbox.environment({}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    if Path('/usr/bin/python3.10').exists():
        result = subprocess.run(
            python_sandbox.command(['/usr/bin/python3.10', '--version']),
            env=python_sandbox.environment({}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 126
        assert 'Python 3.12 only' in result.stderr


def test_base_runtime_is_readonly(python_sandbox: Sandbox) -> None:
    """Agent-side package installation cannot modify the shared Python tree."""
    result = subprocess.run(
        python_sandbox.command(['/bin/sh', '-c', 'touch /benchmark-python/lib/forbidden']),
        env=python_sandbox.environment({}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert python_sandbox.python is not None
    assert not (python_sandbox.python.root / 'lib/forbidden').exists()


def _wheel(path: Path, version: str) -> None:
    """Build a tiny valid pure-Python wheel without package-index or build-tool access."""
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('isolation_marker.py', f'VERSION = {version!r}\n')
        prefix = 'isolation_marker-' + version + '.dist-info/'
        archive.writestr(
            prefix + 'METADATA', 'Metadata-Version: 2.1\nName: isolation-marker\nVersion: ' + version + '\n'
        )
        archive.writestr(prefix + 'WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr(prefix + 'RECORD', '')


def test_parallel_attempt_packages_are_private(tmp_path: Path, base_python: PythonRuntime) -> None:
    """Parallel setup installs distinct local wheels; a fresh retry inherits neither."""

    def attempt(version: str) -> str:
        """Install a fixture in one sandbox and inspect it in a separate invocation."""
        directory = tmp_path / version
        source = directory / 'source'
        source.mkdir(parents=True)
        wheel = source / f'isolation_marker-{version}-py3-none-any.whl'
        _wheel(wheel, version)
        sandbox = Sandbox(
            '/usr/bin/bwrap',
            source,
            source,
            directory / 'home',
            directory / 'launch',
            Path('/usr/bin/true'),
            base_python,
        )
        sandbox.prepare_python()
        subprocess.run(
            sandbox.command(['python', '-m', 'pip', 'install', '--no-index', '--no-deps', '/repo/' + wheel.name]),
            env=sandbox.environment({}),
            capture_output=True,
            check=True,
            timeout=30,
        )
        result = subprocess.run(
            sandbox.command(['python', '-c', 'import isolation_marker; print(isolation_marker.VERSION)']),
            env=sandbox.environment({}),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(attempt, ['1.0', '2.0'])) == ['1.0', '2.0']
    source = tmp_path / 'retry-source'
    source.mkdir()
    retry = Sandbox(
        '/usr/bin/bwrap',
        source,
        source,
        tmp_path / 'retry-home',
        tmp_path / 'retry-launch',
        Path('/usr/bin/true'),
        base_python,
    )
    retry.prepare_python()
    result = subprocess.run(
        retry.command(
            ['python', '-c', 'import importlib.util; assert importlib.util.find_spec("isolation_marker") is None']
        ),
        env=retry.environment({}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('addition', ['sitecustomize.py', 'site-packages', 'linked-directory', 'binary', 'linked-root'])
def test_cache_rejects_unmeasured_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, addition: str) -> None:
    """An intact measured binary cannot authorize extra executable files or package trees."""
    original = tmp_path / 'python'
    original.write_bytes(b'x')
    entry = PythonFile(path='bin/python3.12', sha256=hashlib.sha256(b'x').hexdigest(), size_bytes=1)
    digest = hashlib.sha256(f'{entry.path}\0{entry.sha256}\0{entry.size_bytes}\n'.encode()).hexdigest()
    receipt = PythonReceipt(
        version='3.12.0',
        implementation='cpython',
        platform='test',
        origin='benchmark_venv',
        content_sha256=digest,
        files=[entry],
    )
    monkeypatch.setattr(python_runtime, 'discover_python', lambda _cancel: ({entry.path: original}, receipt))
    runtime = prepare_python(tmp_path / 'cache')
    target = runtime.root / addition
    if addition == 'binary':
        (runtime.root / 'bin/python3.12').write_bytes(b'y')
    elif addition == 'linked-root':
        moved = runtime.root.with_name('original-runtime')
        runtime.root.rename(moved)
        runtime.root.symlink_to(moved, target_is_directory=True)
    elif addition == 'sitecustomize.py':
        target.write_text('raise RuntimeError("unmeasured")')
    elif addition == 'site-packages':
        target.mkdir()
    else:
        target.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BenchmarkError) as error:
        prepare_python(tmp_path / 'cache')
    assert error.value.code == 'python_cache_corrupt'


def test_absolute_pip_uses_private_environment(python_sandbox: Sandbox) -> None:
    """Absolute pip paths cannot select the host's Python or package installer."""
    for command in ('/usr/bin/pip', '/usr/bin/pip3', '/usr/bin/pip3.12', '/bin/pip3', '/usr/local/bin/pip3'):
        result = subprocess.run(
            python_sandbox.command([command, '--version']),
            env=python_sandbox.environment({}),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert '/sandbox-home/.venv/lib/python3.12/site-packages/pip' in result.stdout


@pytest.mark.parametrize('usrmerge', [False, True])
def test_bin_layout_and_broken_python_links(
    tmp_path: Path, base_python: PythonRuntime, monkeypatch: pytest.MonkeyPatch, usrmerge: bool
) -> None:
    """Exercise both bin layouts with a missing 3.12 entry and a broken other-version symlink."""
    base_python = replace(base_python, bins=BinProjection())
    real_symlink, real_resolve, real_readlink, real_iterdir = Path.is_symlink, Path.resolve, os.readlink, Path.iterdir
    broken = tmp_path / 'python3.99'
    broken.symlink_to('/missing-python-target')

    def is_symlink(path: Path) -> bool:
        """Select a bin layout without changing the host filesystem."""
        return usrmerge if str(path) == '/bin' else real_symlink(path)

    def resolve(path: Path, strict: bool = False) -> Path:
        """Make canonical bin ownership match the selected fixture layout."""
        return Path('/usr/bin' if usrmerge else '/bin') if str(path) == '/bin' else real_resolve(path, strict=strict)

    def entries(path: Path) -> Iterator[Path]:
        """Supply the deliberate broken/missing entries to the standard bin projection."""
        if str(path) == '/usr/bin':
            yield from (entry for entry in real_iterdir(path) if entry.name != 'python3.12')
            yield broken
        else:
            yield from real_iterdir(path)

    monkeypatch.setattr(Path, 'is_symlink', is_symlink)
    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(Path, 'iterdir', entries)
    monkeypatch.setattr(os, 'readlink', lambda path: 'usr/bin' if str(path) == '/bin' else real_readlink(path))
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox(
        '/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'launch', Path('/usr/bin/true'), base_python
    )
    sandbox.prepare_python()
    for command in ('/usr/bin/python3.12', '/bin/python3.12'):
        result = subprocess.run(
            sandbox.command([command, '-c', 'import sys; print(sys.prefix)']),
            env=sandbox.environment({}),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert result.stdout.strip() == '/sandbox-home/.venv'
    result = subprocess.run(
        sandbox.command(['/usr/bin/python3.99', '--version']),
        env=sandbox.environment({}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 126 and 'Python 3.12 only' in result.stderr


def test_host_and_user_marker_packages_are_not_inherited(
    tmp_path: Path, base_python: PythonRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover a base containing host/user markers, then prove neither reaches its child attempt."""
    installation = tmp_path / 'installation'
    shutil.copytree(base_python.root, installation)
    stdlib = installation / 'lib/python3.12'
    packages = stdlib / 'site-packages'
    packages.mkdir()
    (packages / 'host_package_marker.py').write_text('VALUE = "host"\n')
    userbase = tmp_path / 'user'
    usersite = userbase / 'lib/python3.12/site-packages'
    usersite.mkdir(parents=True)
    (usersite / 'user_package_marker.py').write_text('VALUE = "user"\n')
    environment = {'PATH': os.defpath, 'HOME': str(tmp_path), 'PYTHONUSERBASE': str(userbase)}
    subprocess.run(
        [str(installation / 'bin/python3.12'), '-c', 'import host_package_marker, user_package_marker'],
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
    )
    original_path = python_runtime.sysconfig.get_path
    with monkeypatch.context() as discovery:
        discovery.setattr(sys, '_base_executable', str(installation / 'bin/python3.12'))
        discovery.setattr(
            python_runtime.sysconfig, 'get_path', lambda name: str(stdlib) if name == 'stdlib' else original_path(name)
        )
        runtime = prepare_python(tmp_path / 'cache')
    assert not (runtime.root / 'lib/python3.12/site-packages').exists()
    monkeypatch.setenv('PYTHONUSERBASE', str(userbase))
    monkeypatch.setenv('PYTHONPATH', str(packages))
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox(
        '/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'launch', Path('/usr/bin/true'), runtime
    )
    sandbox.prepare_python()
    result = subprocess.run(
        sandbox.command(
            [
                'python',
                '-c',
                'import importlib.util; assert all(importlib.util.find_spec(name) is None '
                'for name in ("host_package_marker", "user_package_marker"))',
            ]
        ),
        env=sandbox.environment({}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_setup_rejects_broken_venv_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A discoverable base cannot pass setup when its venv bootstrap fails."""
    broken = tmp_path / 'broken-python'
    broken.write_text('#!/bin/sh\necho "ensurepip wheel unavailable" >&2\nexit 1\n')
    broken.chmod(0o755)
    monkeypatch.setattr(python_runtime, 'discover_python', lambda: None)
    monkeypatch.setattr(sys, 'executable', str(broken))
    with pytest.raises(BenchmarkError) as error:
        python_runtime.validate_setup_python(timeout=5)
    assert error.value.code == 'python_bootstrap_unavailable'


def test_runtime_probe_does_not_import_project_shadow_modules(tmp_path: Path, base_python: PythonRuntime) -> None:
    """Probe stdlib from a trusted script while ordinary agent commands retain source imports."""
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'ssl.py').write_text('raise RuntimeError("project ssl is not a runtime probe")\n')
    (source / 'project_marker.py').write_text('VALUE = 42\n')
    sandbox = Sandbox(
        '/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'launch', Path('/usr/bin/true'), base_python
    )
    sandbox.prepare_python()
    result = subprocess.run(
        sandbox.command(['python', '-c', 'import project_marker; print(project_marker.VALUE)']),
        env=sandbox.environment({}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.strip() == '42'
