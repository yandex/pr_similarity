"""Embedded report evidence remains exact, owned, escaped and usable offline."""

import json
from pathlib import Path

import pytest
from test_operation_workflows import stopped_run

from benchmark.errors import BenchmarkError
from benchmark.persistence.layout import artifact_path
from benchmark.presentation.report import read_report
from benchmark.presentation.visualize import visualize
from benchmark.presentation.visualize_data import embedded_data, source_diff
from benchmark.vcs.workspace import FileState


@pytest.mark.parametrize('exists', [True, False])
def test_empty_file_existence_is_not_an_empty_diff(exists: bool) -> None:
    """Added and deleted empty files retain an explicit difference."""
    base = FileState(not exists, '', '100644' if not exists else None)
    changed = FileState(exists, '', '100644' if exists else None)
    expected = 'added' if exists else 'deleted'
    assert source_diff(base, changed, 'empty.py', 'human') == f'[File {expected}; empty content]\n'


def test_source_diff_preserves_missing_newline_and_full_text() -> None:
    """Diffs retain source payload and explicitly mark a missing final newline."""
    base = FileState(True, 'old\n', '100644')
    changed = FileState(True, '</script>' + 'x' * 20000, '100644')
    result = source_diff(base, changed, 'file.py', 'ai')
    assert '-old\n' in result and '+' + changed.text in result
    assert result.endswith('\\ No newline at end of file\n')
    assert source_diff(base, base, 'file.py', 'ai') == ''


def test_visualize_embeds_selected_diffs_config_and_harness_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A production-shaped sealed run becomes a standalone interactive document."""
    directory, config, _ = stopped_run(tmp_path, monkeypatch)
    report = read_report(directory)
    original = (directory / 'report.json').read_bytes()
    data = embedded_data(directory, report)
    settings, native, diffs = data['benchmark'], data['config'], data['diffs']
    assert isinstance(settings, dict)
    assert isinstance(native, dict)
    assert isinstance(diffs, dict)
    native_config, task_diffs = native['config'], diffs['0']
    assert isinstance(native_config, dict)
    assert isinstance(task_diffs, dict)
    file_diffs = task_diffs['0']
    assert isinstance(file_diffs, dict)
    assert settings['timeout_seconds'] == config.data.agent.timeout_seconds
    assert native['supported'] is True
    assert native_config['model'] == 'fixture/model'
    assert file_diffs['human_diff']
    assert file_diffs['agent_diff']
    document = visualize(directory).read_text()
    payload = document.split('<script type="application/json" id="view-data">', 1)[1].split('</script>', 1)[0]
    assert json.loads(payload)['diffs'] == data['diffs']
    assert 'class="file"' in document and 'id="diff-dialog"' in document
    assert 'Run Report' in document and 'Original PR' in document
    assert (directory / 'report.json').read_bytes() == original


def test_corrupted_source_is_not_embedded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed source bytes fail validation instead of producing a misleading diff."""
    directory, _, _ = stopped_run(tmp_path, monkeypatch)
    report = read_report(directory)
    source = report.tasks[0].scoring.files[0].human
    assert source is not None
    artifact_path(directory, source, must_exist=True).write_text('tampered source')
    with pytest.raises(BenchmarkError, match='damaged|digest|receipt'):
        embedded_data(directory, report)
