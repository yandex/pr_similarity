"""The CLI classifies malformed input as a configuration error."""

import errno
from pathlib import Path

import pytest
from basket_collector.pipeline import Pipeline, main


@pytest.mark.parametrize(
    ('content', 'expected_context'),
    [
        ('dataset: [\n', 'dataset'),
        (
            'dataset: {name: sample, description: fixture, maintainers: [alice]}\n'
            'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
            'generation: {source: issue}\n'
            'filter:\n  files:\n    allowed_path_patterns: [\'[\']\n',
            'filter.files.allowed_path_patterns',
        ),
        (
            'dataset: {name: sample, description: fixture, maintainers: [alice]}\n'
            'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
            'generation: {source: issue}\n'
            'filter:\n  files:\n    forbidden_path_patterns: [\'[\']\n',
            'filter.files.forbidden_path_patterns',
        ),
        (
            'dataset: {name: sample, description: fixture, maintainers: [alice]}\n'
            'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
            'generation: {source: issue}\n'
            'filter:\n  files:\n    required_path_patterns: [\'[\']\n',
            'filter.files.required_path_patterns',
        ),
    ],
    ids=['yaml', 'allowed', 'forbidden', 'required'],
)
def test_malformed_config_exits_two_with_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
    expected_context: str,
) -> None:
    """Verify malformed YAML and regex settings exit with contextual CLI errors."""
    config_path = tmp_path / 'bad.yaml'
    config_path.write_text(content, encoding='utf-8')

    assert main(['collect', '--config', str(config_path)]) == 2
    error = capsys.readouterr().err
    assert 'configuration or artifact error:' in error
    assert expected_context in error
    if expected_context == 'dataset':
        assert 'line 2, column 1' in error
    assert 'pipeline failed:' not in error


def test_runtime_disk_error_exits_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Classify storage I/O failures during a valid run as infrastructure errors."""
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        'dataset: {name: sample, description: fixture, maintainers: [alice]}\n'
        'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
        'generation: {source: issue}\n',
        encoding='utf-8',
    )

    def fail_run(self: Pipeline, command: str, *, overwrite: bool = False, retry_failed: bool = False) -> int:
        """Emulate a disk write failure after configuration has loaded."""
        raise OSError(errno.ENOSPC, 'disk full')

    monkeypatch.setattr(Pipeline, 'run', fail_run)
    assert main(['collect', '--config', str(config_path)]) == 4
    error = capsys.readouterr().err
    assert 'pipeline failed: Not enough disk space' in error
    assert 'configuration or artifact error:' not in error


@pytest.mark.parametrize('missing', ['config', 'template'])
def test_missing_setup_file_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str) -> None:
    """Preserve configuration errors for missing config and selected template files."""
    config_path = tmp_path / 'config.yaml'
    if missing == 'template':
        (tmp_path / 'prompts').mkdir()
        config_path.write_text(
            'dataset: {name: sample, description: fixture, maintainers: [alice]}\n'
            'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
            'generation: {source: llm, context: diff, llm: {model: fixture}}\n',
            encoding='utf-8',
        )
    assert main(['collect', '--config', str(config_path)]) == 2
    error = capsys.readouterr().err
    assert 'configuration or artifact error:' in error
    assert ('config.yaml' if missing == 'config' else 'system.jinja2') in error
    assert 'pipeline failed:' not in error
