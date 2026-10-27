"""Project-owned frozen-basket conformance is checked without runtime dependencies."""

import copy
import json
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest


@pytest.mark.parametrize('version', [1, 2, 3])
def test_vendored_basket_schema_matches_project_bytes(version: int) -> None:
    """Verify the owning wheel resource is the exact versioned contract snapshot."""
    expected = (
        Path(__file__).resolve().parents[1] / f'src/schemas/benchmark-basket-v{version}.schema.json'
    ).read_bytes()
    installed = files('benchmark').joinpath(f'schemas/benchmark-basket-v{version}.schema.json').read_bytes()
    assert installed == expected


@pytest.mark.parametrize('version', [1, 2, 3])
def test_project_positive_and_negative_baskets(version: int) -> None:
    """Apply project-owned structural cases without importing the other producer or consumer."""
    kit = Path(__file__).resolve().parent / 'fixtures/basket-contract'
    schema = json.loads(files('benchmark').joinpath(f'schemas/benchmark-basket-v{version}.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    cases = json.loads((kit / 'cases.json').read_text())
    fixtures = kit.parent / f'basket-v{version}'
    for name in cases['valid']:
        validator.validate(json.loads((fixtures / name).read_text()))
    for case in cases['invalid']:
        document = copy.deepcopy(json.loads((fixtures / case['base']).read_text()))
        target = document
        for key in case['path'][:-1]:
            target = target[key]
        target[case['path'][-1]] = 99 if case['name'] == 'unsupported_version' else case['value']
        assert list(validator.iter_errors(document)), case['name']
