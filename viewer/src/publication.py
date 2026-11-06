"""Shared read-only validation of stored and staged publications."""

import hashlib
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from viewer.config import Config
from viewer.errors import ViewerError
from viewer.models import Basket, Report
from viewer.models.evidence import ArtifactReceipt
from viewer.models.report import ReportAgent, ReportBasket, ReportInference, ReportRun
from viewer.persistence.database import LOGIN
from viewer.persistence.storage import Store
from viewer.presentation.cost_summary import CostEvidenceError
from viewer.presentation.inference import public_inference
from viewer.presentation.metrics import aggregate_snapshot
from viewer.presentation.run_projection import basket_metadata, projection, wall_time
from viewer.presentation.task_measurements import (
    TaskMeasurements,
    build_task_measurements,
)
from viewer.util import (
    JsonValue,
    digest,
    json_bytes,
    parse_json,
    safe_path,
    utc_timestamp,
)
from viewer.validation.basket import validate_basket, validate_basket_report
from viewer.validation.contracts import (
    require,
    validate_report,
    validate_selected_state,
)


@dataclass
class CachedReport:
    """Retain catalogue metadata without retaining task evidence."""

    run: ReportRun
    agent: ReportAgent
    basket: ReportBasket
    inference: ReportInference


class CatalogueEntry(TypedDict):
    """Describe the bounded cache assembled from validated stored documents."""

    marker: dict
    report: CachedReport
    auth_mode: str | None
    basket: dict
    wall_time_seconds: float | None
    benchmark: dict | None
    prefix: str
    aggregates: dict[int, dict]
    measurements: dict[str, TaskMeasurements]


class PublicationReader:
    """Validate a single publication using only configuration and a read source."""

    def __init__(self, config: Config, store: Store) -> None:
        """Bind publication storage and validation limits."""
        self.config, self.store = config, store

    def _json(self, key: str, default: JsonValue = None) -> JsonValue:
        """Read a stored JSON document or copy the caller default when absent."""
        try:
            return parse_json(self.store.get(key, self.config.limits.json_bytes))
        except ViewerError as error:
            if error.status == 404:
                return deepcopy(default)
            raise

    def _object(self, key: str, default: JsonValue = None, *, code: str = 'invalid_contract') -> dict:
        """Check a stored object boundary before validating its dynamic metadata."""
        value = self._json(key, default)
        require(isinstance(value, dict), code)
        assert isinstance(value, dict)
        return value

    def _marker(self, key: str) -> CatalogueEntry:
        """Validate a publication marker and its bound stored documents."""
        marker = self._object(key, code='invalid_publication')
        require(isinstance(marker, dict), 'invalid_publication')
        assert isinstance(marker, dict)
        expected = {
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
        }
        require(
            set(marker) == expected and type(marker['version']) is int and marker['version'] == 1,
            'invalid_publication',
        )
        utc_timestamp(marker['published_at'])
        require(isinstance(marker['owner'], str) and LOGIN.fullmatch(marker['owner']), 'invalid_publication')
        try:
            require(str(uuid.UUID(marker['run_id'])) == marker['run_id'])
        except (ValueError, TypeError, AttributeError):
            raise ViewerError('invalid_publication', 'Invalid stored run identity.') from None
        prefix = 'runs/' + marker['owner'] + '/' + marker['run_id'] + '/'
        require(key == prefix + 'publication.json', 'invalid_publication')
        for name in ('basket_sha256', 'report_sha256', 'archive_sha256'):
            require(
                isinstance(marker[name], str)
                and len(marker[name]) == 64
                and all(c in '0123456789abcdef' for c in marker[name]),
                'invalid_publication',
            )
        require(type(marker['archive_size']) is int and 0 <= marker['archive_size'] <= self.config.limits.archive_bytes)
        require(isinstance(marker['inventory'], list) and len(marker['inventory']) <= self.config.limits.files)
        for entry in marker['inventory']:
            require(isinstance(entry, dict) and set(entry) == {'path', 'size_bytes', 'sha256'})
            safe_path(entry['path'], self.config.limits.path_bytes)
            require(type(entry['size_bytes']) is int and 0 <= entry['size_bytes'] <= self.config.limits.file_bytes)
            require(
                isinstance(entry['sha256'], str)
                and len(entry['sha256']) == 64
                and all(c in '0123456789abcdef' for c in entry['sha256'])
            )
        require(len({entry['path'] for entry in marker['inventory']}) == len(marker['inventory']))
        report_bytes = self.store.get(prefix + 'data/report.json', self.config.limits.json_bytes)
        basket_bytes = self.store.get(prefix + 'data/basket.json', self.config.limits.json_bytes)
        require(
            digest(report_bytes) == marker['report_sha256'] and digest(basket_bytes) == marker['basket_sha256'],
            'stored_digest_mismatch',
        )
        report = validate_report(parse_json(report_bytes))
        basket = validate_basket(parse_json(basket_bytes))
        validate_basket_report(basket, report)
        require(report.inference is not None, 'invalid_report')
        try:
            public_inference(report.inference)
        except ValueError:
            raise ViewerError('invalid_report', 'Unsupported or invalid inference contract.') from None
        self._display_limits(report, basket)
        require(report.run.id == marker['run_id'] and report.basket.sha256 == marker['basket_sha256'])
        require([t.id for t in report.tasks] == [t.id for t in basket.tasks])
        inventory = {entry['path']: ArtifactReceipt.model_validate(entry) for entry in marker['inventory']}

        cache: dict[str, bytes] = {}

        def read_evidence(path: str) -> bytes:
            """Read only inventory-owned evidence through the configured size limit."""
            require(path in inventory, 'invalid_publication')
            if path not in cache:
                cache[path] = self.store.get(prefix + 'data/' + path, self.config.limits.json_bytes)
            return cache[path]

        evidence = validate_selected_state(report, basket, inventory, read_evidence)
        try:
            measurements = build_task_measurements(evidence, report)
        except CostEvidenceError as error:
            raise ViewerError(error.code, error.message) from error
        return {
            'marker': {k: v for k, v in marker.items() if k != 'inventory'},
            'report': CachedReport(report.run, report.agent, report.basket, report.inference),
            'auth_mode': self._auth_mode(prefix, report),
            'basket': {'meta': basket_metadata(basket)},
            'wall_time_seconds': wall_time(report.run),
            'benchmark': self._benchmark_metadata(prefix, report),
            'prefix': prefix,
            'aggregates': aggregate_snapshot(report.tasks, measurements),
            'measurements': measurements,
        }

    def _auth_mode(self, prefix: str, report: Report) -> str | None:
        """Read only a verified allowlisted access mode, without retaining private config."""
        ref = report.config
        if ref.identity_version != 1 or ref.identity_path != 'config_identity.json':
            return None
        try:
            identity = self._json(prefix + 'data/config_identity.json')
        except ViewerError as error:
            if error.status == 413:
                return None
            raise
        if identity is None:
            return None
        require(
            isinstance(identity, dict) and digest(json_bytes(identity)) == ref.identity_sha256,
            'stored_digest_mismatch',
        )
        assert isinstance(identity, dict)
        mode = identity.get('auth_mode')
        return mode if isinstance(mode, str) and mode in ('api_key', 'chatgpt_oauth') else None

    def _benchmark_metadata(self, prefix: str, report: Report) -> dict | None:
        """Cache only verified frozen harness controls, without retaining raw config."""
        identity = self._json(prefix + 'data/config_identity.json')
        require(
            isinstance(identity, dict) and digest(json_bytes(identity)) == report.config.identity_sha256,
            'stored_digest_mismatch',
        )
        assert isinstance(identity, dict)
        try:
            return projection(identity, self.config.limits)
        except ValueError:
            return None

    def _display_limits(self, report: Report, basket: Basket) -> None:
        """Validate public display sizes before publishing a run."""
        limits = self.config.limits
        if (
            len(basket.meta.name) > limits.basket_name_chars
            or len(basket.meta.description) > limits.basket_description_chars
            or len(report.agent.runtime) > limits.runtime_chars
            or any(len(t.title) > limits.task_title_chars for t in report.tasks)
        ):
            raise ViewerError('metadata_limit', 'Display metadata exceeds configured limits.', 413)


class StagedPublication(Store):
    """Read the journal marker and hash-checked staged or installed evidence."""

    def __init__(
        self, op: dict, fallback: Store, path_bytes: int, *, installed_files: int = 0, installed_archive: bool = False
    ) -> None:
        """Bind the journal, local evidence and fallback storage."""
        self.op, self.fallback, self.path_bytes = op, fallback, path_bytes
        self.installed_paths = {entry['path'] for entry in op['marker']['inventory'][:installed_files]}
        self.installed_archive = installed_archive

    def get(self, key: str, limit: int = 64 << 20) -> bytes:
        """Read a staged object and verify its inventory size and digest."""
        if key == self.op['prefix'] + 'publication.json':
            return json_bytes(self.op['marker'])
        prefix = self.op['prefix'] + 'data/'
        root = Path(self.op['staging']) / 'data'
        if not key.startswith(prefix):
            return self.fallback.get(key, limit)
        path = key[len(prefix) :]
        safe_path(path, self.path_bytes)
        inventory = {entry['path']: entry for entry in self.op['marker']['inventory']}
        require(path in inventory, 'invalid_publication')
        if path in self.installed_paths:
            value = self.fallback.get(key, limit)
        else:
            with (root / path).open('rb') as stream:
                value = stream.read(limit + 1)
        require(len(value) <= limit, 'object_limit')
        entry = inventory[path]
        require(len(value) == entry['size_bytes'] and digest(value) == entry['sha256'], 'stored_digest_mismatch')
        return value

    def available(self) -> bool:
        """Require local copies only for files the journal has not recorded as transferred."""
        root = Path(self.op['staging'])
        if not self.installed_archive and not (root / 'archive.tar.gz').is_file():
            return False
        return all(
            entry['path'] in self.installed_paths or (root / 'data' / entry['path']).is_file()
            for entry in self.op['marker']['inventory']
        )

    def validate(self, budget: Callable[[], None]) -> None:
        """Hash every recovery input from its recorded source before publication completion."""
        marker = self.op['marker']
        self._verify('archive.tar.gz', marker['archive_size'], marker['archive_sha256'], self.installed_archive, budget)
        for entry in marker['inventory']:
            safe_path(entry['path'], self.path_bytes)
            self._verify(
                'data/' + entry['path'],
                entry['size_bytes'],
                entry['sha256'],
                entry['path'] in self.installed_paths,
                budget,
            )

    def _verify(self, relative: str, size: int, sha256: str, installed: bool, budget: Callable[[], None]) -> None:
        """Check a bounded stream without loading large archives or source files into memory."""
        budget()
        source = (
            self.fallback.open(self.op['prefix'] + relative)
            if installed
            else (Path(self.op['staging']) / relative).open('rb')
        )
        hasher = hashlib.sha256()
        observed = 0
        with source as stream:
            while True:
                budget()
                chunk = stream.read(64 << 10)
                if not chunk:
                    break
                observed += len(chunk)
                require(observed <= size, 'stored_digest_mismatch')
                hasher.update(chunk)
        budget()
        require(observed == size and hasher.hexdigest() == sha256, 'stored_digest_mismatch')
