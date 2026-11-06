"""Embedded report evidence remains exact, owned, escaped and usable offline."""

import json
from pathlib import Path

import pytest
from benchmark.errors import BenchmarkError
from benchmark.models.evidence import ArtifactReceipt
from benchmark.models.report import ReportFile, ReportFileExists
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.storage import read_json
from benchmark.presentation import visualize_data
from benchmark.presentation.file_views import file_view
from benchmark.presentation.report import read_report
from benchmark.presentation.visualize import visualize
from benchmark.presentation.visualize_data import embedded_data
from benchmark.runtime import attempt as executor
from test_operation_workflows import stopped_run


def _source_view(before: str | None, after: str | None) -> dict:
    """Render explicit source-role existence through the production file projection."""
    row = ReportFile(
        path='file.py',
        changed_human=True,
        changed_agent=True,
        human_loc=1,
        agent_loc=1,
        loc_weight=1,
        mlp=1.0,
        branch='default',
        base='base' if before is not None else None,
        human='human' if after is not None else None,
        ai='human' if after is not None else None,
        exists=ReportFileExists(base=before is not None, human=after is not None, ai=after is not None),
    )
    sources = {'base': (before or '').encode(), 'human': (after or '').encode()}
    return file_view(row, lambda path, limit: sources[path][:limit], 8 << 20, 20000)


@pytest.mark.parametrize('exists', [True, False])
def test_empty_file_existence_is_not_an_empty_diff(exists: bool) -> None:
    """Added and deleted empty files retain an explicit difference."""
    result = _source_view(None if exists else '', '' if exists else None)
    expected = 'added' if exists else 'deleted'
    assert result['human_diff'] == {
        'text': f'[File {expected}; empty content]\n',
        'truncated': False,
    }


def test_file_diff_preserves_missing_newline_and_full_text() -> None:
    """Displayed diffs retain complete in-limit text and missing-newline markers."""
    changed = '</script>' + 'x' * 20000
    result = _source_view('old\n', changed)['human_diff']
    assert not result['truncated']
    assert '-old\n' in result['text'] and '+' + changed in result['text']
    assert result['text'].endswith('\\ No newline at end of file\n')
    assert _source_view('old\n', 'old\n')['human_diff'] == {
        'text': '',
        'truncated': False,
    }


def test_visualize_embeds_selected_diffs_config_and_harness_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A production-shaped sealed run becomes a standalone interactive document."""
    directory, config, _ = stopped_run(tmp_path, monkeypatch)
    report = read_report(directory)
    original = (directory / 'report.json').read_bytes()
    data = embedded_data(directory, report)
    settings, native = data['benchmark'], data['config']
    task = report.tasks[0]
    file_diffs = data['file_diffs'][task.id][task.scoring.files[0].path]
    assert settings['timeout_seconds'] == config.data.agent.timeout_seconds
    assert native['supported'] is True
    assert native['config']['model'] == 'fixture/model'
    assert file_diffs['human_diff']['text']
    assert file_diffs['agent_diff']['text']
    document = visualize(directory).read_text()
    payload = document.split('<script type="application/json" id="local-data">', 1)[1].split('</script>', 1)[0]
    local = json.loads(payload)
    assert local['file_diffs'] == data['file_diffs']
    assert local['config'] == native
    assert local['run']['benchmark'] == settings
    assert 'function makeFile' in document and 'id="diff-dialog"' in document
    assert 'Run Report' in document and 'Open Human PR' in document
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


def test_missing_declared_usage_receipt_finishes_visualize_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An integrity failure uses the CLI error contract and closes its invocation."""
    original = executor._Attempt.inventory

    def omit_usage(self: executor._Attempt) -> list[ArtifactReceipt]:
        """Simulate an incomplete native inventory without rewriting saved evidence."""
        return [receipt for receipt in original(self) if not receipt.path.endswith('/telemetry/usage.json')]

    monkeypatch.setattr(executor._Attempt, 'inventory', omit_usage)
    directory, _, _ = stopped_run(tmp_path, monkeypatch)
    with pytest.raises(BenchmarkError, match='Declared display usage requires a sealed receipt'):
        visualize(directory)
    invocations = [read_json(path) for path in (directory / '_meta/invocations').glob('*/invocation.json')]
    selected = [row for row in invocations if row['kind'] == 'visualize']
    assert len(selected) == 1
    assert selected[0]['status'] == 'failed'
    assert selected[0]['finished_at'] is not None
    assert not (directory / 'report.html').exists()


@pytest.mark.parametrize('cache', ['missing', 'stale'])
def test_visualize_uses_authoritative_snapshot_without_report_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: str
) -> None:
    """A derived cache cannot prevent viewing otherwise intact saved evidence."""
    directory, _, _ = stopped_run(tmp_path, monkeypatch)
    expected = read_report(directory)
    path = directory / 'report.json'
    if cache == 'missing':
        path.unlink()
    else:
        path.write_text('{}')
    document = visualize(directory).read_text()
    payload = document.split('<script type="application/json" id="local-data">', 1)[1].split('</script>', 1)[0]
    local = json.loads(payload)
    assert local['run']['run_id'] == expected.run.id
    assert [task['task_id'] for task in local['tasks']] == [task.id for task in expected.tasks]
    for row, task in zip(local['tasks'], expected.tasks, strict=True):
        assert row['metrics']['LCf']['score'] == task.scoring.LCf_mlp
    assert path.exists() is (cache == 'stale')
    if path.exists():
        assert path.read_text() == '{}'


def test_local_input_config_respects_viewer_display_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized native input stays unavailable instead of exposing a partial document."""
    directory, _, _ = stopped_run(tmp_path, monkeypatch)
    monkeypatch.setattr(visualize_data, 'CONFIG_DISPLAY_BYTES', 1)
    data = embedded_data(directory, read_report(directory))
    config = data['config']
    assert isinstance(config, dict)
    assert config['supported'] is False
    assert config['config'] is None
    assert config['reason'] == 'display_limit'


def test_local_html_contains_only_bounded_display_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source beyond the display limit must not survive in an unused HTML payload."""
    directory, _, _ = stopped_run(tmp_path, monkeypatch)
    monkeypatch.setattr(visualize_data, 'DIFF_BYTES', 4)
    document = visualize(directory).read_text()
    payload = document.split('<script type="application/json" id="local-data">', 1)[1].split('</script>', 1)[0]
    data = json.loads(payload)
    task = read_report(directory).tasks[0]
    view = data['file_diffs'][task.id][task.scoring.files[0].path]
    assert view['human_diff']['truncated']
    assert len(view['human_diff']['text'].encode()) <= 4
    assert 'human = 2' not in document
    assert document.count('<script type="application/json"') == 1


@pytest.mark.parametrize('field', ['task_id', 'source_path'])
def test_visualize_rejects_unselected_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    """Report identity and file selection must match the authoritative state snapshot."""
    directory, _, _ = stopped_run(tmp_path, monkeypatch)
    report = read_report(directory)
    if field == 'task_id':
        report.tasks[0].id = 'foreign-task'
    else:
        report.tasks[0].scoring.files[0].path = 'foreign.py'
    with pytest.raises(BenchmarkError) as captured:
        embedded_data(directory, report)
    assert captured.value.code == 'report_changed'
