"""Standalone HTML stays offline, escapes untrusted task data and shows unknown usage."""

import json
import subprocess
import sys
import webbrowser
from pathlib import Path

import pytest
import yaml
from test_runner import config_and_basket, fake_attempt, provision_fixture

from benchmark import __main__ as cli
from benchmark.config import load_config
from benchmark.models import BasketV3
from benchmark.persistence.storage import read_json, write_json
from benchmark.presentation.report import read_report
from benchmark.presentation.visualize import _status, _task, visualize
from benchmark.runtime.runner import run


def sample(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Generate an actual registry/state run with frozen unsafe title and unknown usage."""
    source = tmp_path / name
    source.mkdir()
    _config, dataset = config_and_basket(source)
    config_path = source / 'config.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['agent']['name'] = 'fixture-agent' if name == 'one' else 'second-agent'
    config_path.write_text(yaml.safe_dump(config))
    basket = BasketV3.model_validate(read_json(dataset))
    basket.tasks[0].change_request.title = '</script><script>alert("untrusted")</script>'
    if name == 'two':
        basket.meta.name = 'Different basket'
    write_json(dataset, basket)
    provision_fixture(monkeypatch)
    status, directory = run(
        load_config(config_path), dataset, output=source / 'runs', attempt_function=fake_attempt(['success'])
    )
    assert status == 0
    return directory


def test_visualize_is_standalone_safe_and_preserves_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify visualize is standalone safe and preserves report."""
    run = sample(tmp_path, 'one', monkeypatch)
    before = (run / 'report.json').read_bytes()
    monkeypatch.setattr('webbrowser.open', lambda url: (_ for _ in ()).throw(AssertionError('browser forbidden')))
    path = visualize(run)
    document = path.read_text()
    assert '&lt;/script&gt;&lt;script&gt;' in document
    assert '<script>alert(' not in document
    assert 'cache read / cache write' in document and '—' in document
    assert '<script src=' not in document and '<link ' not in document
    assert 'Filter tasks' in document
    assert 'class="run-header"' in document and 'class="task"' in document
    assert 'class="task-body"' in document and 'class="condition"' in document
    assert '<table>' not in document
    assert 'id="task-sort"' in document and 'id="task-direction"' in document
    assert (run / 'report.json').read_bytes() == before


def test_visualize_embeds_lossless_facts_and_viewer_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep embedded evidence exact and expose the viewer's default selection controls."""
    directory = sample(tmp_path, 'one', monkeypatch)
    document = visualize(directory).read_text()
    payload = document.split('<script type="application/json" id="report-data">', 1)[1].split('</script>', 1)[0]
    assert json.loads(payload) == read_json(directory / 'report.json')
    assert '<input type="checkbox" value="fail">' in document
    assert '<input type="checkbox" value="OK" checked>' in document
    assert '<option selected>LCpr</option>' in document
    assert '<option selected>LCf</option>' in document
    assert 'href="#overview"' in document and 'href="#tasks"' in document
    assert 'Original PR' in document


def test_visualize_status_is_independent_of_scoring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinguish a no-op, timeout and failed execution without inferring from score."""
    directory = sample(tmp_path, 'one', monkeypatch)
    report = read_report(directory)
    task = report.tasks[0]
    task.processing_status = 'completed'
    task.infrastructure_error = None
    task.execution.status = 'completed'
    task.changes.status = 'empty'
    assert _status(task) == 'no-op'
    task.changes.status = 'nonempty'
    assert _status(task) == 'OK'
    task.execution.status = 'timeout'
    assert _status(task) == 'timeout'
    task.execution.status = 'error'
    assert _status(task) == 'fail'
    task.execution.status = 'completed'
    task.processing_status = 'retry_pending'
    assert _status(task) == 'fail'


def test_visualize_opens_browser_only_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit browser opt-in receives the already-written HTML URI."""
    directory = sample(tmp_path, 'one', monkeypatch)
    calls: list[str] = []

    def open_browser(url: str) -> bool:
        """Record the request without starting a real desktop browser."""
        assert (directory / 'report.html').is_file()
        calls.append(url)
        return True

    monkeypatch.setattr(webbrowser, 'open', open_browser)
    path = visualize(directory, open_browser=True)
    assert calls == [path.as_uri()]


@pytest.mark.parametrize('failure', ['false', 'browser', 'os', 'process'])
def test_browser_failure_keeps_html_and_returns_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    """A missing browser cannot discard the report or leak a traceback into the CLI."""
    directory = sample(tmp_path, 'one', monkeypatch)
    original = (directory / 'report.json').read_bytes()

    def fail_open(_url: str) -> bool:
        """Simulate platform browser controllers returning false or raising errors."""
        if failure == 'browser':
            raise webbrowser.Error('fixture browser payload')
        if failure == 'os':
            raise OSError('fixture browser payload')
        if failure == 'process':
            raise subprocess.SubprocessError('fixture browser payload')
        return False

    monkeypatch.setattr(webbrowser, 'open', fail_open)
    monkeypatch.setenv('PYTHONHASHSEED', '42')
    monkeypatch.setattr(sys, 'argv', ['benchmark', 'visualize', '--run', str(directory), '--open-browser', '1'])
    capsys.readouterr()
    assert cli.main() == 1
    error = capsys.readouterr().err
    assert 'Could not open a browser' in error and str(directory / 'report.html') in error
    assert 'Traceback' not in error and 'fixture browser payload' not in error
    assert (directory / 'report.html').is_file()
    assert (directory / 'report.json').read_bytes() == original


def test_open_browser_default_environment_and_cli_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make exports opt-in while an explicit CLI choice takes precedence."""
    monkeypatch.delenv('PRSIM_BM_OPEN_BROWSER', raising=False)
    assert cli.parser().parse_args(['visualize']).open_browser == '0'
    monkeypatch.setenv('PRSIM_BM_OPEN_BROWSER', '1')
    assert cli.parser().parse_args(['visualize']).open_browser == '1'
    assert cli.parser().parse_args(['visualize', '--open-browser', '0']).open_browser == '0'


def test_make_visualize_opens_only_with_explicit_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make defaults to saving HTML and forwards an explicit opt-in to the browser."""
    directory = sample(tmp_path, 'one', monkeypatch)
    marker = tmp_path / 'browser-uri'
    browser = tmp_path / 'browser'
    browser.write_text('#!/bin/sh\nprintf \'%s\' "$1" > "$BROWSER_LOG"\n')
    browser.chmod(0o755)
    monkeypatch.setenv('BROWSER', str(browser))
    monkeypatch.setenv('BROWSER_LOG', str(marker))
    root = Path(__file__).resolve().parents[1]
    for arguments, opened in (([], False), (['OPEN_BROWSER=1'], True)):
        result = subprocess.run(
            ['make', '--no-print-directory', 'visualize', 'RUN=' + str(directory), *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert 'Saved local HTML:' in result.stderr
        assert marker.exists() == opened
    assert marker.read_text() == (directory / 'report.html').as_uri()


@pytest.mark.parametrize('kind,label', [('pull_request', 'Original PR'), ('merge_request', 'Original MR')])
def test_original_change_link_is_inside_task_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, label: str
) -> None:
    """The caption expands the card while the source link uses its authoritative request kind."""
    directory = sample(tmp_path, 'one', monkeypatch)
    report = read_report(directory)
    row = report.tasks[0].model_copy(update={'kind': kind})
    document = _task(row, 0)
    summary, body = document.split('</summary>', 1)
    assert '<a ' not in summary and label in body
    assert 'target="_blank"' in body and 'rel="noopener noreferrer"' in body
