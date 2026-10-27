"""Exercise setup confirmation and install order without invoking package tools."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROMPT = 'Virtual environment already exists. Reinstall dependencies? [Y/n] '


def _project(tmp_path: Path, *, existing: bool, usable: bool = True) -> tuple[Path, Path]:
    """Create an isolated Make project and a recording interpreter that never uses the network."""
    shutil.copy2(Path(__file__).parents[1] / 'Makefile', tmp_path / 'Makefile')
    interpreter = tmp_path / 'setup-python'
    interpreter.write_text('#!' + sys.executable + '\n' + '''
import json
import os
import shutil
import sys
from pathlib import Path

arguments = sys.argv[1:]
with Path(os.environ['SETUP_CALL_LOG']).open('a') as stream:
    stream.write(json.dumps({'argv': arguments, 'index': os.getenv('PIP_INDEX_URL')}) + '\\n')
if arguments[:2] == ['-m', 'venv']:
    if os.environ.get('SETUP_VENV_FALLBACK') and '--without-pip' not in arguments:
        sys.exit(1)
    target = Path('.venv/bin/python')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, target)
if arguments == ['-m', 'pip', '--version'] and os.environ.get('SETUP_BOOTSTRAP'):
    sys.exit(1)
if os.environ.get('SETUP_FAIL_LOCK') and arguments[-2:] == ['-r', 'requirements-build.lock']:
    sys.exit(7)
''')
    interpreter.chmod(0o755)
    if existing:
        (tmp_path / '.venv').mkdir()
        (tmp_path / '.venv/preserved').write_text('existing environment')
        if usable:
            (tmp_path / '.venv/bin').mkdir()
            shutil.copy2(interpreter, tmp_path / '.venv/bin/python')
    return interpreter, tmp_path / 'calls.jsonl'


def _setup(
    tmp_path: Path, interpreter: Path, log: Path, answer: str, *, variables: tuple[str, ...] = (), **environment: str
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    """Run only the copied Make recipe and return the fake interpreter calls."""
    result = subprocess.run(
        [
            'make',
            'setup',
            'PYTHON=' + str(interpreter),
            'PACKAGE_INDEX_URL=https://packages.example/simple',
            *variables,
        ],
        cwd=tmp_path,
        env={**os.environ, 'SETUP_CALL_LOG': str(log), **environment},
        input=answer,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


@pytest.mark.parametrize('answer', ['\n', 'y\n', 'Y\n', 'yes\n', 'YES\n'])
def test_existing_setup_requires_affirmative_answer(tmp_path: Path, answer: str) -> None:
    """A confirmed setup preserves the environment and upgrades pip before dependencies."""
    interpreter, log = _project(tmp_path, existing=True)
    result, calls = _setup(tmp_path, interpreter, log, answer)
    assert result.returncode == 0 and PROMPT in result.stdout
    assert (tmp_path / '.venv/preserved').read_text() == 'existing environment'
    assert not any(call['argv'][:2] == ['-m', 'venv'] for call in calls)
    installs = [call for call in calls if call['argv'][:3] == ['-m', 'pip', 'install']]
    assert installs[0]['argv'] == ['-m', 'pip', 'install', '--upgrade', 'pip']
    assert installs[1]['argv'] == ['-m', 'pip', 'install', '--require-hashes', '-r', 'requirements-build.lock']
    assert installs[2]['argv'] == ['-m', 'pip', 'install', '-r', 'requirements.lock']
    assert installs[3]['argv'] == ['-m', 'pip', 'install', '--no-build-isolation', '--no-deps', '-e', '.']
    assert all(call['index'] == 'https://packages.example/simple' for call in installs[:3])


@pytest.mark.parametrize('usable', [False, True])
@pytest.mark.parametrize('answer', ['n\n', 'N\n', 'no\n', 'NO\n', ''])
def test_existing_setup_decline_or_eof_never_calls_python(tmp_path: Path, usable: bool, answer: str) -> None:
    """Both partial and usable environments remain untouched on decline or missing input."""
    interpreter, log = _project(tmp_path, existing=True, usable=usable)
    result, calls = _setup(tmp_path, interpreter, log, answer)
    assert PROMPT in result.stdout and not calls
    assert (result.returncode == 0) == bool(answer)
    assert (tmp_path / '.venv/preserved').read_text() == 'existing environment'
    assert (tmp_path / '.venv/bin/python').exists() == usable


def test_fresh_setup_needs_no_input_and_keeps_bootstrap_fallback(tmp_path: Path) -> None:
    """Create a missing environment and preserve pip bootstrap when initial venv setup fails."""
    interpreter, log = _project(tmp_path, existing=False)
    result, calls = _setup(tmp_path, interpreter, log, '', SETUP_VENV_FALLBACK='1', SETUP_BOOTSTRAP='1')
    assert result.returncode == 0 and PROMPT not in result.stdout
    assert calls[0]['argv'] == ['-m', 'venv', '.venv']
    assert calls[1]['argv'] == ['-m', 'venv', '--without-pip', '.venv']
    bootstrap = next(call for call in calls if '--python' in call['argv'])
    assert bootstrap['argv'] == ['-m', 'pip', '--python', '.venv', 'install', 'pip']
    assert bootstrap['index'] == 'https://packages.example/simple'


def test_setup_stops_after_failed_dependency_install(tmp_path: Path) -> None:
    """A failed build dependency step must prevent runtime and editable installation."""
    interpreter, log = _project(tmp_path, existing=True)
    result, calls = _setup(tmp_path, interpreter, log, '\n', SETUP_FAIL_LOCK='1')
    assert result.returncode != 0
    assert calls[-1]['argv'][-2:] == ['-r', 'requirements-build.lock']


def test_setup_answer_is_not_executed_as_shell_text(tmp_path: Path) -> None:
    """An invalid answer is retried without evaluating command substitution."""
    interpreter, log = _project(tmp_path, existing=True)
    marker = tmp_path / 'executed'
    result, calls = _setup(tmp_path, interpreter, log, f'$(touch {marker})\nn\n')
    assert result.returncode == 0 and result.stdout.count(PROMPT) == 2
    assert not calls and not marker.exists()


def test_setup_detects_a_broken_environment_link(tmp_path: Path) -> None:
    """Declining a broken environment link must not follow or replace it."""
    interpreter, log = _project(tmp_path, existing=False)
    (tmp_path / '.venv').symlink_to(tmp_path / 'missing')
    result, calls = _setup(tmp_path, interpreter, log, 'n\n')
    assert result.returncode == 0 and PROMPT in result.stdout and not calls
    assert (tmp_path / '.venv').is_symlink()


def test_setup_detects_a_custom_executable_without_venv(tmp_path: Path) -> None:
    """An explicitly supplied usable interpreter also requires reinstall confirmation."""
    interpreter, log = _project(tmp_path, existing=False)
    result, calls = _setup(tmp_path, interpreter, log, 'n\n', variables=('VENV_PYTHON=' + str(interpreter),))
    assert result.returncode == 0 and PROMPT in result.stdout and not calls
    assert not (tmp_path / '.venv').exists()


def test_setup_package_index_remains_literal_shell_text(tmp_path: Path) -> None:
    """Keep Make and shell substitutions inside the package index inert."""
    interpreter, log = _project(tmp_path, existing=True)
    marker = tmp_path / 'executed'
    index = f'https://packages.example/$(shell touch {marker})'
    result, calls = _setup(tmp_path, interpreter, log, '\n', variables=('PACKAGE_INDEX_URL=' + index,))
    assert result.returncode == 0 and not marker.exists()
    upgrades = [call for call in calls if call['argv'] == ['-m', 'pip', 'install', '--upgrade', 'pip']]
    assert len(upgrades) == 1 and upgrades[0]['index'] == index
