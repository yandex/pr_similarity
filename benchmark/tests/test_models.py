"""Producer models stay aligned with current schemas and preserve artifact values."""

import importlib
import inspect
from pathlib import Path

import pytest
from test_inference import identity
from test_run_store import fresh_store

from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonValue
from benchmark.models import WireModel
from benchmark.models.evidence import ConfigIdentity, OperationDetails
from benchmark.models.report import ReportTelemetryCost
from benchmark.persistence.storage import canonical_bytes, parse_json
from benchmark.validation.contracts import document_validator, validate_document


@pytest.mark.parametrize(
    ('schema_name', 'module_name'),
    [
        ('report', 'report'),
        ('run', 'run'),
        ('state', 'state'),
        ('manifest', 'archive'),
        ('usage', 'usage'),
        ('pricing', 'pricing'),
        ('basket', 'basket'),
    ],
)
def test_schema_fields_and_requiredness(schema_name: str, module_name: str) -> None:
    """Keep all complete object declarations synchronized with their owning schemas."""
    schema = document_validator(schema_name).schema
    module = importlib.import_module('benchmark.models.' + module_name)
    for _, model in inspect.getmembers(module, inspect.isclass):
        if (
            not issubclass(model, WireModel)
            or model.__module__ != module.__name__
            or model.__name__ in ('TaskDraft', 'ReportDraft')
        ):
            continue
        version = {
            'LegacyProducer': 1,
            'LegacyLineageReceipt': 1,
            'GenerationV1': 1,
            'BasketMetaV1': 1,
            'BasketV1': 1,
            'BasketMetaV2': 2,
            'BasketV2': 2,
        }.get(model.__name__)
        node = document_validator('basket', version).schema if schema_name == 'basket' and version else schema
        for part in model.schema_pointer.removeprefix('#').split('/'):
            if part:
                assert isinstance(node, (dict, list))
                node = node[int(part)] if isinstance(node, list) else node[part]
        assert isinstance(node, dict)
        fields = {field.alias or name: field for name, field in model.model_fields.items()}
        assert set(fields) == set(node['properties']), model.__name__
        assert {name for name, field in fields.items() if field.is_required()} == set(node.get('required', []))


@pytest.mark.parametrize('depth', [260, 500])
def test_open_native_json_preserves_existing_parser_depth(depth: int) -> None:
    """Do not impose a recursive-union ceiling on already parsed native JSON."""
    nested: JsonValue = 1
    for _ in range(depth):
        nested = {'nested': nested}
    original = identity({'extension': nested}).to_wire()
    raw = parse_json(canonical_bytes(original))
    parsed = ConfigIdentity.model_validate(raw)
    assert canonical_bytes(parsed) == canonical_bytes(raw)


def test_optional_absent_and_explicit_null_remain_distinct() -> None:
    """Optional operation details must not silently expand an empty receipt."""
    assert OperationDetails().to_wire() == {}
    assert OperationDetails(error=None).to_wire() == {'error': None}
    assert OperationDetails() != OperationDetails(error=None)


@pytest.mark.parametrize('amount', [0, 0.0, 0.125])
def test_number_serialization_keeps_input_representation(amount: int | float) -> None:
    """Retain exact JSON integer versus floating tokens in number-valued measurements."""
    raw = {'amount': amount, 'currency': 'USD', 'source': 'opencode'}
    assert canonical_bytes(ReportTelemetryCost.model_validate(raw)) == canonical_bytes(raw)


def test_authoritative_schema_rejects_integral_float_state_revision(tmp_path: Path) -> None:
    """Benchmark integer policy remains strict before any model conversion occurs."""
    store = fresh_store(tmp_path)
    raw = store.state.to_wire()
    raw['revision'] = 1.0
    with pytest.raises(BenchmarkError, match='revision'):
        validate_document('state', raw)
