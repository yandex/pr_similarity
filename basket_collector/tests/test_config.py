"""Tests for configuration validation, path resolution, and environment loading."""

import os
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from basket_collector.config import (
    CollectorConfig,
    effective_settings,
    load_config,
    load_environment,
)


def _base(source: str = 'issue') -> dict[str, Any]:
    """Build the smallest valid collector configuration for a generation source."""
    return {
        'schema_version': 2,
        'source': {'provider': 'github', 'project': 'owner/repo'},
        'dataset': {'name': 'sample', 'description': 'fixture', 'maintainers': ['alice']},
        'collect': {'selection': 'change_ids', 'change_ids': [2, 1]},
        'generation': {'source': source},
    }


def test_issue_source_needs_no_llm_or_templates() -> None:
    """Check defaults and semantic settings for direct issue generation."""
    config = CollectorConfig.model_validate(_base())
    assert config.output_dir == './output'
    assert config.cache_dir == './cache'
    assert config.collect.change_ids == [2, 1]
    assert config.collect.default_branch_only is True
    assert cast(dict[str, Any], effective_settings(config, 'collect'))['collect']['default_branch_only'] is True
    assert effective_settings(config, 'filter')['revision_policy'] == 'change_merge_base_v1'
    assert config.generation.max_retries == 3
    assert effective_settings(config, 'generate') == {
        'source': 'issue',
        'max_retries': 3,
        'retry_delay_seconds': 10,
        'max_retry_delay_seconds': 120,
    }
    assert effective_settings(config, 'export')['include_golden_diff'] is False


@pytest.mark.parametrize('change_ids', [[], [1, 1], [True], [0], ['1'], [1, 2, 3]])
def test_invalid_pr_id_lists(change_ids: list[object]) -> None:
    """Reject invalid, duplicate, and over-limit PR ID selections."""
    config = _base()
    config['collect'] = {'selection': 'change_ids', 'change_ids': change_ids, 'max_changes': 2}
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(config)


def test_unknown_keys_and_bad_boundaries() -> None:
    """Reject unknown fields, invalid booleans, and reversed limits."""
    config = _base()
    config['collect']['default_branch_only'] = 'false'
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(config)
    config = _base()
    config['unknown'] = True
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(config)
    config.pop('unknown')
    config['collect'] = {
        'selection': 'date_range',
        'merged_from': '2026-02-01T00:00:00Z',
        'merged_before': '2026-01-01T00:00:00Z',
    }
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(config)
    config = _base()
    config['filter'] = {'size': {'min_lines': 10, 'max_lines': 1}}
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(config)


def test_paths_relative_to_yaml_and_selected_template(tmp_path: Path) -> None:
    """Resolve paths from the YAML directory and load only the selected template."""
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    (prompts / 'system.jinja2').write_text('System', encoding='utf-8')
    (prompts / 'issue.jinja2').write_text('Issue', encoding='utf-8')
    (prompts / 'custom.jinja2').write_text('Custom', encoding='utf-8')
    config_path = tmp_path / 'my config.yaml'
    config_path.write_text(
        'schema_version: 2\ndataset: {name: sample, description: fixture, maintainers: [alice]}\n'
        'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [2]}\n'
        'generation:\n  source: llm\n  context: issue\n'
        '  llm: {model: model-id}\n  templates: {user: ./prompts/custom.jinja2}\n',
        encoding='utf-8',
    )
    config = load_config(config_path)
    assert config.output_dir == str(tmp_path / 'output')
    assert config.generation.templates.user == str(prompts / 'custom.jinja2')
    settings = effective_settings(config, 'generate')
    assert cast(dict[str, Any], settings)['templates']['user']['content'] == 'Custom'
    assert str(tmp_path) not in str(settings)


def test_duplicate_yaml_key_rejected(tmp_path: Path) -> None:
    """Reject duplicate YAML mapping keys before model validation."""
    path = tmp_path / 'config.yaml'
    path.write_text('dataset: {}\ndataset: {}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate YAML key'):
        load_config(path)


def test_environment_falls_back_to_file_without_overriding_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fill missing environment values from .env without replacing process values."""
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        'schema_version: 2\ndataset: {name: sample, description: fixture, maintainers: [alice]}\n'
        'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
        'generation: {source: issue}\n',
        encoding='utf-8',
    )
    (tmp_path / '.env').write_text(
        'BASKET_TEST_FILE_ONLY=file-value\nBASKET_TEST_PRIORITY=file-value\n',
        encoding='utf-8',
    )
    monkeypatch.delenv('BASKET_TEST_FILE_ONLY', raising=False)
    monkeypatch.setenv('BASKET_TEST_PRIORITY', 'process-value')
    config = load_config(config_path)
    assert config.env_file == str(tmp_path / '.env')
    load_environment(config)
    assert os.environ['BASKET_TEST_FILE_ONLY'] == 'file-value'
    assert os.environ['BASKET_TEST_PRIORITY'] == 'process-value'
    monkeypatch.delenv('BASKET_TEST_FILE_ONLY', raising=False)
    load_environment(config.model_copy(update={'env_file': None}))
    assert 'BASKET_TEST_FILE_ONLY' not in os.environ


def test_api_parameter_translation_and_override_rejection() -> None:
    """Translate safe LLM options per API and reject request-level overrides."""
    config = _base('llm')
    config['generation'] = {
        'source': 'llm',
        'llm': {'model': 'chosen', 'parameters': {'max_output_tokens': 20, 'reasoning': {'effort': 'medium'}}},
    }
    parsed = CollectorConfig.model_validate(config)
    assert parsed.generation.llm is not None
    assert parsed.generation.llm.parameters.api_parameters('openai_completions') == {
        'max_completion_tokens': 20,
        'reasoning_effort': 'medium',
    }
    assert parsed.generation.llm.parameters.api_parameters('openai_responses') == {
        'max_output_tokens': 20,
        'reasoning': {'effort': 'medium'},
    }
    config['generation']['llm']['parameters']['messages'] = []
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(config)
