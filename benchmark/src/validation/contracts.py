"""Packaged document schemas with strict primitive types and safe validation messages."""

import json
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker, validators

from benchmark.errors import BenchmarkError
from benchmark.models.base import WireModel

_TypeChecker = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _checker, value: type(value) is int)
StrictValidator = validators.extend(Draft202012Validator, type_checker=_TypeChecker)


@cache
def document_validator(kind: str, version: int | None = None) -> Draft202012Validator:
    """Load schemas from the installed wheel, independent of cwd."""
    if kind not in {'config', 'report', 'manifest', 'usage', 'pricing', 'basket', 'run', 'state'}:
        raise BenchmarkError('unknown_contract', 'Unsupported document contract.')
    current = 3 if kind == 'basket' else (2 if kind in {'report', 'run', 'state', 'manifest'} else 1)
    version = current if version is None else version
    if (kind == 'basket' and version not in (1, 2, 3)) or (kind != 'basket' and version != current):
        raise BenchmarkError('unknown_contract', 'Unsupported document version.')
    name = f'benchmark-{kind}-v{version}.schema.json'
    schema = json.loads(files('benchmark').joinpath('schemas', name).read_text(encoding='utf-8'))
    StrictValidator.check_schema(schema)
    return StrictValidator(schema, format_checker=FormatChecker())


def validate_document(kind: str, value: object) -> None:
    """Reject invalid data without including values or snippets in the message."""
    if isinstance(value, WireModel):
        value = value.to_wire()
    if not isinstance(value, dict):
        raise BenchmarkError('invalid_contract', f'Invalid {kind} document; expected an object.')
    version = 3 if kind == 'basket' else (2 if kind in {'report', 'run', 'state', 'manifest'} else 1)
    if kind == 'basket':
        meta = value.get('meta') if isinstance(value, dict) else None
        version = meta.get('schema_version') if isinstance(meta, dict) else None
        if type(version) is not int or version not in (1, 2, 3):
            raise BenchmarkError('invalid_contract', 'Basket requires integer schema_version 1, 2 or 3.')
    error = next(document_validator(kind, version).iter_errors(value), None)
    if error is not None:
        path = '.'.join(str(part) for part in error.absolute_path) or '<root>'
        raise BenchmarkError(
            'invalid_contract', f'Invalid {kind} field at {path}; check the v{version} {error.validator} rule.'
        )
