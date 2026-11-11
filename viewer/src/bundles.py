"""Immutable archive backups with separately discoverable closing manifests."""

import hashlib
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.database import LOGIN
from viewer.persistence.storage import Store
from viewer.publication import PublicationReader
from viewer.util import digest, json_bytes, parse_json, safe_path, utc_timestamp
from viewer.validation.archive import CHUNK, ImportedRun, validate_archive
from viewer.validation.contracts import require

INDEX = 'bundle-index/v1/'
DATA = 'bundle-data/v1/'
REGISTRY = 'bundle-registry/v1/'
REGISTRY_HEAD = REGISTRY + 'head.json'
RESERVATIONS = REGISTRY + 'reservations.json'


def _sha(value: object) -> bool:
    """Recognize canonical SHA-256 identities."""
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _json_object(data: bytes, code: str) -> dict:
    """Decode a bounded JSON object while retaining the caller's contract error code."""
    value = parse_json(data)
    require(isinstance(value, dict), code)
    return cast(dict, value)


class BundleVerificationError(ViewerError):
    """Identify the failed backup object without exposing storage internals."""

    def __init__(self, error: ViewerError, component: str) -> None:
        """Retain a viewer failure code while identifying the damaged bundle component."""
        super().__init__(error.code, error.message, error.status)
        self.component = component


@dataclass(frozen=True)
class BundleIdentity:
    """Fence a backup by owner, generation, exact archive and metadata revision."""

    owner: str
    run_id: str
    generation: int
    archive_sha256: str
    metadata_revision: int = 1

    def __post_init__(self) -> None:
        """Validate the canonical owner, UUID, archive digest and monotonic revision values."""
        require(
            isinstance(self.owner, str) and LOGIN.fullmatch(self.owner),
            'invalid_bundle',
        )
        try:
            require(str(uuid.UUID(self.run_id)) == self.run_id, 'invalid_bundle')
        except (ValueError, TypeError, AttributeError):
            raise ViewerError('invalid_bundle', 'Invalid bundle identity.') from None
        require(
            type(self.generation) is int and 0 < self.generation < 2**63,
            'invalid_bundle',
        )
        require(
            type(self.metadata_revision) is int and 0 < self.metadata_revision < 2**63,
            'invalid_bundle',
        )
        require(_sha(self.archive_sha256), 'invalid_bundle')

    def to_wire(self) -> dict:
        """Serialize only the public backup identity."""
        return asdict(self)

    @classmethod
    def from_wire(cls, value: dict) -> 'BundleIdentity':
        """Reject unknown fields before rebuilding an identity."""
        require(
            isinstance(value, dict)
            and set(value) == {'owner', 'run_id', 'generation', 'archive_sha256', 'metadata_revision'},
            'invalid_bundle',
        )
        return cls(**value)

    @property
    def prefix(self) -> str:
        """Address the owner and run generation shared by its immutable bundle objects."""
        return f'{self.owner}/{self.run_id}/{self.generation}/'

    @property
    def closing_key(self) -> str:
        """Address the closure that confirms one exact metadata revision."""
        return f'{INDEX}{self.prefix}{self.metadata_revision}.json'

    @property
    def archive_key(self) -> str:
        """Address the original archive shared by this generation's metadata revisions."""
        return f'{DATA}{self.prefix}{self.archive_sha256}/archive.tar.gz'

    @property
    def metadata_key(self) -> str:
        """Address the immutable metadata document for this revision."""
        return f'{DATA}{self.prefix}metadata/{self.metadata_revision}.json'


@dataclass(frozen=True)
class BundleDescriptor:
    """Describe a closed backup; marker retains the strict publication v1 shape."""

    identity: BundleIdentity
    marker: dict
    metadata: dict
    archive_size: int
    tombstone: bool = False

    @property
    def manifest_key(self) -> str:
        """Return the closing object key bound to this verified bundle identity."""
        return self.identity.closing_key

    def to_wire(self) -> dict:
        """Serialize a discovery-plan entry without storage credentials."""
        return asdict(self)

    @classmethod
    def from_wire(cls, value: dict) -> 'BundleDescriptor':
        """Rebuild an audit-plan entry; reads still revalidate remote closure."""
        require(
            isinstance(value, dict) and set(value) == {'identity', 'marker', 'metadata', 'archive_size', 'tombstone'},
            'invalid_bundle',
        )
        require(
            isinstance(value['marker'], dict)
            and isinstance(value['metadata'], dict)
            and type(value['archive_size']) is int
            and value['archive_size'] >= 0
            and type(value['tombstone']) is bool,
            'invalid_bundle',
        )
        return cls(
            BundleIdentity.from_wire(value['identity']),
            value['marker'],
            value['metadata'],
            value['archive_size'],
            value['tombstone'],
        )


@dataclass(frozen=True)
class DownloadedBundle:
    """Return validated original bytes, extracted evidence and presentation data."""

    imported: ImportedRun
    marker: dict
    metadata: dict
    descriptor: BundleDescriptor


@dataclass(frozen=True)
class BundleDiscoveryError:
    """Record an unreadable closing entry without hiding other publications."""

    key: str
    code: str


@dataclass(frozen=True)
class BundleRegistrySnapshot:
    """One authoritative complete set of latest closed publication references."""

    revision: int
    snapshot_sha256: str
    head_bytes: bytes
    entries: tuple[dict, ...]


class BundleRepository:
    """Store O(1) objects per revision, closing only after full byte verification.

    The service owns single-process job fencing. This lock also serializes
    read-before-write immutability checks made through this repository instance.
    """

    def __init__(self, config: Config, store: Store) -> None:
        """Bind the remote store and per-run coordination for immutable bundle publication."""
        self.config, self.store = config, store
        self._lock = threading.RLock()
        self._runs: dict[str, tuple[threading.RLock, int]] = {}

    @contextmanager
    def _run_lock(self, run_id: str) -> Iterator[None]:
        """Serialize one UUID while allowing unrelated archive transfers."""
        with self._lock:
            lock, users = self._runs.get(run_id, (threading.RLock(), 0))
            self._runs[run_id] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._lock:
                _, users = self._runs[run_id]
                if users == 1:
                    del self._runs[run_id]
                else:
                    self._runs[run_id] = (lock, users - 1)

    def _metadata(self, metadata: dict) -> dict:
        """Validate owner-editable display metadata before it enters an immutable bundle."""
        require(isinstance(metadata, dict), 'invalid_bundle')
        require(
            set(metadata) == {'display_name', 'tags', 'pinned', 'created_at'},
            'invalid_bundle',
        )
        require(
            isinstance(metadata['display_name'], str)
            and len(metadata['display_name']) <= self.config.limits.name_chars,
            'invalid_bundle',
        )
        require(type(metadata['pinned']) is bool, 'invalid_bundle')
        tags = metadata['tags']
        require(
            isinstance(tags, list) and len(tags) <= self.config.limits.tags,
            'invalid_bundle',
        )
        require(
            all(isinstance(tag, str) and len(tag) <= self.config.limits.tag_chars for tag in tags),
            'invalid_bundle',
        )
        utc_timestamp(metadata['created_at'])
        return cast(dict, parse_json(json_bytes(metadata)))

    def _document(self, descriptor: BundleDescriptor) -> bytes:
        """Encode the full metadata document bound to a bundle descriptor."""
        return json_bytes(
            dict(
                version=1,
                identity=asdict(descriptor.identity),
                marker=descriptor.marker,
                metadata=descriptor.metadata,
                archive_size=descriptor.archive_size,
                tombstone=descriptor.tombstone,
            )
        )

    def _closure(self, descriptor: BundleDescriptor) -> bytes:
        """Bind the archive and metadata identities in the final publication manifest."""
        document = self._document(descriptor)
        return json_bytes(
            dict(
                version=1,
                identity=asdict(descriptor.identity),
                archive_size=descriptor.archive_size,
                metadata_size=len(document),
                metadata_sha256=digest(document),
                basket_sha256=descriptor.marker.get('basket_sha256'),
                report_sha256=descriptor.marker.get('report_sha256'),
                tombstone=descriptor.tombstone,
            )
        )

    def _get_optional(self, key: str) -> bytes | None:
        """Read a bounded object, treating only an explicit not-found response as absence."""
        try:
            return self.store.get(key, self.config.limits.json_bytes)
        except ViewerError as error:
            if error.status == 404:
                return None
            raise

    def _immutable(self, key: str, data: bytes) -> None:
        """Create or verify identical immutable bytes, resolving an uncertain write by readback."""
        require(len(data) <= self.config.limits.json_bytes, 'object_limit')
        old = self._get_optional(key)
        if old is not None:
            if old != data:
                raise ViewerError('bundle_conflict', 'Immutable bundle conflict.', 409)
            return
        try:
            self.store.put_if_absent(key, data)
        except Exception:
            # A timeout can occur after storage committed the complete object.
            if self._get_optional(key) != data:
                raise
        if self.store.get(key, self.config.limits.json_bytes) != data:
            raise ViewerError('bundle_conflict', 'Immutable bundle conflict.', 409)

    def _archive(self, identity: BundleIdentity, size: int, destination: Path | None = None) -> None:
        """Stream and verify the complete original archive, optionally retaining a local copy."""
        require(
            type(size) is int and 0 <= size <= self.config.limits.archive_bytes,
            'invalid_bundle',
        )
        sha = hashlib.sha256()
        count = 0
        deadline = time.monotonic() + self.config.limits.transfer_seconds
        output = destination.open('xb') if destination is not None else None
        try:
            with self.store.open(identity.archive_key) as source:
                while chunk := source.read(CHUNK):
                    count += len(chunk)
                    require(count <= size, 'stored_digest_mismatch')
                    require(time.monotonic() <= deadline, 'transfer_timeout')
                    sha.update(chunk)
                    if output is not None:
                        output.write(chunk)
            require(
                count == size and sha.hexdigest() == identity.archive_sha256,
                'stored_digest_mismatch',
            )
        except ViewerError as error:
            raise BundleVerificationError(error, 'archive') from error
        finally:
            if output is not None:
                output.close()

    def _marker_identity(self, identity: BundleIdentity, marker: dict) -> None:
        """Validate the strict publication marker against its bundle identity and inventory."""
        require(
            isinstance(marker, dict)
            and set(marker)
            == {
                'version',
                'owner',
                'run_id',
                'basket_sha256',
                'report_sha256',
                'archive_sha256',
                'archive_size',
                'published_at',
                'operation_id',
                'inventory',
            },
            'invalid_bundle',
        )
        require(type(marker['version']) is int and marker['version'] == 1, 'invalid_bundle')
        require(
            marker['owner'] == identity.owner
            and marker['run_id'] == identity.run_id
            and marker['archive_sha256'] == identity.archive_sha256,
            'invalid_bundle',
        )
        require(
            _sha(marker['basket_sha256']) and _sha(marker['report_sha256']),
            'invalid_bundle',
        )
        require(
            type(marker['archive_size']) is int and 0 <= marker['archive_size'] <= self.config.limits.archive_bytes,
            'invalid_bundle',
        )
        require(isinstance(marker['operation_id'], str), 'invalid_bundle')
        utc_timestamp(marker['published_at'])
        require(
            isinstance(marker['inventory'], list) and len(marker['inventory']) <= self.config.limits.files,
            'invalid_bundle',
        )
        paths = set()
        for entry in marker['inventory']:
            require(
                isinstance(entry, dict) and set(entry) == {'path', 'size_bytes', 'sha256'},
                'invalid_bundle',
            )
            safe_path(entry['path'], self.config.limits.path_bytes)
            require(
                entry['path'] not in paths
                and type(entry['size_bytes']) is int
                and 0 <= entry['size_bytes'] <= self.config.limits.file_bytes
                and _sha(entry['sha256']),
                'invalid_bundle',
            )
            paths.add(entry['path'])

    def _check_import(self, identity: BundleIdentity, imported: ImportedRun, marker: dict) -> None:
        """Check imported evidence, inventory and marker binding before remote publication."""
        self._marker_identity(identity, marker)
        require(
            marker.get('owner') == identity.owner and marker.get('run_id') == identity.run_id,
            'invalid_bundle',
        )
        require(
            marker.get('archive_sha256') == identity.archive_sha256 == imported.archive_sha256,
            'invalid_bundle',
        )
        require(
            marker.get('archive_size') == imported.archive.stat().st_size,
            'invalid_bundle',
        )
        expected = [entry.to_wire() for entry in imported.manifest.files]
        manifest = imported.directory / 'manifest.json'
        expected.append(
            dict(
                path='manifest.json',
                size_bytes=manifest.stat().st_size,
                sha256=digest(manifest.read_bytes()),
            )
        )
        require(marker.get('inventory') == expected, 'invalid_bundle')
        prefix = f'runs/{identity.owner}/{identity.run_id}/'
        source = _ImportedPublication(imported, marker, prefix, self.config.limits.path_bytes)
        PublicationReader(self.config, source)._marker(prefix + 'publication.json')

    def publish_bundle(
        self,
        identity: BundleIdentity,
        imported: ImportedRun | Path | None,
        marker: dict,
        metadata: dict,
    ) -> BundleDescriptor:
        """Upload exact original archive once and close a verified metadata revision."""
        self.registry_snapshot()
        if imported is None:
            self._marker_identity(identity, marker)
            with tempfile.TemporaryDirectory(prefix='bundle-metadata-') as temporary:
                archive = Path(temporary) / 'archive.tar.gz'
                self._archive(identity, marker['archive_size'], archive)
                validated = validate_archive(archive, Path(temporary) / 'data', self.config.limits)
                return self._publish_verified(identity, validated, marker, metadata, allow_upload=False)
        if isinstance(imported, Path):
            with tempfile.TemporaryDirectory(prefix='bundle-validation-') as temporary:
                validated = validate_archive(imported, Path(temporary) / 'data', self.config.limits)
                return self.publish_bundle(identity, validated, marker, metadata)
        return self._publish_verified(identity, imported, marker, metadata, allow_upload=True)

    def _publish_verified(
        self, identity: BundleIdentity, imported: ImportedRun, marker: dict, metadata: dict, *, allow_upload: bool
    ) -> BundleDescriptor:
        """Close verified archive and metadata objects, then advance the authoritative registry."""
        self._check_import(identity, imported, marker)
        descriptor = BundleDescriptor(
            identity,
            cast(dict, parse_json(json_bytes(marker))),
            self._metadata(metadata),
            marker['archive_size'],
        )
        with self._run_lock(identity.run_id):
            self._reserve(identity)
            try:
                self._archive(identity, descriptor.archive_size)
            except ViewerError as error:
                if error.status != 404 or not allow_upload:
                    raise
                try:
                    self.store.put_file_if_absent(
                        identity.archive_key,
                        imported.archive,
                        deadline=time.monotonic() + self.config.limits.transfer_seconds,
                    )
                except Exception:
                    # A failed response can follow a complete upload; verify its exact bytes.
                    self._archive(identity, descriptor.archive_size)
                self._archive(identity, descriptor.archive_size)
            self._immutable(identity.metadata_key, self._document(descriptor))
            self._immutable(identity.closing_key, self._closure(descriptor))
            self._register(descriptor)
        return descriptor

    def publish_tombstone(
        self, identity: BundleIdentity, metadata: dict, marker: dict | None = None
    ) -> BundleDescriptor:
        """Close a deletion generation without copying or deleting archive objects."""
        self.registry_snapshot()
        if marker:
            self._marker_identity(identity, {**marker, 'inventory': marker.get('inventory', [])})
        descriptor = BundleDescriptor(identity, marker or {}, self._metadata(metadata), 0, True)
        with self._run_lock(identity.run_id):
            self._reserve(identity)
            self._immutable(identity.metadata_key, self._document(descriptor))
            self._immutable(identity.closing_key, self._closure(descriptor))
            self._register(descriptor)
        return descriptor

    def read_descriptor(self, key: str, *, expected_sha256: str | None = None) -> BundleDescriptor:
        """Attach the failing object class to storage validation errors."""
        try:
            raw = self.store.get(key, self.config.limits.json_bytes)
            if expected_sha256 is not None:
                require(digest(raw) == expected_sha256, 'stored_digest_mismatch')
            return self._read_descriptor(key, raw)
        except BundleVerificationError:
            raise
        except ViewerError as error:
            raise BundleVerificationError(error, 'closure') from error

    def _read_descriptor(self, key: str, raw: bytes) -> BundleDescriptor:
        """Validate a closed manifest and its exactly bound metadata document."""
        closure = _json_object(raw, 'invalid_bundle')
        require(
            isinstance(closure, dict)
            and set(closure)
            == {
                'version',
                'identity',
                'archive_size',
                'metadata_size',
                'metadata_sha256',
                'basket_sha256',
                'report_sha256',
                'tombstone',
            },
            'invalid_bundle',
        )
        require(
            type(closure['version']) is int and closure['version'] == 1,
            'invalid_bundle',
        )
        require(
            isinstance(closure['identity'], dict)
            and set(closure['identity']) == {'owner', 'run_id', 'generation', 'archive_sha256', 'metadata_revision'},
            'invalid_bundle',
        )
        identity = BundleIdentity.from_wire(closure['identity'])
        require(key == identity.closing_key, 'invalid_bundle')
        require(
            type(closure['metadata_size']) is int and 0 <= closure['metadata_size'] <= self.config.limits.json_bytes,
            'invalid_bundle',
        )
        try:
            document = self.store.get(identity.metadata_key, self.config.limits.json_bytes)
            require(
                len(document) == closure['metadata_size'] and digest(document) == closure['metadata_sha256'],
                'stored_digest_mismatch',
            )
        except ViewerError as error:
            raise BundleVerificationError(error, 'metadata') from error
        value = _json_object(document, 'invalid_bundle')
        require(
            isinstance(value, dict)
            and set(value)
            == {
                'version',
                'identity',
                'marker',
                'metadata',
                'archive_size',
                'tombstone',
            },
            'invalid_bundle',
        )
        require(type(value['version']) is int and value['version'] == 1, 'invalid_bundle')
        require(value['identity'] == asdict(identity), 'invalid_bundle')
        require(
            type(value['tombstone']) is bool
            and type(value['archive_size']) is int
            and 0 <= value['archive_size'] <= self.config.limits.archive_bytes,
            'invalid_bundle',
        )
        require(isinstance(value['marker'], dict), 'invalid_bundle')
        marker = value['marker']
        if value['tombstone']:
            require(value['archive_size'] == 0, 'invalid_bundle')
            if marker:
                self._marker_identity(identity, {**marker, 'inventory': marker.get('inventory', [])})
        else:
            self._marker_identity(identity, marker)
            require(
                marker.get('owner') == identity.owner
                and marker.get('run_id') == identity.run_id
                and marker.get('archive_sha256') == identity.archive_sha256
                and marker.get('archive_size') == value['archive_size'],
                'invalid_bundle',
            )
            require(
                _sha(marker.get('basket_sha256')) and _sha(marker.get('report_sha256')),
                'invalid_bundle',
            )
        descriptor = BundleDescriptor(
            identity,
            marker,
            self._metadata(value['metadata']),
            value['archive_size'],
            value['tombstone'],
        )
        require(self._closure(descriptor) == json_bytes(closure), 'invalid_bundle')
        return descriptor

    def read_bundle(self, identity: BundleIdentity, *, expected_sha256: str | None = None) -> BundleDescriptor:
        """Read one exact closed revision without listing storage."""
        descriptor = self.read_descriptor(identity.closing_key, expected_sha256=expected_sha256)
        require(descriptor.identity == identity, 'bundle_conflict')
        return descriptor

    def initialize_registry(self) -> BundleRegistrySnapshot:
        """Explicitly initialize a new namespace; never infer emptiness during reads."""
        if self._get_optional(REGISTRY_HEAD) is not None:
            return self.registry_snapshot()
        if next(iter(self.store.list(INDEX)), None) is not None:
            raise ViewerError('registry_missing', 'Existing bundles have no authoritative registry.', 503)
        self.store.put_if_absent(RESERVATIONS, json_bytes(dict(version=1, entries={})))
        snapshot = json_bytes(dict(version=1, revision=0, parent=None, entries=[]))
        sha = digest(snapshot)
        self._immutable(REGISTRY + 'snapshots/' + sha + '.json', snapshot)
        head = json_bytes(dict(version=1, revision=0, snapshot_sha256=sha, snapshot_size=len(snapshot), count=0))
        try:
            self.store.compare_and_swap(REGISTRY_HEAD, None, head)
        except Exception:
            # Resolve a possibly committed CAS only by reading back the intended head.
            if self._get_optional(REGISTRY_HEAD) != head:
                raise
        return self.registry_snapshot()

    def registry_snapshot(self) -> BundleRegistrySnapshot:
        """Read a complete immutable registry through its CAS-installed head."""
        raw_head = self._get_optional(REGISTRY_HEAD)
        if raw_head is None:
            raise ViewerError('registry_missing', 'Authoritative backup registry is missing.', 503)
        head = _json_object(raw_head, 'invalid_registry')
        require(
            isinstance(head, dict)
            and set(head) == {'version', 'revision', 'snapshot_sha256', 'snapshot_size', 'count'},
            'invalid_registry',
        )
        require(
            type(head['version']) is int
            and head['version'] == 1
            and type(head['revision']) is int
            and 0 <= head['revision'] < 2**63
            and _sha(head['snapshot_sha256'])
            and type(head['snapshot_size']) is int
            and 0 <= head['snapshot_size'] <= self.config.limits.json_bytes
            and type(head['count']) is int
            and 0 <= head['count'] <= 100_000,
            'invalid_registry',
        )
        raw = self.store.get(REGISTRY + 'snapshots/' + head['snapshot_sha256'] + '.json', self.config.limits.json_bytes)
        require(len(raw) == head['snapshot_size'] and digest(raw) == head['snapshot_sha256'], 'invalid_registry')
        snapshot = _json_object(raw, 'invalid_registry')
        require(
            isinstance(snapshot, dict) and set(snapshot) == {'version', 'revision', 'parent', 'entries'},
            'invalid_registry',
        )
        require(
            type(snapshot['version']) is int
            and snapshot['version'] == 1
            and type(snapshot['revision']) is int
            and snapshot['revision'] == head['revision']
            and (snapshot['parent'] is None if head['revision'] == 0 else _sha(snapshot['parent']))
            and isinstance(snapshot['entries'], list)
            and len(snapshot['entries']) == head['count'],
            'invalid_registry',
        )
        run_ids = []
        for entry in snapshot['entries']:
            require(
                isinstance(entry, dict)
                and set(entry) == {'identity', 'manifest_key', 'closure_sha256', 'archive_size', 'tombstone'},
                'invalid_registry',
            )
            identity = BundleIdentity.from_wire(entry['identity'])
            require(
                entry['manifest_key'] == identity.closing_key
                and _sha(entry['closure_sha256'])
                and type(entry['archive_size']) is int
                and 0 <= entry['archive_size'] <= self.config.limits.archive_bytes
                and type(entry['tombstone']) is bool,
                'invalid_registry',
            )
            run_ids.append(identity.run_id)
        require(run_ids == sorted(set(run_ids)), 'invalid_registry')
        return BundleRegistrySnapshot(head['revision'], head['snapshot_sha256'], raw_head, tuple(snapshot['entries']))

    def reservation_floors(self) -> tuple[bytes, dict[str, tuple[int, int]]]:
        """Read allocation floors, including backups that never reached the registry."""
        raw = self._get_optional(RESERVATIONS)
        require(raw is not None, 'reservations_missing')
        assert raw is not None
        document = _json_object(raw, 'invalid_reservations')
        require(set(document) == {'version', 'entries'} and document['version'] == 1, 'invalid_reservations')
        entries = document['entries']
        require(isinstance(entries, dict), 'invalid_reservations')
        floors = {}
        for rid, value in entries.items():
            try:
                require(str(uuid.UUID(rid)) == rid, 'invalid_reservations')
            except (ValueError, TypeError, AttributeError):
                raise ViewerError('invalid_reservations', 'Invalid reserved run identity.') from None
            require(
                isinstance(value, list)
                and len(value) == 2
                and all(type(number) is int and 0 < number < 2**63 for number in value),
                'invalid_reservations',
            )
            floors[rid] = (value[0], value[1])
        return raw, floors

    def _reserve(self, identity: BundleIdentity) -> None:
        """CAS independent generation/revision high-water marks before immutable writes."""
        for _ in range(32):
            raw, floors = self.reservation_floors()
            generation, revision = floors.get(identity.run_id, (0, 0))
            floor = (max(generation, identity.generation), max(revision, identity.metadata_revision))
            if floor == (generation, revision):
                return
            floors[identity.run_id] = floor
            updated = json_bytes(dict(version=1, entries=floors))
            require(len(updated) <= self.config.limits.json_bytes, 'object_limit')
            try:
                if self.store.compare_and_swap(RESERVATIONS, raw, updated):
                    return
            except Exception:
                if self._get_optional(RESERVATIONS) == updated:
                    return
                raise
        raise ViewerError('registry_busy', 'Backup reservations changed repeatedly; retry the backup.', 503)

    def _register(self, descriptor: BundleDescriptor) -> None:
        """CAS the latest-reference registry without letting late workers roll it back."""
        identity = descriptor.identity
        reference = dict(
            identity=identity.to_wire(),
            manifest_key=descriptor.manifest_key,
            closure_sha256=digest(self._closure(descriptor)),
            archive_size=descriptor.archive_size,
            tombstone=descriptor.tombstone,
        )
        for _ in range(32):
            current = self.registry_snapshot()
            entries = {entry['identity']['run_id']: entry for entry in current.entries}
            previous = entries.get(identity.run_id)
            if previous is not None:
                old = BundleIdentity.from_wire(previous['identity'])
                require(old.owner == identity.owner, 'bundle_conflict')
                if old.generation == identity.generation:
                    require(
                        old.archive_sha256 == identity.archive_sha256 and previous['tombstone'] == descriptor.tombstone,
                        'bundle_conflict',
                    )
                if (old.generation, old.metadata_revision) > (identity.generation, identity.metadata_revision):
                    return
                if (old.generation, old.metadata_revision) == (identity.generation, identity.metadata_revision):
                    require(previous == reference, 'bundle_conflict')
                    return
            entries[identity.run_id] = reference
            require(len(entries) <= 100_000, 'bundle_discovery_limit')
            snapshot = json_bytes(
                dict(
                    version=1,
                    revision=current.revision + 1,
                    parent=current.snapshot_sha256,
                    entries=[entries[key] for key in sorted(entries)],
                )
            )
            sha = digest(snapshot)
            self._immutable(REGISTRY + 'snapshots/' + sha + '.json', snapshot)
            head = json_bytes(
                dict(
                    version=1,
                    revision=current.revision + 1,
                    snapshot_sha256=sha,
                    snapshot_size=len(snapshot),
                    count=len(entries),
                )
            )
            try:
                if self.store.compare_and_swap(REGISTRY_HEAD, current.head_bytes, head):
                    return
            except Exception:
                # Retain an uncertain registry write unless exact readback proves success.
                if self._get_optional(REGISTRY_HEAD) == head:
                    return
                raise
        raise ViewerError('registry_busy', 'Backup registry changed repeatedly; retry the backup.', 503)

    def iter_descriptors(self, *, max_objects: int = 100_000) -> Iterator[BundleDescriptor | BundleDiscoveryError]:
        """Read every authoritative reference without relying on object LIST."""
        require(type(max_objects) is int and max_objects > 0, 'bundle_discovery_limit')
        snapshot = self.registry_snapshot()
        require(len(snapshot.entries) <= max_objects, 'bundle_discovery_limit')
        for entry in snapshot.entries:
            try:
                yield self.read_descriptor(entry['manifest_key'], expected_sha256=entry['closure_sha256'])
            except ViewerError as error:
                yield BundleDiscoveryError(entry['manifest_key'], error.code)

    def discover(self, *, max_objects: int = 100_000) -> list[BundleDescriptor]:
        """Return the exact complete current registry, rejecting any unreadable entry."""
        result = []
        for item in self.iter_descriptors(max_objects=max_objects):
            if isinstance(item, BundleDiscoveryError):
                raise ViewerError(item.code, 'Registered bundle could not be verified.')
            result.append(item)
        return result

    def verify_bundle(self, descriptor: BundleDescriptor) -> None:
        """Verify closure, metadata and full original archive size/SHA, never ETag."""
        require(
            self.read_descriptor(descriptor.identity.closing_key) == descriptor,
            'bundle_conflict',
        )
        if not descriptor.tombstone:
            self._archive(descriptor.identity, descriptor.archive_size)

    def download_bundle(self, descriptor: BundleDescriptor, destination: Path) -> DownloadedBundle:
        """Restore only a closed bundle and validate its complete publication evidence."""
        require(not descriptor.tombstone, 'bundle_deleted')
        require(
            self.read_descriptor(descriptor.identity.closing_key) == descriptor,
            'bundle_conflict',
        )
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        completed = False
        try:
            archive = destination / 'archive.tar.gz'
            self._archive(descriptor.identity, descriptor.archive_size, archive)
            try:
                imported = validate_archive(archive, destination / 'data', self.config.limits)
            except ViewerError as error:
                raise BundleVerificationError(error, 'archive') from error
            try:
                self._check_import(descriptor.identity, imported, descriptor.marker)
            except ViewerError as error:
                raise BundleVerificationError(error, 'metadata') from error
            result = DownloadedBundle(imported, descriptor.marker, descriptor.metadata, descriptor)
            completed = True
            return result
        finally:
            if not completed:
                shutil.rmtree(destination)


class _ImportedPublication(Store):
    """Map a validated extraction without reading remote unpacked artifacts."""

    def __init__(self, imported: ImportedRun, marker: dict, prefix: str, path_bytes: int) -> None:
        """Expose one validated staging tree through the publication reader's logical prefix."""
        self.directory = imported.directory
        self.prefix, self.marker, self.path_bytes = prefix, marker, path_bytes

    def get(self, key: str, limit: int = 64 << 20) -> bytes:
        """Read the exact staged publication object without fetching remote evidence."""
        if key == self.prefix + 'publication.json':
            return json_bytes(self.marker)
        prefix = self.prefix + 'data/'
        require(key.startswith(prefix), 'invalid_publication')
        path = key[len(prefix) :]
        inventory = {entry['path']: entry for entry in self.marker['inventory']}
        require(path in inventory, 'invalid_publication')
        safe_path(path, self.path_bytes)
        with (self.directory / path).open('rb') as stream:
            data = stream.read(limit + 1)
        entry = inventory[path]
        require(
            len(data) <= limit and len(data) == entry['size_bytes'] and digest(data) == entry['sha256'],
            'stored_digest_mismatch',
        )
        return data
