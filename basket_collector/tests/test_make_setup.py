"""Exercise setup confirmation and command ordering without installing anything."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ('existing', 'answer', 'expected_status', 'runs'),
    [
        (True, 'n\n', 0, False),
        (True, 'N\n', 0, False),
        (True, '', 2, False),
        (True, '\n', 0, True),
        (True, 'Y\n', 0, True),
        (True, 'yes\n', 0, True),
        (True, 'invalid\nn\n', 0, False),
        (False, '', 0, True),
    ],
)
def test_setup_confirmation(tmp_path: Path, existing: bool, answer: str, expected_status: int, runs: bool) -> None:
    """Cancellation makes no interpreter calls; confirmation keeps pip first."""
    shutil.copyfile(PROJECT / 'Makefile', tmp_path / 'Makefile')
    interpreter = tmp_path / 'fake-python'
    interpreter.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$SETUP_CALLS"\n'
        'if [ "$1 $2" = "-m venv" ]; then\n'
        '  mkdir -p .venv/bin\n'
        '  cp "$0" .venv/bin/python\n'
        'fi\n'
    )
    interpreter.chmod(0o755)
    if existing:
        (tmp_path / '.venv/bin').mkdir(parents=True)
        shutil.copyfile(interpreter, tmp_path / '.venv/bin/python')
        (tmp_path / '.venv/bin/python').chmod(0o755)
        (tmp_path / '.venv/keep').write_text('preserved')
    calls = tmp_path / 'calls'
    result = subprocess.run(
        ['make', 'setup', f'PYTHON={interpreter}'],
        cwd=tmp_path,
        env={**os.environ, 'SETUP_CALLS': str(calls)},
        input=answer,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected_status, result.stdout + result.stderr
    assert ('[Y/n]' in result.stdout) == existing
    if existing:
        assert (tmp_path / '.venv/keep').read_text() == 'preserved'
    if not runs:
        assert not calls.exists()
        return
    commands = calls.read_text().splitlines()
    assert commands[-3:] == [
        '-m pip install --upgrade pip',
        '-m pip install --require-hashes -r requirements.lock',
        '-m pip install --no-build-isolation --no-deps --config-settings editable_mode=strict -e .',
    ]
    assert ('-m venv .venv' in commands) != existing


def test_setup_stops_after_pip_bootstrap_failure(tmp_path: Path) -> None:
    """A failed fallback bootstrap must not proceed to pip upgrade or installation."""
    shutil.copyfile(PROJECT / 'Makefile', tmp_path / 'Makefile')
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\n' 'printf "%s\\n" "$*" >> "$SETUP_CALLS"\n' 'if [ "$1" = "-m" ]; then exit 1; fi\n')
    python.chmod(0o755)
    calls = tmp_path / 'calls'
    result = subprocess.run(
        ['make', 'setup', f'PYTHON={python}'],
        cwd=tmp_path,
        env={**os.environ, 'SETUP_CALLS': str(calls)},
        input='Y\n',
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    commands = calls.read_text().splitlines()
    assert commands[-1] == '-m pip --python .venv install --quiet --no-input pip'
    assert not any('install --upgrade' in command for command in commands)
