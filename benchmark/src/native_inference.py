"""Resolve explicitly selected OpenCode controls without retaining private configuration."""

import re

_FIELDS = ('provider', 'model', 'effort')
_TOKEN = re.compile(r'^[A-Za-z0-9_.:/-]+$')
_SDKS = {'openai': '@ai-sdk/openai', 'anthropic': '@ai-sdk/anthropic', 'openrouter': '@openrouter/ai-sdk-provider'}
_CONTROLS = {
    '@ai-sdk/openai': ('reasoningEffort',),
    '@ai-sdk/openai-compatible': ('reasoningEffort',),
    '@ai-sdk/anthropic': ('effort',),
    '@openrouter/ai-sdk-provider': ('reasoning', 'effort'),
}
_LEVELS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}


def fact(status: str, value: str | None, ref: str | None, field: str, reason: str | None = None) -> dict:
    """Retain the exact safe value and source of one inference control."""
    return {
        'status': status,
        'value': value,
        'reason': reason,
        'source': {'ref': ref, 'field': field} if ref is not None else None,
    }


def identifier(value: object, maximum: int = 256) -> bool:
    """Accept bounded identifiers without whitespace or control characters."""
    return isinstance(value, str) and 0 < len(value) <= maximum and _TOKEN.fullmatch(value) is not None


def _selection(native: dict, prefix: str) -> tuple[object, str, dict, str]:
    """Resolve the selected primary agent; ignore all sibling agent settings."""
    name = native.get('default_agent', 'build')
    agents = native.get('agent', {})
    if not identifier(name) or not isinstance(agents, dict):
        raise ValueError('agent_selection_unsupported')
    agent = agents.get(name, {})
    if not isinstance(agent, dict) or agent.get('disable') or agent.get('hidden') or agent.get('mode') == 'subagent':
        raise ValueError('agent_selection_unsupported')
    if name not in ('build', 'plan') and name not in agents:
        raise ValueError('agent_selection_unsupported')
    field = prefix + 'agent.' + name
    model = agent.get('model') or native.get('model')
    return model, field + '.model' if agent.get('model') else prefix + 'model', agent, field


def _overlay(target: dict, sources: dict, value: dict, field: str) -> None:
    """Merge options deeply while preserving the source of every selected leaf."""
    for key, item in value.items():
        if isinstance(item, dict):
            if not isinstance(target.get(key), dict):
                target[key], sources[key] = {}, {}
            _overlay(target[key], sources[key], item, field + '.' + key)
        else:
            target[key], sources[key] = item, field + '.' + key


def _builtin_level(model: dict, selected: str, control: tuple[str, ...], level: str) -> bool:
    """Recognize deterministic built-in levels; unknown catalogue capabilities stay unknown."""
    api = model.get('id', selected)
    if not isinstance(api, str) or model.get('reasoning') is False:
        return False
    api = api.lower()
    if control == ('effort',):
        if 'claude' not in api:
            return False
        adaptive = re.search(r'(?:opus|sonnet)[.-](?:4[.-][6-9]|[5-9])', api)
        if adaptive:
            return level in {'low', 'medium', 'high', 'max'}
        if 'opus-4-5' in api or 'opus-4.5' in api:
            return level in {'low', 'medium', 'high'}
        return model.get('reasoning') is True and level in {'high', 'max'}
    # These families have named reasoning variants in the pinned native adapter.
    # Do not manufacture levels for arbitrary aliases or nonreasoning models.
    reasoning = re.search(r'(?:^|/)(?:gpt-[5-9](?:[.-]|$)|o[1-9](?:[.-]|$))', api)
    if reasoning:
        if 'deep-research' in api:
            return level == 'medium'
        if '-chat' in api:
            return level == 'medium' and re.search(r'gpt-5[.-]\d+', api) is not None
        if re.search(r'gpt-5(?:[.-]\d+)?[.-]?pro(?:[.-]|$)', api):
            return level == 'high'
        return level in {'low', 'medium', 'high'}
    excluded = any(name in api for name in ('deepseek', 'minimax', 'kimi', 'qwen', 'big-pickle', 'glm'))
    return (
        not excluded
        and control == ('reasoning', 'effort')
        and model.get('reasoning') is True
        and level in {'low', 'medium', 'high'}
    )


def _options(
    model: dict, agent: dict, model_field: str, agent_field: str, control: tuple[str, ...], selected: str
) -> tuple[dict, dict]:
    """Apply model, selected-agent and selected-variant options in runtime order."""
    options, sources = {}, {}
    for value, field in [
        (model.get('options', {}), model_field + '.options'),
        (agent.get('options', {}), agent_field + '.options'),
    ]:
        if not isinstance(value, dict):
            raise ValueError('options_not_object')
        _overlay(options, sources, value, field)
    variant = agent.get('variant')
    if variant is None:
        return options, sources
    variants = model.get('variants', {})
    if not identifier(variant, 64) or not isinstance(variants, dict):
        raise ValueError('variant_selection_unsupported')
    definition = variants.get(variant)
    if definition is not None and (not isinstance(definition, dict) or definition.get('disabled')):
        raise ValueError('variant_selection_unsupported')
    if _builtin_level(model, selected, control, variant):
        # An explicit named level supplies the built-in overlay before its
        # custom definition. It is not a conversion of an arbitrary budget.
        value, origin = options, sources
        for part in control[:-1]:
            if not isinstance(value.get(part), dict):
                value[part], origin[part] = {}, {}
            value, origin = value[part], origin[part]
        value[control[-1]], origin[control[-1]] = variant, agent_field + '.variant'
    elif definition is None:
        raise ValueError('variant_definition_unavailable')
    if definition is None:
        return options, sources
    _overlay(options, sources, definition, model_field + '.variants.' + variant)
    return options, sources


def _effort(native: dict, provider: str, selected: str, agent: dict, agent_field: str, ref: str, prefix: str) -> dict:
    """Interpret the selected SDK's control; SDK connection options are not model options."""
    field = prefix + f'provider.{provider}'
    providers = native.get('provider', {})
    entry = providers.get(provider, {}) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        return fact('unsupported', None, ref, field, 'provider_options_not_object')
    models = entry.get('models', {})
    model = models.get(selected, {}) if isinstance(models, dict) else None
    model_field = field + '.models.' + selected
    if not isinstance(model, dict):
        return fact('unsupported', None, ref, model_field, 'selected_model_options_not_object')
    transport = model.get('provider', {})
    if not isinstance(transport, dict):
        return fact('unsupported', None, ref, model_field + '.provider', 'sdk_selection_unsupported')
    sdk = transport.get('npm', entry.get('npm', _SDKS.get(provider, '@ai-sdk/openai-compatible')))
    control = _CONTROLS.get(sdk) if isinstance(sdk, str) else None
    if control is None:
        return fact('unsupported', None, ref, field + '.npm', 'sdk_selection_unsupported')
    effort_field = model_field + '.options.' + '.'.join(control)
    try:
        options, sources = _options(model, agent, model_field, agent_field, control, selected)
    except ValueError as error:
        return fact(
            'unsupported', None, ref, agent_field + '.variant' if agent.get('variant') else effort_field, str(error)
        )
    value, source = options, sources
    for part in control:
        if not isinstance(value, dict):
            return fact(
                'unsupported',
                None,
                ref,
                source if isinstance(source, str) else effort_field,
                'effort_shape_unsupported',
            )
        if part not in value:
            sdk_options = entry.get('options', {})
            if isinstance(sdk_options, dict) and any(
                key in sdk_options for key in ('reasoningEffort', 'reasoning', 'effort')
            ):
                return fact(
                    'unsupported',
                    None,
                    ref,
                    field + '.options.' + '.'.join(control),
                    'provider_effort_options_unsupported',
                )
            return fact('unknown', None, ref, effort_field, 'effort_not_configured')
        value, source = value[part], source[part]
    assert isinstance(source, str)
    if not isinstance(value, str) or not identifier(value, 64) or value not in _LEVELS:
        return fact('unsupported', None, ref, source, 'effort_value_unsupported')
    return fact('known', value, ref, source)


def extract(native: object, ref: str, prefix: str = '') -> dict:
    """Resolve configured facts without guessing runtime defaults or scanning sibling models."""
    field = prefix + 'model'
    if native is None:
        return {key: fact('unknown', None, ref, field, 'model_not_configured') for key in _FIELDS}
    if not isinstance(native, dict):
        return {key: fact('unsupported', None, ref, field, 'native_config_not_object') for key in _FIELDS}
    try:
        model, field, agent, agent_field = _selection(native, prefix)
    except ValueError as error:
        return {key: fact('unsupported', None, ref, prefix + 'default_agent', str(error)) for key in _FIELDS}
    if model is None:
        return {key: fact('unknown', None, ref, field, 'model_not_configured') for key in _FIELDS}
    if not isinstance(model, str) or not identifier(model) or '/' not in model:
        return {key: fact('unsupported', None, ref, field, 'explicit_provider_model_required') for key in _FIELDS}
    assert isinstance(model, str)
    provider, selected = model.split('/', 1)
    if not provider or not selected:
        return {key: fact('unsupported', None, ref, field, 'explicit_provider_model_required') for key in _FIELDS}
    result = {'provider': fact('known', provider, ref, field), 'model': fact('known', model, ref, field)}
    if native.get('variant') is not None:
        result['effort'] = fact('unsupported', None, ref, prefix + 'variant', 'variant_or_agent_selection_unsupported')
    else:
        result['effort'] = _effort(native, provider, selected, agent, agent_field, ref, prefix)
    return result
