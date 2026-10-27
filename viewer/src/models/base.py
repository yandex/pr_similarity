"""Lossless object representation for JSON Schema validated artifacts."""

from types import NotImplementedType
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, SkipValidation

from viewer.util import JsonValue


def json_value(value: object) -> JsonValue:
    """Check serialized output without coercing JSON numeric or scalar types."""
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, (str, bool, int, float)):
            continue
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise TypeError('JSON object keys must be strings')
            pending.extend(item.values())
        else:
            raise TypeError(f'Unsupported JSON value: {type(item).__name__}')
    if value is None or isinstance(value, (str, bool, int, float, list, dict)):
        return value
    raise TypeError(f'Unsupported JSON value: {type(value).__name__}')


def _identity_json(value: JsonValue) -> JsonValue:
    """Preserve an already validated JSON subtree during Python serialization."""
    return value


# Only use these aliases after the existing JSON parser and owning schema have
# validated the input. Open JSON pockets must not acquire Pydantic's separate
# recursive-union depth ceiling. Concrete object fields still parse normally.
type ValidatedJsonValue = Annotated[SkipValidation[JsonValue], PlainSerializer(_identity_json, return_type=object)]
type ValidatedJsonObject = dict[str, ValidatedJsonValue]


class WireModel(BaseModel):
    """Expose typed attributes while retaining the original JSON representation.

    JSON Schema validation must run before parsing these objects. Constraints
    such as formats, conditional requirements and numeric bounds belong to that
    authoritative validation boundary. Optional absent fields remain absent on
    serialization, and unknown properties allowed by a schema remain intact.
    """

    model_config = ConfigDict(strict=True, extra='allow', protected_namespaces=())
    schema_pointer: ClassVar[str] = '#'
    __pydantic_extra__: dict[str, ValidatedJsonValue] = Field(init=False)

    def __eq__(self, other: object) -> bool | NotImplementedType:
        """Compare artifact content across independently declared schema fragments."""
        if not isinstance(other, WireModel):
            return NotImplemented
        return self.to_wire() == other.to_wire()

    def to_wire(self) -> dict[str, JsonValue]:
        """Return a checked JSON object preserving aliases and absent fields."""
        result = json_value(self.model_dump(mode='python', by_alias=True, exclude_unset=True))
        if not isinstance(result, dict):
            raise TypeError('Artifact serialization must produce a JSON object')
        return result
