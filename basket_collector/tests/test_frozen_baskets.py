"""Published basket compatibility stays separate from current mutable stage storage."""

import hashlib
import json
from pathlib import Path

import pytest

from basket_collector.models.artifacts import BasketDocument
from basket_collector.models.frozen_baskets import BasketV1, BasketV2, validate_frozen_basket
from basket_collector.persistence.artifact_io import StorageError
from basket_collector.persistence.baskets import read_basket
from basket_collector.persistence.publication import PublicationStore
from basket_collector.persistence.storage import read_checkpoint, read_document

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture_path(version: int, provider: str) -> Path:
    """Select immutable historical fixtures or the current producer-owned example."""
    folder = 'basket-contract' if version == 3 else f'published-v{version}'
    return FIXTURES / folder / f'{provider}.json'


@pytest.mark.parametrize('version', [1, 2, 3])
@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_published_basket_keeps_original_bytes_and_typed_fields(version: int, provider: str) -> None:
    """Read each supported frozen contract without rewriting or inventing serialized fields."""
    path = fixture_path(version, provider)
    original = path.read_bytes()
    basket = read_basket(path)
    assert basket.content == original == path.read_bytes()
    assert basket.sha256 == hashlib.sha256(original).hexdigest()
    assert basket.document.meta.schema_version == version
    assert basket.document.model_dump(mode='json', exclude_unset=True) == json.loads(original)
    expected = {1: BasketV1, 2: BasketV2, 3: BasketDocument}[version]
    assert isinstance(basket.document, expected)
    if version < 3:
        hashes = json.loads((path.parent / 'sha256.json').read_text())
        assert basket.sha256 == hashes[path.name]
        assert not isinstance(basket.document, BasketDocument)


@pytest.mark.parametrize('version', [1, 2])
def test_reading_history_never_checks_installed_source(version: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat old producer fingerprints only as recorded data, never resume authority."""
    read_bytes = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        """Allow the published JSON but fail any installed-code or lock inspection."""
        assert path.suffix == '.json'
        return read_bytes(path)

    def forbidden(*args: object, **kwargs: object) -> None:
        """Fail if the reader tries to compute the current producer identity."""
        raise AssertionError('published baskets must not inspect installed producer code')

    monkeypatch.setattr(Path, 'read_bytes', guarded_read)
    monkeypatch.setattr(Path, 'rglob', forbidden)
    monkeypatch.setattr('basket_collector.persistence.storage.current_producer', forbidden)
    assert read_basket(fixture_path(version, 'github')).document.meta.schema_version == version


@pytest.mark.parametrize('version', [1, 2])
def test_published_compatibility_does_not_enable_old_stages(version: int, tmp_path: Path) -> None:
    """Read an old export while rejecting the same bytes as current publication state."""
    path = tmp_path / 'basket.json'
    original = fixture_path(version, 'github').read_bytes()
    path.write_bytes(original)
    read_basket(path)
    with pytest.raises(StorageError, match='version 3'):
        PublicationStore(tmp_path).check_layout()
    with pytest.raises(StorageError, match='invalid document'):
        read_document(path, BasketDocument)
    work = tmp_path / 'legacy-checkpoint.json'
    checkpoint = json.loads(original)
    checkpoint['checkpoint'] = {}
    work.write_text(json.dumps(checkpoint))
    checkpoint_bytes = work.read_bytes()
    with pytest.raises(StorageError, match='invalid checkpoint document'):
        read_checkpoint(work, BasketDocument)
    assert work.read_bytes() == checkpoint_bytes
    assert path.read_bytes() == original
    assert not (tmp_path / '.current').exists()


@pytest.mark.parametrize('version', [None, False, 0, 4, 1.0, '2'])
def test_unsupported_frozen_version_is_not_coerced(version: object) -> None:
    """Reject unknown or noninteger versions without selecting a fallback decoder."""
    value = json.loads(fixture_path(1, 'github').read_bytes())
    value['meta']['schema_version'] = version
    with pytest.raises(ValueError, match='integer schema_version 1, 2 or 3'):
        validate_frozen_basket(value)


@pytest.mark.parametrize('version', [1, 2, 3])
@pytest.mark.parametrize('damage', ['task', 'counts', 'generation', 'status'])
def test_frozen_reader_checks_semantics(version: int, damage: str) -> None:
    """Historical support does not bypass task, count, provenance or completion validation."""
    value = json.loads(fixture_path(version, 'github').read_bytes())
    if damage == 'task':
        value['tasks'][0]['agent_task'] = 'unrelated task'
    elif damage == 'counts':
        value['meta']['counts'].update(input=2, succeeded=2)
    elif damage == 'generation':
        value['tasks'][0]['provenance']['generation']['issue_ids'] = ['unrelated']
    else:
        value['meta'].update(status='running', completed_at=None)
    with pytest.raises(ValueError):
        validate_frozen_basket(value)


@pytest.mark.parametrize('version', [1, 2, 3])
def test_historical_basket_read_warns_once(version: int, caplog: pytest.LogCaptureFixture) -> None:
    """Warn once per historical basket read while leaving current inputs quiet."""
    path = fixture_path(version, 'github')
    content = path.read_bytes()
    read_basket(path)
    warnings = [record for record in caplog.records if record.name == 'basket_collector.basket']
    assert len(warnings) == (1 if version < 3 else 0)
    if warnings:
        assert 'deprecated' in warnings[0].message and f'v{version}' in warnings[0].message
    assert path.read_bytes() == content
