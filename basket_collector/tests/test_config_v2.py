"""Require explicit v2 configuration and independently owned record retry policies."""

from typing import Any

import pytest
from pydantic import ValidationError

from basket_collector.config import CollectorConfig, effective_settings


def configuration() -> dict[str, Any]:
    """Provide a complete minimal v2 issue configuration without external resources."""
    return {
        'schema_version': 2,
        'dataset': {'name': 'fixture', 'description': 'v2', 'maintainers': ['test']},
        'source': {'provider': 'github', 'project': 'owner/repo'},
        'collect': {'selection': 'change_ids', 'change_ids': [1]},
        'generation': {'source': 'issue'},
    }


def test_v2_stage_defaults_and_git_owner() -> None:
    """Give every record stage the same finite defaults and keep Git under filter."""
    config = CollectorConfig.model_validate(configuration())
    for policy in (config.collect, config.filter, config.generation):
        assert (policy.max_retries, policy.retry_delay_seconds, policy.max_retry_delay_seconds) == (3, 10, 120)
    assert config.filter.git.timeout_seconds == 120
    assert 'git' not in config.model_dump()
    assert 'git' not in effective_settings(config, 'filter')
    assert effective_settings(config, 'generate')['max_retries'] == 3


@pytest.mark.parametrize('version', [None, 1, True, 2.0, '2', 3])
def test_only_explicit_integer_version_two_is_accepted(version: object) -> None:
    """Reject absent and legacy versions before environment or transport initialization."""
    data = configuration()
    if version is None:
        del data['schema_version']
    else:
        data['schema_version'] = version
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(data)


@pytest.mark.parametrize('stage', ['collect', 'filter', 'generation'])
@pytest.mark.parametrize(
    'policy',
    [
        {'max_retries': True},
        {'max_retries': -1},
        {'retry_delay_seconds': 0},
        {'retry_delay_seconds': 121, 'max_retry_delay_seconds': 120},
    ],
)
def test_invalid_stage_policies_are_rejected(stage: str, policy: dict[str, object]) -> None:
    """Enforce strict bounded retry limits independently for each stage."""
    data = configuration()
    data.setdefault(stage, {}).update(policy)
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate(data)


@pytest.mark.parametrize('fields', [{'git': {'timeout_seconds': 300}}, {'filter': {'git': {'max_retries': 3}}}])
def test_git_legacy_and_nested_retry_keys_are_rejected(fields: dict[str, object]) -> None:
    """Avoid aliases or hidden transport loops in the v2 configuration."""
    with pytest.raises(ValidationError):
        CollectorConfig.model_validate({**configuration(), **fields})
