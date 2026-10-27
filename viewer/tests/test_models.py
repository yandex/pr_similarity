"""Object model coverage and lossless serialization of validated artifacts."""

import importlib
import inspect
import json
import tarfile
from pathlib import Path

import pytest
from helpers import archive_fixture, documents

from viewer.errors import ViewerError
from viewer.models import (
    ArchiveManifest,
    BasketV1,
    BasketV2,
    BasketV3,
    Report,
    Run,
    State,
    Usage,
    WireModel,
)
from viewer.models.report import ReportBasket
from viewer.models.run import RunBaselineBasket
from viewer.util import JsonValue, json_bytes, parse_json
from viewer.validation.schema import validate_document, validator


@pytest.mark.parametrize(
    ('schema_name', 'version', 'module_name'),
    [
        ('report', 2, 'report'),
        ('run', 2, 'run'),
        ('state', 2, 'state'),
        ('archive', 2, 'archive'),
        ('usage', 1, 'usage'),
        ('basket', 1, 'basket_v1'),
        ('basket', 2, 'basket_v2'),
        ('basket', 3, 'basket_v3'),
    ],
)
def test_declared_object_fields_match_schema(schema_name: str, version: int, module_name: str) -> None:
    """Require every represented object to retain all schema fields and requiredness."""
    schema = validator(schema_name, version).schema
    module = importlib.import_module('viewer.models.' + module_name)
    classes = [
        value
        for _, value in inspect.getmembers(module, inspect.isclass)
        if issubclass(value, WireModel) and value is not WireModel and value.__module__ == module.__name__
    ]
    assert classes
    for model in classes:
        node = schema
        for part in model.schema_pointer.removeprefix('#').split('/'):
            if part:
                assert isinstance(node, (dict, list))
                node = node[int(part)] if isinstance(node, list) else node[part]
        assert isinstance(node, dict)
        fields = {field.alias or name: field for name, field in model.model_fields.items()}
        assert set(fields) == set(node['properties']), model.__name__
        required = set(node.get('required', []))
        if 'python_runtime' in fields:
            # One reader represents v1 (absent) and v2 (schema-required) without rewriting old documents.
            assert 'python_runtime' in required and not fields['python_runtime'].is_required()
            required.remove('python_runtime')
        assert {name for name, field in fields.items() if field.is_required()} == required


@pytest.mark.parametrize(
    ('filename', 'model'),
    [
        ('basket-v1/github.json', BasketV1),
        ('basket-v1/gitlab.json', BasketV1),
        ('basket-v2/github.json', BasketV2),
        ('basket-v2/gitlab.json', BasketV2),
        ('basket.json', BasketV3),
        ('basket-v3/github.json', BasketV3),
        ('basket-v3/gitlab.json', BasketV3),
    ],
)
def test_basket_roundtrip(filename: str, model: type[BasketV1 | BasketV2 | BasketV3]) -> None:
    """Preserve every frozen basket field without rewriting its numeric tokens."""
    raw = json.loads((Path(__file__).parent / 'fixtures' / filename).read_bytes())
    validate_document('basket', raw)
    parsed = model.model_validate(raw)
    assert parsed.tasks[0].id == raw['tasks'][0]['id']
    assert json_bytes(parsed.to_wire()) == json_bytes(raw)


@pytest.mark.parametrize('integer_as_float', [False, True])
def test_report_roundtrip_preserves_numeric_representation(
    integer_as_float: bool,
) -> None:
    """Preserve integral floats accepted by the report JSON Schema validator."""
    raw, _, _ = documents()
    if integer_as_float:
        raw['schema_version'] = 2.0
        raw['run']['revision'] = 2.0
        raw['tasks'][0]['number'] = 1.0
    validate_document('report', raw)
    parsed = Report.model_validate(raw)
    assert parsed.tasks[0].scoring.files[0].path == raw['tasks'][0]['scoring']['files'][0]['path']
    assert json_bytes(parsed.to_wire()) == json_bytes(raw)


def test_absent_optional_fields_remain_absent() -> None:
    """Keep omission distinct from a present null in optional basket fields."""
    raw = json.loads((Path(__file__).parent / 'fixtures/basket.json').read_bytes())
    raw['meta'].pop('schema_family', None)
    raw['meta'].pop('stage', None)
    raw['meta']['input'] = None
    validate_document('basket', raw)
    parsed = BasketV3.model_validate(raw)
    assert parsed.meta.schema_family is None
    assert parsed.meta.input is None
    assert 'schema_family' not in parsed.meta.to_wire()
    assert 'input' in parsed.meta.to_wire()
    assert json_bytes(parsed.to_wire()) == json_bytes(raw)


@pytest.mark.parametrize('open_json_depth', [0, 260, 500])
def test_archive_artifact_roundtrip(tmp_path: Path, open_json_depth: int) -> None:
    """Round-trip actual registry, state, report and inventory fixture artifacts."""
    path = tmp_path / 'run.tar.gz'
    archive_fixture(path)
    models = {
        'report.json': ('report', Report),
        'run.json': ('run', Run),
        '_meta/state.json': ('state', State),
        'manifest.json': ('archive', ArchiveManifest),
    }
    with tarfile.open(path, 'r:gz') as archive:
        for filename, (schema_name, model) in models.items():
            stream = archive.extractfile(filename)
            assert stream is not None
            raw = json.loads(stream.read())
            if schema_name == 'run' and open_json_depth:
                nested: JsonValue = 0
                for _ in range(open_json_depth):
                    nested = {'nested': nested}
                raw['tasks'][0]['basket_row']['extension'] = nested
            raw = parse_json(json_bytes(raw))
            validate_document(schema_name, raw)
            assert json_bytes(model.model_validate(raw).to_wire()) == json_bytes(raw)


def test_usage_roundtrip() -> None:
    """Keep ledger measurements and unknown nullable values exactly as recorded."""
    raw = {
        'schema_family': 'pr_similarity_os.usage',
        'schema_version': 1,
        'run_id': '11111111-1111-4111-8111-111111111111',
        'task_id': 'change:' + 'a' * 64,
        'attempt_id': '22222222-2222-4222-8222-222222222222',
        'runtime': 'opencode',
        'adapter_version': '1',
        'source_db': {'path': 'session.db', 'sha256': 'a' * 64},
        'complete': True,
        'sessions': [],
        'requests': [],
    }
    validate_document('usage', raw)
    assert json_bytes(Usage.model_validate(raw).to_wire()) == json_bytes(raw)


def test_open_json_object_preserves_extra_properties() -> None:
    """Preserve custom fields where schema-owned objects permit extensions."""
    payload = {'custom': {'items': [None, True, 1, 1.0, 'value']}}
    parsed = WireModel.model_validate(payload)
    assert json_bytes(parsed.to_wire()) == json_bytes(payload)


def test_cross_schema_object_equality() -> None:
    """Compare repeated schema fragments by wire content without accepting mapping shims."""
    raw = {
        'sha256': 'a' * 64,
        'path': 'basket.json',
        'partial': False,
        'total_tasks': 1,
        'omitted_failed_count': 0,
    }
    report_basket = ReportBasket.model_validate(raw)
    registry_basket = RunBaselineBasket.model_validate(raw)
    assert report_basket == registry_basket
    assert report_basket != raw
    registry_basket.total_tasks = 2
    assert report_basket != registry_basket
    with pytest.raises(TypeError):
        hash(report_basket)


def test_equality_distinguishes_absent_and_null() -> None:
    """Preserve original JSON object equality when nullable fields are omitted."""
    raw = json.loads((Path(__file__).parent / 'fixtures/basket.json').read_bytes())
    raw['meta'].pop('schema_family', None)
    absent = BasketV3.model_validate(raw)
    present = absent.model_copy(deep=True)
    present.meta.schema_family = None
    assert absent != present


@pytest.mark.parametrize('kind', ['report', 'run', 'state'])
def test_v2_requires_python_reference_field(tmp_path: Path, kind: str) -> None:
    """Legacy optional model defaults never weaken the mandatory v2 ingress schema."""
    archive = tmp_path / 'current.tar.gz'
    archive_fixture(archive)
    name = '_meta/state.json' if kind == 'state' else kind + '.json'
    with tarfile.open(archive, 'r:gz') as source:
        member = source.extractfile(name)
        assert member is not None
        document = json.loads(member.read())
    owner = document if kind == 'state' else document['agent'] if kind == 'report' else document['baseline']['agent']
    del owner['python_runtime']
    with pytest.raises(ViewerError):
        validate_document(kind, document)
