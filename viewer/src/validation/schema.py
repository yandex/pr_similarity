"""Package-owned structural schemas independent of semantic document checks."""

from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType

from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import ValidationError

from viewer.errors import ViewerError
from viewer.models import WireModel
from viewer.util import parse_json, require_object

_TypeChecker = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _checker, value: type(value) is int)
StrictValidator = validators.extend(Draft202012Validator, type_checker=_TypeChecker)


SCHEMA_VERSIONS = MappingProxyType({'basket': 3, 'report': 2, 'run': 2, 'state': 2, 'archive': 2, 'usage': 1})


@lru_cache(maxsize=8)
def validator(name: str, version: int | None = None) -> Draft202012Validator:
    """Load the package-owned schema for an explicit consumer contract."""
    expected = SCHEMA_VERSIONS.get(name)
    supported = (
        (1, 2, 3) if name == 'basket' else ((1, 2) if name in ('report', 'run', 'state', 'archive') else (expected,))
    )
    if expected is None or (version is not None and version not in supported):
        raise ViewerError('unsupported_version', 'Unsupported document version; create a new run.')
    version = expected if version is None else version
    schema = require_object(parse_json(files('viewer').joinpath('schemas', f'{name}-v{version}.json').read_bytes()))
    cls = StrictValidator if name == 'basket' else Draft202012Validator
    return cls(schema, format_checker=FormatChecker())


def validate_document(name: str, value: object) -> None:
    """Reject unsupported schema shapes without exposing submitted field values."""
    if isinstance(value, WireModel):
        value = value.to_wire()
    if not isinstance(value, dict):
        raise ViewerError('invalid_' + name, 'Expected a document object.')
    version = SCHEMA_VERSIONS[name]
    try:
        if name == 'basket':
            meta = value.get('meta') if isinstance(value, dict) else None
            candidate = meta.get('schema_version') if isinstance(meta, dict) else None
            if type(candidate) is not int or candidate not in (1, 2, 3):
                raise ViewerError('invalid_basket', 'Basket requires integer schema_version 1, 2 or 3.')
            version = candidate
        elif name in ('report', 'run', 'state', 'archive'):
            candidate = value.get('schema_version')
            version = int(candidate) if type(candidate) in (int, float) and candidate in (1, 2) else 1
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
