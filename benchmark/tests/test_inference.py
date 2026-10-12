"""Frozen native inference facts keep exact selected options and evidence origins."""

from benchmark.inference import build_inference, generated_groups


def native(effort: str = 'medium', model: str = 'openai/selected') -> dict:
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
    config['provider']['openai']['options']['headers'] = {'Authorization': 'MUST_NOT_LEAK'}
    result = build_inference({'native_config': config})
    assert result['configured']['model']['value'] == 'openai/selected'
    effort = result['configured']['effort']
    assert effort['value'] == 'medium' and effort['status'] == 'known'
    assert effort['source'] == {
        'ref': 'config_identity.json',
        'field': 'native_config.provider.openai.models.selected.options.reasoningEffort',
    }
    assert 'MUST_NOT_LEAK' not in str(result)
    assert generated_groups(result) == {'model': ['openai/selected [medium]'], 'effort': ['medium']}


def test_missing_unknown_and_unsupported_shapes_never_guess_effort() -> None:
    """Missing effort and unsupported variants cannot become a guessed default."""
    result = build_inference({'native_config': {'model': 'openai/absent', 'provider': native()['provider']}})
    assert result['configured']['effort']['status'] == 'unsupported'
    assert result['configured']['effort']['reason'] == 'provider_effort_options_unsupported'
    result = build_inference({'native_config': {'model': 'openai/absent'}})
    assert result['configured']['effort']['status'] == 'unknown'
    result = build_inference({'native_config': {**native(), 'variant': 'custom'}})
    assert result['configured']['effort']['status'] == 'unsupported'
    assert 'effort' not in generated_groups(result)


def test_effective_mixed_retains_each_attempt_and_discrepancy() -> None:
    """Conflicting runtime settings remain per-attempt evidence."""
    result = build_inference(
        {'native_config': native()},
        [
            {'attempt_id': 'a', 'source_ref': 'tasks/x/attempts/1/effective_config.json', 'config': native('low')},
            {'attempt_id': 'b', 'source_ref': 'tasks/x/attempts/2/effective_config.json', 'config': native('high')},
        ],
    )
    assert result['effective']['effort']['status'] == 'mixed'
    assert result['effective']['effort']['value'] is None
    assert [row['effort']['value'] for row in result['effective']['attempts']] == ['low', 'high']
    assert result['effective']['attempts'][1]['effort']['source']['ref'] == 'tasks/x/attempts/2/effective_config.json'
    assert result['discrepancy'] is True


def test_missing_identity_native_config_is_unknown() -> None:
    """An unavailable frozen selection cannot create known generated values."""
    result = build_inference({})
    assert all(row['status'] == 'unknown' for row in result['configured'].values())
    assert generated_groups(result) == {'model': []}
