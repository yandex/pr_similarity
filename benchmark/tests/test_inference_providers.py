"""Provider-specific controls describe the selected frozen configuration."""

from copy import deepcopy

import pytest
from test_inference import identity

from benchmark.presentation.inference import build_inference, generated_groups


@pytest.mark.parametrize(
    'provider,sdk,options,field',
    [
        ('openai', None, {'reasoningEffort': 'medium'}, 'reasoningEffort'),
        ('anthropic', None, {'effort': 'medium'}, 'effort'),
        ('openrouter', None, {'reasoning': {'effort': 'medium'}}, 'reasoning.effort'),
        ('custom', '@ai-sdk/openai', {'reasoningEffort': 'medium'}, 'reasoningEffort'),
        ('custom', '@ai-sdk/openai-compatible', {'reasoningEffort': 'medium'}, 'reasoningEffort'),
        ('custom', '@ai-sdk/anthropic', {'effort': 'medium'}, 'effort'),
        ('custom', '@openrouter/ai-sdk-provider', {'reasoning': {'effort': 'medium'}}, 'reasoning.effort'),
    ],
)
def test_selected_sdk_controls_keep_exact_source(provider: str, sdk: str | None, options: dict, field: str) -> None:
    """Aliases resolve by SDK; sibling models cannot supply the selected level."""
    entry: dict = {'models': {'selected': {'options': options}, 'decoy': {'options': {'effort': 'high'}}}}
    if sdk:
        entry['npm'] = sdk
    result = build_inference(identity({'model': provider + '/selected', 'provider': {provider: entry}}))
    effort = result.configured.effort
    assert effort.status == 'known' and effort.value == 'medium'
    assert effort.source is not None
    assert effort.source.field == 'native_config.provider.' + provider + '.models.selected.options.' + field
    assert generated_groups(result) == {'model': [provider + '/selected [medium]'], 'effort': ['medium']}


def test_selected_agent_and_variant_override_model_options() -> None:
    """A variant overrides the selected agent, which overrides model options."""
    native = {
        'model': 'openai/decoy',
        'default_agent': 'coder',
        'agent': {
            'coder': {
                'model': 'openrouter/selected',
                'options': {'reasoning': {'effort': 'low'}},
                'variant': 'careful',
            },
            'unused': {'model': 'anthropic/decoy', 'options': {'effort': 'max'}},
        },
        'provider': {
            'openrouter': {
                'models': {
                    'selected': {
                        'options': {'reasoning': {'effort': 'medium'}},
                        'variants': {'careful': {'reasoning': {'effort': 'high'}}},
                    }
                }
            }
        },
    }
    result = build_inference(identity(native))
    assert result.configured.model.value == 'openrouter/selected'
    assert result.configured.effort.value == 'high'
    assert result.configured.effort.source is not None
    assert result.configured.effort.source.field.endswith('.variants.careful.reasoning.effort')
    del native['agent']['coder']['variant']
    assert build_inference(identity(native)).configured.effort.value == 'low'


@pytest.mark.parametrize('options', [{'thinking': {'type': 'enabled', 'budgetTokens': 16000}}, {}, {'effort': None}])
def test_unknown_effort_has_no_tag_or_brackets(options: dict) -> None:
    """A budget or missing level cannot fabricate a medium label."""
    value = build_inference(
        identity(
            {
                'model': 'anthropic/selected',
                'provider': {'anthropic': {'models': {'selected': {'options': options}}}},
            }
        )
    )
    assert generated_groups(value) == {'model': ['anthropic/selected']}


def test_unselected_agent_overrides_do_not_hide_selected_facts() -> None:
    """Unused agent options cannot erase the primary configuration."""
    value = build_inference(
        identity(
            {
                'model': 'openai/selected',
                'agent': {'unused': {'model': 'anthropic/other', 'options': {'effort': 'max'}}},
                'provider': {'openai': {'models': {'selected': {'options': {'reasoningEffort': 'medium'}}}}},
            }
        )
    )
    assert generated_groups(value)['model'] == ['openai/selected [medium]']


def test_nested_variant_overlay_preserves_untouched_effort() -> None:
    """Deep overlays preserve effort while changing another reasoning option."""
    native = {
        'model': 'openrouter/selected',
        'agent': {'build': {'variant': 'quiet'}},
        'provider': {
            'openrouter': {
                'models': {
                    'selected': {
                        'options': {'reasoning': {'effort': 'medium'}},
                        'variants': {'quiet': {'reasoning': {'exclude': True}}},
                    }
                }
            }
        },
    }
    original = deepcopy(native)
    assert build_inference(identity(native)).configured.effort.value == 'medium'
    assert native == original


@pytest.mark.parametrize('model', ['openai/gpt-5', 'anthropic/claude-opus-4-6', 'openrouter/openai/gpt-5'])
def test_explicit_named_level_preserves_selector_origin(model: str) -> None:
    """A supported built-in level retains its selector as the exact source."""
    value = build_inference(identity({'model': model, 'agent': {'build': {'variant': 'medium'}}}))
    assert value.configured.effort.value == 'medium'
    assert value.configured.effort.source is not None
    assert value.configured.effort.source.field == 'native_config.agent.build.variant'


@pytest.mark.parametrize('variant', ['custom', '', None, 123])
def test_unresolved_variant_does_not_fabricate_effort(variant: str | int | None) -> None:
    """An unknown custom selector cannot become a named effort level."""
    value = build_inference(
        identity(
            {
                'model': 'openai/selected',
                'agent': {'build': {'variant': variant}},
            }
        )
    )
    assert 'effort' not in generated_groups(value)


def test_standard_variant_customization_keeps_its_named_level() -> None:
    """A custom overlay for a built-in variant does not remove its configured effort."""
    value = build_inference(
        identity(
            {
                'model': 'openai/gpt-5',
                'agent': {'build': {'variant': 'medium'}},
                'provider': {
                    'openai': {
                        'models': {
                            'gpt-5': {
                                'options': {'reasoningEffort': 'high'},
                                'variants': {'medium': {'textVerbosity': 'low'}},
                            }
                        }
                    }
                },
            }
        )
    )
    assert value.configured.effort.value == 'medium'
    assert value.configured.effort.source is not None
    assert value.configured.effort.source.field == 'native_config.agent.build.variant'


@pytest.mark.parametrize('model', ['openai/gpt-4o', 'anthropic/claude-haiku-3-5', 'openrouter/opaque'])
def test_unavailable_builtin_level_has_no_effort_tag(model: str) -> None:
    """A named medium selector cannot claim support on an unknown or incompatible model."""
    value = build_inference(identity({'model': model, 'agent': {'build': {'variant': 'medium'}}}))
    assert value.configured.effort.status == 'unsupported'
    assert generated_groups(value) == {'model': [model]}
