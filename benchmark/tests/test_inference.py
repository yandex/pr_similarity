"""Frozen native inference facts keep exact selected options and evidence origins."""

from benchmark.json_types import JsonObject
from benchmark.models.evidence import (
    ConfigIdentity,
    EffectiveConfig,
    IdentityInstructions,
    IdentityRetries,
)
from benchmark.presentation.inference import build_inference, generated_groups


def identity(native: JsonObject) -> ConfigIdentity:
    """Embed native test settings in a complete producer identity."""
    return ConfigIdentity(
        identity_version=1,
        runtime='opencode:v1.0.0',
        auth_mode='api_key',
        native_config=native,
        timeout_seconds=1800,
        setup=None,
        instructions=IdentityInstructions(protocol='benchmark_task_v1', sha256='a' * 64),
        resources=[],
        retries=IdentityRetries(max_retries=0, categories=[]),
        diff_excludes=[],
        env={},
    )


def native(effort: str = 'medium', model: str = 'openai/selected') -> JsonObject:
    """Build selected and distracting model settings."""
    return {
        'model': model,
        'provider': {
            'openai': {
                'options': {'reasoningEffort': 'low'},
                'models': {
                    'decoy': {'options': {'reasoningEffort': 'high'}},
                    'selected': {'options': {'reasoningEffort': effort}},
                },
            }
        },
    }


def test_selected_model_effort_and_safe_origins() -> None:
    """Only selected model-local options establish effort; unrelated values never leak."""
    config = native()
    providers = config['provider']
    assert isinstance(providers, dict)
    provider = providers['openai']
    assert isinstance(provider, dict)
    options = provider['options']
    assert isinstance(options, dict)
    options['headers'] = {'Authorization': 'MUST_NOT_LEAK'}
    result = build_inference(identity(config))
    assert result.configured.model.value == 'openai/selected'
    effort = result.configured.effort
    assert effort.value == 'medium' and effort.status == 'known'
    assert effort.source is not None
    assert effort.source.to_wire() == {
        'ref': 'config_identity.json',
        'field': 'native_config.provider.openai.models.selected.options.reasoningEffort',
    }
    assert 'MUST_NOT_LEAK' not in str(result)
    assert generated_groups(result) == {'model': ['openai/selected [medium]'], 'effort': ['medium']}


def test_missing_unknown_and_unsupported_shapes_never_guess_effort() -> None:
    """Missing effort and unsupported variants cannot become a guessed default."""
    result = build_inference(identity({'model': 'openai/absent', 'provider': native()['provider']}))
    assert result.configured.effort.status == 'unsupported'
    assert result.configured.effort.reason == 'provider_effort_options_unsupported'
    result = build_inference(identity({'model': 'openai/absent'}))
    assert result.configured.effort.status == 'unknown'
    result = build_inference(identity({**native(), 'variant': 'custom'}))
    assert result.configured.effort.status == 'unsupported'
    assert 'effort' not in generated_groups(result)


def test_effective_mixed_retains_each_attempt_and_discrepancy() -> None:
    """Conflicting runtime settings remain per-attempt evidence."""
    result = build_inference(
        identity(native()),
        [
            EffectiveConfig(
                attempt_id='a', source_ref='tasks/x/attempts/1/effective_config.json', config=native('low')
            ),
            EffectiveConfig(
                attempt_id='b', source_ref='tasks/x/attempts/2/effective_config.json', config=native('high')
            ),
        ],
    )
    assert result.effective.effort.status == 'mixed'
    assert result.effective.effort.value is None
    assert [row.effort.value for row in result.effective.attempts] == ['low', 'high']
    assert result.effective.attempts[1].effort.source is not None
    assert result.effective.attempts[1].effort.source.ref == 'tasks/x/attempts/2/effective_config.json'
    assert result.discrepancy is True


def test_empty_identity_native_config_is_unknown() -> None:
    """An unavailable frozen selection cannot create known generated values."""
    result = build_inference(identity({}))
    assert all(
        row.status == 'unknown'
        for row in (result.configured.provider, result.configured.model, result.configured.effort)
    )
    assert generated_groups(result) == {'model': []}
