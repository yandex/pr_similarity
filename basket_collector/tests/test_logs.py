"""Console thresholds, diagnostics and redaction are independently verified."""

import logging
from pathlib import Path

import pytest
from test_filter import collected
from test_history_rejections import history_rejection
from test_pipeline import BASE, _config, _producer

from basket_collector.pipeline import Pipeline, main
from basket_collector.runtime.logs import InvocationLogs


def test_error_level_keeps_debug_storage_and_mandatory_warnings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """INFO can be hidden outside while DEBUG records and warnings remain durable."""
    logs = InvocationLogs(tmp_path, 'ERROR')
    try:
        logging.getLogger('basket_collector.diagnostics').debug('INTERNAL_DETAIL')
        logs.message('ROUTINE_PROGRESS')
        logs.message('ACTIONABLE_WARNING', level='WARNING')
    finally:
        logs.close()
    console = capsys.readouterr()
    assert 'ACTIONABLE_WARNING' in console.out
    assert 'ROUTINE_PROGRESS' not in console.out and 'INTERNAL_DETAIL' not in console.out + console.err
    saved = logs.path.read_text()
    assert 'INTERNAL_DETAIL' in saved and 'ROUTINE_PROGRESS' in saved and 'ACTIONABLE_WARNING' in saved
    assert logs.path.stat().st_mode & 0o777 == 0o600


def test_runtime_details_stay_in_debug_and_credentials_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A full runtime exception is useful in the file, without secret or console leakage."""
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-secret-for-redaction')
    monkeypatch.setattr('basket_collector.pipeline.load_config', lambda _path: _config(tmp_path))

    def failure(*args: object, **kwargs: object) -> None:
        """Fail without any external operation or auth request."""
        raise RuntimeError('PRIVATE_FAILURE_DETAIL synthetic-secret-for-redaction')

    monkeypatch.setattr(Pipeline, 'run', failure)
    assert main(['collect', '--config', str(tmp_path / 'config.yaml'), '--log-level', 'DEBUG']) == 4
    console = capsys.readouterr()
    assert 'Unexpected runtime failure' in console.err and 'DEBUG log:' in console.err
    assert 'PRIVATE_FAILURE_DETAIL' not in console.out + console.err
    assert 'synthetic-secret-for-redaction' not in console.out + console.err
    log = next((tmp_path / 'output').glob('logs_*/collector.log')).read_text()
    assert 'PRIVATE_FAILURE_DETAIL' in log and '<redacted>' in log
    assert 'synthetic-secret-for-redaction' not in log
    assert 'raise RuntimeError' in log


def test_validation_never_dumps_rejected_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Even unknown literal credential fields never enter file or user-facing diagnostics."""
    path = tmp_path / 'config.yaml'
    path.write_text('password: SENSITIVE_LITERAL_REJECTED\n', encoding='utf-8')
    assert main(['collect', '--config', str(path)]) == 2
    console = capsys.readouterr()
    saved = next((tmp_path / 'output').glob('logs_*/collector.log')).read_text()
    assert 'Invalid field' in console.err
    assert 'SENSITIVE_LITERAL_REJECTED' not in console.out + console.err + saved
    assert 'input_value=' not in saved


def test_normal_completed_resume_has_distinct_log_directories(tmp_path: Path) -> None:
    """Two invocations cannot overwrite earlier diagnostic files."""
    first = InvocationLogs(tmp_path)
    first.message('first')
    first.close()
    second = InvocationLogs(tmp_path)
    second.message('second')
    second.close()
    assert first.path != second.path
    assert 'first' in first.path.read_text() and 'second' in second.path.read_text()


@pytest.mark.parametrize('malformed', [False, True])
def test_history_rejection_warning_is_safe_and_identifies_merge_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    malformed: bool,
) -> None:
    """Log only normalized evidence and redact credentials even at ERROR threshold."""
    monkeypatch.setenv('GITLAB_TOKEN', 'synthetic-history-secret')
    rejected = history_rejection(collected())
    change = rejected.change_request.model_copy(
        update={'kind': 'merge_request', 'repository': 'group/synthetic-history-secret'}
    )
    rejected = rejected.model_copy(update={'change_request': change})
    if malformed:
        reason = rejected.reasons[0].model_copy(
            update={'observed': {'missing_commit': 'https://user:password@host/SENSITIVE_BODY', 'depth': True}}
        )
        rejected = rejected.model_copy(update={'reasons': [reason]})
    logs = InvocationLogs(tmp_path, 'ERROR')
    pipeline = Pipeline(_config(tmp_path), producer=_producer(), logs=logs)
    try:
        pipeline._print_history_rejection(rejected, tmp_path / '.work/filter.json')
    finally:
        pipeline.close()
        logs.close()
    console = capsys.readouterr().out
    saved = logs.path.read_text()
    assert 'merge_request group/<redacted> !7 rejected: source_history_unavailable;' in console
    assert '.work/filter.json' in console
    assert ('missing_commit=' + BASE in console) is not malformed
    assert 'synthetic-history-secret' not in console + saved
    assert 'SENSITIVE_BODY' not in console + saved and 'user:password' not in console + saved
