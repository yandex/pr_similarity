"""Generated inference groups and owner labels remain independently observable."""

from collections.abc import Callable

import pytest
from helpers import inference

from viewer.errors import ViewerError
from viewer.presentation.inference import public_inference


def test_projection_keeps_facts_sources_and_drops_arbitrary_private_fields() -> None:
    """Allowlisting applies recursively to inference evidence."""
    value = inference()
    value['raw_config'] = {'Authorization': 'MUST_NOT_LEAK'}
    value['configured']['effort']['headers'] = {'Authorization': 'MUST_NOT_LEAK'}
    projected = public_inference(value)
    assert projected == inference()
    assert 'MUST_NOT_LEAK' not in str(projected)


@pytest.mark.parametrize(
    'change',
    [
        lambda value: value['configured']['model'].update(value=['bad']),
        lambda value: value['configured']['effort'].update(status='unknown'),
        lambda value: value['configured']['model']['source'].update(ref='/private/config.json'),
        lambda value: value['configured']['model']['source'].update(ref='../config.json'),
        lambda value: value.update(discrepancy='yes'),
    ],
)
def test_invalid_projection_has_no_raw_fallback(change: Callable[[dict], None]) -> None:
    """Malformed native metadata cannot escape through a convenience projection."""
    value = inference()
    change(value)
    with pytest.raises((ValueError, ViewerError)):
        public_inference(value)
