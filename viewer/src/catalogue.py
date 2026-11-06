"""Read-only catalogue construction and coherent in-memory versions."""

import re
import threading
import time
import uuid
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field

from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.database import LOGIN
from viewer.persistence.storage import Store
from viewer.publication import CatalogueEntry, PublicationReader
from viewer.util import utc_now, utc_timestamp
from viewer.validation.contracts import require


@dataclass(frozen=True)
class CatalogueSnapshot:
    """A replacement unit; published containers are never mutated by the service."""

    runs: dict[str, CatalogueEntry] = field(default_factory=dict)
    metadata: dict[str, dict] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    display: dict[str, dict[str, bool]] = field(default_factory=dict)
    quarantined: dict[str, dict] = field(default_factory=dict)
    persisted_order: list[str] = field(default_factory=list)
    persisted_display: dict[str, dict[str, bool]] = field(default_factory=dict)
    last_refresh: str | None = None
    revision: int = 0

    def counts(self) -> dict[str, int]:
        """Count visible runs, baskets and quarantined publications."""
        return {'runs': len(self.runs), 'baskets': len(self.order), 'quarantined': len(self.quarantined)}


class CatalogueBuilder(PublicationReader):
    """Build a candidate from read-only storage and copied owner bindings."""

    def __init__(self, config: Config, store: Store, owners: dict) -> None:
        """Bind read-only storage and the copied owner reservations."""
        super().__init__(config, store)
        self.owners = owners

    def _validate_metadata(self, value: dict) -> None:
        """Validate the persisted run display metadata."""
        require(isinstance(value, dict), 'invalid_metadata')
        for rid, row in value.items():
            require(isinstance(rid, str) and isinstance(row, dict), 'invalid_metadata')
            require(set(row) == {'display_name', 'tags', 'pinned', 'created_at'}, 'invalid_metadata')
            self.validate_patch({k: row[k] for k in ('display_name', 'tags')})
            require(type(row['pinned']) is bool, 'invalid_metadata')
            utc_timestamp(row['created_at'])

    def validate_patch(self, patch: dict) -> None:
        """Validate editable run metadata without changing publication evidence."""
        limits = self.config.limits
        require(
            isinstance(patch, dict) and bool(patch) and not set(patch) - {'display_name', 'tags'},
            'invalid_metadata',
        )
        if 'display_name' in patch:
            require(
                isinstance(patch['display_name'], str)
                and bool(patch['display_name'].strip())
                and len(patch['display_name']) <= limits.name_chars,
                'invalid_metadata',
            )
        if 'tags' in patch:
            require(
                isinstance(patch['tags'], list)
                and len(patch['tags']) <= limits.tags
                and all(
                    isinstance(t, str)
                    and bool(t.strip())
                    and len(t) <= limits.tag_chars
                    and all(ord(c) >= 32 and ord(c) != 127 for c in t)
                    for t in patch['tags']
                ),
                'invalid_metadata',
            )

    def _validate_display(self, display: dict) -> None:
        """Validate public basket labels and presentation settings."""
        require(isinstance(display, dict), 'invalid_basket_display')
        for h, flags in display.items():
            require(isinstance(h, str) and re.fullmatch(r'[0-9a-f]{64}', h) is not None, 'invalid_basket_display')
            require(
                isinstance(flags, dict)
                and set(flags) == {'hidden', 'collapsed'}
                and all(type(v) is bool for v in flags.values()),
                'invalid_basket_display',
            )

    def _scan_publications(self) -> tuple[dict[str, CatalogueEntry], dict[str, dict], set[str]]:
        """Read publication markers and quarantine invalid records without replacing caches."""
        runs = {}
        quarantined = {}
        protected_hashes = set()
        for key in self.store.list('runs/'):
            if not key.endswith('/publication.json'):
                continue
            try:
                row = self._marker(key)
            except ViewerError as error:
                if error.code not in (
                    'invalid_report',
                    'invalid_basket',
                    'invalid_contract',
                    'invalid_public_artifact',
                ):
                    raise
                parts = key.split('/')
                require(
                    len(parts) == 4 and parts[0] == 'runs' and LOGIN.fullmatch(parts[1]),
                    'invalid_publication',
                )
                try:
                    rid = str(uuid.UUID(parts[2]))
                    require(rid == parts[2], 'invalid_publication')
                except (ValueError, TypeError, AttributeError):
                    raise ViewerError('invalid_publication', 'Invalid stored run identity.') from None
                marker = self._object(key, code='invalid_publication')
                require(isinstance(marker, dict), 'invalid_publication')
                assert isinstance(marker, dict)
                basket_hash = marker.get('basket_sha256')
                if isinstance(basket_hash, str) and re.fullmatch(r'[0-9a-f]{64}', basket_hash):
                    protected_hashes.add(basket_hash)
                quarantined[rid] = {
                    'code': error.code,
                    'prefix': '/'.join(parts[:3]) + '/',
                    'basket_hash': basket_hash,
                }
                continue
            rid = row['marker']['run_id']
            require(rid not in runs, 'duplicate_run')
            owner = self.owners.get(rid)
            require(
                owner is None
                or owner == {'login': row['marker']['owner'], 'basket_hash': row['marker']['basket_sha256']},
                'owner_conflict',
            )
            runs[rid] = row
        return runs, quarantined, protected_hashes

    def build(self) -> CatalogueSnapshot:
        """Validate all catalogue fields without publishing or persisting them."""
        runs, quarantined, protected_hashes = self._scan_publications()
        metadata = self._object('settings/viewer-metadata.json', {}, code='invalid_metadata')
        self._validate_metadata(metadata)
        assert isinstance(metadata, dict)
        metadata = {rid: row for rid, row in metadata.items() if rid in runs or rid in quarantined}
        for rid, row in runs.items():
            metadata.setdefault(
                rid,
                {
                    'display_name': row['report'].agent.name,
                    'tags': row['report'].run.tags,
                    'pinned': False,
                    'created_at': row['report'].run.created_at,
                },
            )
        self._validate_metadata(metadata)
        order = self._json('settings/basket-order.json', [])
        require(
            isinstance(order, list) and all(isinstance(x, str) for x in order) and len(set(order)) == len(order),
            'invalid_basket_order',
        )
        assert isinstance(order, list)
        order = [item for item in order if isinstance(item, str)]
        hashes = {row['marker']['basket_sha256'] for row in runs.values()}
        stored_order = [h for h in order if h in hashes or h in protected_hashes]
        order = [h for h in stored_order if h in hashes]
        first = {
            h: min(row['marker']['published_at'] for row in runs.values() if row['marker']['basket_sha256'] == h)
            for h in hashes
        }
        order += sorted(hashes - set(order), key=lambda basket_hash: (first[basket_hash], basket_hash))
        stored_display = self._object(
            'settings/basket-display.json', {'version': 1, 'items': {}}, code='invalid_basket_display'
        )
        require(
            isinstance(stored_display, dict)
            and set(stored_display) == {'version', 'items'}
            and type(stored_display['version']) is int
            and stored_display['version'] == 1,
            'invalid_basket_display',
        )
        assert isinstance(stored_display, dict)
        self._validate_display(stored_display['items'])
        display = {h: stored_display['items'].get(h, {'hidden': False, 'collapsed': False}) for h in order}
        persisted_order = stored_order + [h for h in order if h not in stored_order]
        persisted_display = {
            h: stored_display['items'].get(h, {'hidden': False, 'collapsed': False}) for h in persisted_order
        }
        return CatalogueSnapshot(
            runs=runs,
            metadata=metadata,
            order=order,
            display=deepcopy(display),
            quarantined=quarantined,
            persisted_order=persisted_order,
            persisted_display=deepcopy(persisted_display),
            last_refresh=utc_now(time.time()),
        )


class ScanStore(Store):
    """Check cancellation between bounded storage calls during a full scan."""

    def __init__(self, store: Store, stop: threading.Event) -> None:
        """Wrap storage with the maintenance cancellation event."""
        self.source, self.stop = store, stop

    def check(self) -> None:
        """Reject scan work once shutdown requests cancellation."""
        if self.stop.is_set():
            raise ViewerError('refresh_cancelled', 'Catalogue refresh was cancelled.', 503)

    def get(self, key: str, limit: int = 64 << 20) -> bytes:
        """Read one bounded object and check cancellation around the read."""
        self.check()
        value = self.source.get(key, limit)
        self.check()
        return value

    def list(self, prefix: str) -> Iterator[str]:
        """Yield stored keys while checking cancellation between entries."""
        self.check()
        for key in self.source.list(prefix):
            self.check()
            yield key
