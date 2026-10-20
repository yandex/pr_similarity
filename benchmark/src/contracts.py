"""Packaged document schemas with strict primitive types and safe validation messages."""

import json
from functools import cache
from importlib.resources import files

from benchmark.errors import BenchmarkError
from jsonschema import Draft202012Validator, FormatChecker, validators

_TypeChecker = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _checker, value: type(value) is int)
StrictValidator = validators.extend(Draft202012Validator, type_checker=_TypeChecker)


@cache
def document_validator(kind: str, version: int = 1) -> Draft202012Validator:
    """Load schemas from the installed wheel, independent of cwd."""
    if kind not in {'config', 'report', 'manifest', 'usage', 'pricing', 'basket', 'run', 'state'}:
        raise BenchmarkError('unknown_contract', 'Unsupported document contract.')
    if version not in (1, 2) or (kind != 'basket' and version != 1):
        raise BenchmarkError('unknown_contract', 'Unsupported document version.')
    name = f'benchmark-{kind}-v{version}.schema.json'
    schema = json.loads(files('benchmark').joinpath('schemas', name).read_text(encoding='utf-8'))
    StrictValidator.check_schema(schema)
    return StrictValidator(schema, format_checker=FormatChecker())


def validate_document(kind: str, value: object) -> None:
    """Reject invalid data without including values or snippets in the message."""
    if not isinstance(value, dict):
        raise BenchmarkError('invalid_contract', f'Invalid {kind} document; expected an object.')
    version = 1
    if kind == 'basket':
        meta = value.get('meta') if isinstance(value, dict) else None
        version = meta.get('schema_version') if isinstance(meta, dict) else None
        if type(version) is not int or version not in (1, 2):
            raise BenchmarkError('invalid_contract', 'Basket requires integer schema_version 1 or 2.')
    error = next(document_validator(kind, version).iter_errors(value), None)
    if error is not None:
        path = '.'.join(str(part) for part in error.absolute_path) or '<root>'
        raise BenchmarkError(
            'invalid_contract', f'Invalid {kind} field at {path}; check the v{version} {error.validator} rule.'
        )
