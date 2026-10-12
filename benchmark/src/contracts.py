"""Packaged v1 schemas with strict primitive types and safe validation messages."""

import json
from functools import cache
from importlib.resources import files

from benchmark.errors import BenchmarkError
from jsonschema import Draft202012Validator, FormatChecker, validators

_TypeChecker = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _checker, value: type(value) is int)
StrictValidator = validators.extend(Draft202012Validator, type_checker=_TypeChecker)


@cache
def document_validator(kind: str) -> Draft202012Validator:
    """Load schemas from the installed wheel, independent of cwd."""
    if kind not in {'config', 'report', 'manifest', 'usage', 'pricing', 'basket', 'run', 'state'}:
        raise BenchmarkError('unknown_contract', 'Unsupported document contract.')
    name = f'benchmark-{kind}-v1.schema.json'
    schema = json.loads(files('benchmark').joinpath('schemas', name).read_text(encoding='utf-8'))
    StrictValidator.check_schema(schema)
    return StrictValidator(schema, format_checker=FormatChecker())


def validate_document(kind: str, value: dict) -> None:
    """Reject invalid data without including values or snippets in the message."""
    error = next(document_validator(kind).iter_errors(value), None)
    if error is not None:
        path = '.'.join(str(part) for part in error.absolute_path) or '<root>'
        raise BenchmarkError(
            'invalid_contract', f'Invalid {kind} field at {path}; check the v1 {error.validator} rule.'
        )
