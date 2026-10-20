"""Package-owned structural schemas independent of semantic document checks."""

from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import ValidationError
from viewer.errors import ViewerError
from viewer.util import parse_json

_TypeChecker = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _checker, value: type(value) is int)
StrictValidator = validators.extend(Draft202012Validator, type_checker=_TypeChecker)


@lru_cache(maxsize=8)
def validator(name: str, version: int = 1) -> Draft202012Validator:
    """Load the package-owned schema for an explicit consumer contract."""
    schema = parse_json(files('viewer').joinpath('schemas', f'{name}-v{version}.json').read_bytes())
    cls = StrictValidator if name == 'basket' else Draft202012Validator
    return cls(schema, format_checker=FormatChecker())


def validate_document(name: str, value: object) -> None:
    """Reject unsupported schema shapes without exposing submitted field values."""
    if not isinstance(value, dict):
        raise ViewerError('invalid_' + name, 'Expected a document object.')
    try:
        version = 1
        if name == 'basket':
            meta = value.get('meta') if isinstance(value, dict) else None
            version = meta.get('schema_version') if isinstance(meta, dict) else None
            if type(version) is not int or version not in (1, 2):
                raise ViewerError('invalid_basket', 'Basket requires integer schema_version 1 or 2.')
        validator(name, version).validate(value)
    except ValidationError as error:
        names = _field_names(name, version)
        path = '.'.join(
            str(part) if isinstance(part, int) or part in names else '<entry>' for part in error.absolute_path
        )
        raise ViewerError('invalid_' + name, f'Invalid {name} v{version} field at {path or '<root>'}.') from None
    except RecursionError:
        raise ViewerError('invalid_' + name, 'Document nesting exceeds validation limits.') from None


@lru_cache(maxsize=8)
def _field_names(name: str, version: int) -> frozenset[str]:
    """Only schema-owned field names may enter public validation diagnostics."""
    names: set[str] = set()
    pending: list[object] = [validator(name, version).schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            names.update(value.get('properties', {}))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return frozenset(names)
