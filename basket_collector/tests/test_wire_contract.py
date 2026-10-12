"""Project-owned frozen-basket conformance is checked without runtime dependencies."""

import copy
import hashlib
import json
from importlib.resources import files
from pathlib import Path

import jsonschema


def test_vendored_basket_schema_matches_project_bytes() -> None:
    """Verify the owning wheel resource is the exact versioned contract snapshot."""
    kit = Path(__file__).resolve().parent / 'fixtures/basket-contract'
    expected = (Path(__file__).resolve().parents[1] / 'src/schemas/basket-v1.schema.json').read_bytes()
    installed = files('basket_collector').joinpath('schemas/basket-v1.schema.json').read_bytes()
    assert installed == expected
    assert hashlib.sha256(expected).hexdigest() == (kit / 'schema.sha256').read_text().strip()


def test_project_positive_and_negative_baskets() -> None:
    """Apply project-owned structural cases without importing the other producer or consumer."""
    kit = Path(__file__).resolve().parent / 'fixtures/basket-contract'
    schema = json.loads(files('basket_collector').joinpath('schemas/basket-v1.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    cases = json.loads((kit / 'cases.json').read_text())
    for name in cases['valid']:
        validator.validate(json.loads((kit / name).read_text()))
    for case in cases['invalid']:
        document = copy.deepcopy(json.loads((kit / case['base']).read_text()))
        target = document
        for key in case['path'][:-1]:
            target = target[key]
        target[case['path'][-1]] = case['value']
        assert list(validator.iter_errors(document)), case['name']
