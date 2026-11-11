"""Explicit resource snapshots with one bounded, cancellable object inventory."""

import os
import shutil
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.storage import Store
from viewer.util import utc_now

if TYPE_CHECKING:
    from viewer.service import ViewerService


def local_resources(service: 'ViewerService') -> dict:
    """Measure local capacity under the caller's service lock without remote reads."""
    config = service.config
    disk = shutil.disk_usage(config.runtime)
    cache = None
    if config.publication_storage == 'local':
        with service.db.lock:
            paths = service.db.connection.execute(
                '''SELECT json_extract(data,'$.local_path'),max(json_extract(data,'$.cache_bytes')) '''
                '''FROM local_records WHERE json_extract(data,'$.local_path') IS NOT NULL '''
                '''GROUP BY json_extract(data,'$.local_path')'''
            ).fetchall()
        root = (config.runtime / 'local-cache').resolve()
        present = [
            (Path(path), size)
            for path, size in paths
            if Path(path).resolve().is_relative_to(root) and Path(path).is_dir()
        ]
        cache = {
            'used_bytes': sum(size for _, size in present),
            'limit_bytes': config.cache_max_bytes,
            'resident_versions': len(present),
            'idle_ttl_seconds': config.cache_idle_ttl_seconds,
        }
    memory = None
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith(('MemTotal:', 'MemAvailable:')):
                values[line.split(':', 1)[0]] = int(line.split()[1]) * 1024
        if 0 <= values['MemAvailable'] <= values['MemTotal']:
            memory = {'used_bytes': values['MemTotal'] - values['MemAvailable'], 'total_bytes': values['MemTotal']}
    except (OSError, KeyError, ValueError):
        pass
    cpu = None
    try:
        cores = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
        cpu = {'load_1m': os.getloadavg()[0], 'cores': cores}
    except (OSError, AttributeError):
        pass
    return {
        'sampled_at': utc_now(),
        'cache': cache,
        'memory': memory,
        'cpu': cpu,
        'disk': {'used_bytes': disk.used, 'total_bytes': disk.total, 'free_bytes': disk.free},
    }


class ResourceSnapshots:
    """Keep read-only collection outside catalogue locks and preserve last-good totals."""

    def __init__(self, config: Config, store: Store) -> None:
        """Prepare one optional inventory worker and its last complete result."""
        self.config, self.store = config, store
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.value: dict = {}
        self.last_good: dict | None = None

    def current(self) -> dict:
        """Return a detached copy without starting a storage request."""
        with self.lock:
            return deepcopy(self.value)

    def request(self, local: dict) -> dict:
        """Measure one requested snapshot, coalescing overlapping inventory work."""
        with self.lock:
            if self.stop.is_set():
                raise ViewerError('service_stopping', 'Resource collection is stopping.', 503)
            if self.thread and self.thread.is_alive():
                return deepcopy(self.value)
            self.value = deepcopy(local)
            if self.config.storage != 's3':
                self.value['object_storage'] = None
                return deepcopy(self.value)
            self.value['object_storage'] = {**self._previous(), 'status': 'collecting', 'error_code': None}
            self.thread = threading.Thread(target=self._collect, name='viewer-resource-inventory', daemon=True)
            self.thread.start()
            return deepcopy(self.value)

    def _previous(self) -> dict:
        """Copy the last complete storage totals or an unknown-value envelope."""
        return (
            deepcopy(self.last_good)
            if self.last_good
            else {
                'used_bytes': None,
                'object_count': None,
                'capacity_bytes': None,
                'groups': {},
                'sampled_at': None,
            }
        )

    def _collect(self) -> None:
        """Collect bounded metadata totals while isolating optional monitoring failures."""
        deadline = time.monotonic() + self.config.storage_stats_timeout_seconds
        count = 0
        size = 0
        groups: dict[str, int] = {}
        categories = {
            'runs': 'publications',
            'bundle-data': 'archives',
            'backup': 'backups',
            'trash': 'trash',
            'checkpoint-data': 'indexes',
            'checkpoints': 'indexes',
            'bundle-index': 'indexes',
            'bundle-registry': 'indexes',
            'settings': 'indexes',
        }
        error_code = 'storage_unavailable'

        def check() -> None:
            """Stop before further enumeration after cancellation or the deadline."""
            nonlocal error_code
            if self.stop.is_set():
                error_code = 'collection_cancelled'
                raise InterruptedError()
            if time.monotonic() >= deadline:
                error_code = 'storage_stats_timeout'
                raise TimeoutError()

        try:
            for key, length in self.store.iter_sizes(check=check):
                check()
                count += 1
                if count > self.config.storage_stats_max_objects:
                    error_code = 'storage_stats_limit'
                    raise ValueError()
                if not isinstance(key, str) or type(length) is not int or length < 0:
                    raise ValueError()
                size += length
                group = categories.get(key.split('/', 1)[0], 'other')
                groups[group] = groups.get(group, 0) + length
            if self.stop.is_set() or time.monotonic() >= deadline:
                error_code = 'collection_cancelled' if self.stop.is_set() else 'storage_stats_timeout'
                raise InterruptedError()
            result = {
                'status': 'ready',
                'used_bytes': size,
                'object_count': count,
                'capacity_bytes': None,
                'groups': groups,
                'sampled_at': utc_now(),
                'error_code': None,
            }
            with self.lock:
                self.last_good = deepcopy(result)
                self.value['object_storage'] = result
        except Exception:
            with self.lock:
                self.value['object_storage'] = {**self._previous(), 'status': 'unavailable', 'error_code': error_code}

    def close(self, timeout: float | None = None) -> bool:
        """Cancel future requests and join the inventory within the shutdown budget."""
        self.stop.set()
        with self.lock:
            thread = self.thread
        if thread:
            thread.join(self.config.shutdown_seconds if timeout is None else timeout)
        return thread is None or not thread.is_alive()
