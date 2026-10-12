"""Make passes user strings as environment data, preserving validation and stdin."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Make with raw arguments and capture output in the owning project."""
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    return subprocess.run(
        ['make', '--no-print-directory', *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_help_works_before_setup_without_expanding_user_values(tmp_path: Path) -> None:
    """The entrypoint works with no venv/config and never evaluates user-supplied Make expressions."""
    marker = tmp_path / 'unexpected'
    makefile = Path(__file__).resolve().parents[1] / 'Makefile'
    result = subprocess.run(
        [
            'make',
            '-f',
            str(makefile),
            'help',
            'VENV_PYTHON=/missing/python',
            'PYTHON=/missing/python',
            'CONFIG=$(shell touch ' + str(marker) + ')',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists() and not (tmp_path / '.venv').exists()
    assert all(
        'make ' + name in result.stdout
        for name in (
            'setup',
            'env',
            'run',
            'auth',
            'rescore',
            'reprice',
            'visualize',
            'compare',
            'publish',
            'test',
            'smoke',
            'help',
        )
    )
    assert 'no setup or credentials needed' in result.stdout


def test_make_paths_are_data_and_cannot_execute_shell(tmp_path: Path) -> None:
    """Verify make paths are data and cannot execute shell."""
    marker = tmp_path / 'unexpected'
    config = 'missing config \' `touch ' + str(marker) + '` .yaml'
    result = invoke('run', 'CONFIG=' + config, 'DATASET=missing basket.json')
    assert result.returncode != 0
    assert not marker.exists()
    assert 'Traceback' not in result.stderr


def test_make_required_arguments_and_boolean_validation() -> None:
    """Verify make required arguments and boolean validation."""
    result = invoke('run', 'CONFIG=')
    assert 'CONFIG must not be empty' in result.stderr
    result = invoke('rescore', 'RUN=')
    assert 'Rescore requires RUN' in result.stderr
    result = invoke('run', 'REPRICE=yes')
    assert 'REPRICE accepts only 0 or 1' in result.stderr
    result = invoke('run', 'RESUME=a', 'OUTPUT=b')
    assert 'RESUME and explicit OUTPUT cannot be combined' in result.stderr
    result = invoke('run', 'PROGRESS=invalid')
    assert 'PROGRESS must be auto, plain or off' in result.stderr


def test_literal_credentials_are_rejected_without_printing_values() -> None:
    """Verify literal credentials are rejected without printing values."""
    result = invoke('publish', 'TOKEN=fixture_secret_do_not_echo')
    assert result.returncode != 0
    assert 'fixture_secret_do_not_echo' not in result.stdout + result.stderr
    assert 'Unsupported Make parameter' in result.stderr
    result = invoke('publish', 'DRY_RUN=yes', 'RUN=missing', 'URL=https://viewer.example')
    assert 'accept only 0 or 1' in result.stderr
    result = invoke('compare', 'RUNS_JSON=[1,2]', 'NO_BROWSER=1')
    assert 'nonempty path strings' in result.stderr


def test_unknown_selectors_and_malformed_json_are_not_silently_ignored() -> None:
    """Verify unknown selectors and malformed json are not silently ignored."""
    result = invoke('run', 'sampling=1')
    assert 'Unsupported Make parameter' in result.stderr
    result = invoke('compare', 'RUNS_JSON=bad-json', 'NO_BROWSER=1')
    assert 'RUNS_JSON must be a valid JSON array' in result.stderr


def test_make_preserves_dollars_and_does_not_expand_make_functions(tmp_path: Path) -> None:
    """Pass dollar-bearing paths and Make-function text as literal environment values."""
    supplemental = tmp_path / 'inspect.mk'
    marker = tmp_path / 'must_not_exist'
    output = tmp_path / 'values.json'
    supplemental.write_text(
        '.PHONY: inspect-values\ninspect-values:\n'
        '\t@.venv/bin/python -c \'import json,os; '
        'open(os.environ["PRSIM_BM_RUN"],"w").write(json.dumps({k:os.environ[k] for k in '
        '["PRSIM_BM_CONFIG","PRSIM_BM_DATASET","PRSIM_BM_RUNS_JSON"]}))\'\n'
    )
    values = {
        'CONFIG': str(tmp_path / 'config$NAME.yaml'),
        'DATASET': str(tmp_path / 'basket$ID.json'),
        'RUNS_JSON': '["a$PATH","b$(shell touch ' + str(marker) + ')"]',
    }
    result = invoke(
        '-f',
        'Makefile',
        '-f',
        str(supplemental),
        'inspect-values',
        'RUN=' + str(output),
        *(name + '=' + value for name, value in values.items()),
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    captured = json.loads(output.read_text())
    assert captured == {'PRSIM_BM_' + name: value for name, value in values.items()}


@pytest.mark.parametrize('name', ['TOKEN', 'sampling'])
def test_unknown_make_parameters_never_expand_values(tmp_path: Path, name: str) -> None:
    """Reject unsupported command-line names without evaluating their Make expressions."""
    marker = tmp_path / 'unexpected_unknown_expansion'
    result = invoke('run', name + '=$(shell touch ' + str(marker) + ')')
    assert result.returncode != 0
    assert not marker.exists()
    assert 'Unsupported Make parameter' in result.stderr


@pytest.mark.parametrize('command', ['run', 'publish'])
def test_unknown_cli_options_are_rejected_without_echoing_values(command: str) -> None:
    """Reject unknown options before loading inputs and keep literal values out of errors."""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-m', 'benchmark', command, '--unknown-option', 'fixture_secret_do_not_echo'],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert 'Invalid arguments; use --help for accepted options.' in result.stderr
    assert 'fixture_secret_do_not_echo' not in result.stdout + result.stderr
