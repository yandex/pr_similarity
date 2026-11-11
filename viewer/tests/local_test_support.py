"""Build validated publications and typed inputs shared by local-storage tests."""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from helpers import archive_fixture
from viewer.bundles import BundleIdentity, BundleRepository
from viewer.config import Config
from viewer.errors import StorageError
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore
from viewer.publication import CatalogueEntry, PublicationReader, StagedPublication
from viewer.service import ViewerService
from viewer.util import digest
from viewer.validation.archive import ImportedRun, validate_archive

RUN_ID = '11111111-1111-4111-8111-111111111111'


@dataclass(frozen=True)
class ValidatedPublication:
    """Keep exact archive inputs beside the catalogue projection built from them."""

    imported: ImportedRun
    marker: dict
    metadata: dict
    entry: CatalogueEntry


def build_publication(root: Path, *, owner: str = 'alice', run_id: str | None = None) -> ValidatedPublication:
    """Create a real archive and validate its evidence through the publication reader."""
    root.mkdir(parents=True, exist_ok=True)
    config = Config(storage='mock', runtime=root / 'runtime')
    archive = root / 'run.tar.gz'
    archive_fixture(archive, run_id=run_id)
    imported = validate_archive(archive, root / 'source' / 'data', config.limits)
    manifest = imported.directory / 'manifest.json'
    marker = {
        'version': 1,
        'owner': owner,
        'run_id': imported.report.run.id,
        'basket_sha256': imported.report.basket.sha256,
        'report_sha256': imported.manifest.report_sha256,
        'archive_sha256': imported.archive_sha256,
        'archive_size': archive.stat().st_size,
        'published_at': '2026-11-10T00:00:00Z',
        'operation_id': 'fixture',
        'inventory': [
            *[item.to_wire() for item in imported.manifest.files],
            {'path': 'manifest.json', 'size_bytes': manifest.stat().st_size, 'sha256': digest(manifest.read_bytes())},
        ],
    }
    metadata = {'display_name': 'Run', 'tags': ['setup'], 'pinned': False, 'created_at': '2026-11-10T00:00:00Z'}
    prefix = f'runs/{owner}/{imported.report.run.id}/'
    source = StagedPublication(
        {'prefix': prefix, 'marker': marker, 'staging': str(root / 'source')}, MemoryStore(), config.limits.path_bytes
    )
    entry = PublicationReader(config, source)._marker(prefix + 'publication.json')
    return ValidatedPublication(imported, marker, metadata, entry)


class FaultStore(MemoryStore):
    """Record writes and simulate rejected requests or lost successful responses."""

    def __init__(self) -> None:
        """Start with an empty store and disabled fault injection."""
        super().__init__()
        self.writes: list[str] = []
        self.fail: str | None = None
        self.lost_reply = False

    def put(self, key: str, data: bytes) -> None:
        """Fail before storage for selected keys or after storage for lost replies."""
        self.writes.append(key)
        if self.fail and self.fail in key:
            raise StorageError()
        super().put(key, data)
        if self.lost_reply:
            raise StorageError()


class BundleFixture(NamedTuple):
    """Expose the repository and exact publication inputs without pytest coupling."""

    bundles: BundleRepository
    store: FaultStore
    identity: BundleIdentity
    imported: ImportedRun
    marker: dict
    metadata: dict
    entry: CatalogueEntry


def build_bundle_fixture(root: Path) -> BundleFixture:
    """Initialize an offline registry around one fully validated publication."""
    publication = build_publication(root)
    config = Config(storage='mock', runtime=root / 'runtime')
    store = FaultStore()
    bundles = BundleRepository(config, store)
    bundles.initialize_registry()
    identity = BundleIdentity('alice', publication.imported.report.run.id, 1, publication.imported.archive_sha256)
    return BundleFixture(
        bundles, store, identity, publication.imported, publication.marker, publication.metadata, publication.entry
    )


def build_legacy_service(root: Path, store: MemoryStore) -> ViewerService:
    """Create the legacy writer with an existing Alice account for migration tests."""
    config = Config(runtime=root / 'runtime', storage='mock')
    database = Database(config.runtime)
    database.create_user('alice', 'secret')
    return ViewerService(config, store, database)


def publish_fixture(service: ViewerService, root: Path, *, run_id: str | None = None) -> tuple[dict, dict, ImportedRun]:
    """Publish validated fixture bytes with a fresh operation key through the legacy API."""
    operation = str(uuid.uuid4())
    publication = build_publication(root / operation, run_id=run_id)
    receipt = service.publish(publication.imported, 'alice', operation, overwrite=bool(run_id))
    return publication.imported.report.to_wire(), receipt, publication.imported
