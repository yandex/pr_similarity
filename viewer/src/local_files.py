"""Immutable disk versions, bounded reservations and reader-pinned cache files."""

import os
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Self

from viewer.bundles import BundleIdentity
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.storage import ClosableReader, Store
from viewer.util import json_bytes, parse_json, safe_path
from viewer.validation.archive import ImportedRun


def fsync_directory(path: Path) -> None:
    """Persist directory entries after replacing a durable file or tree."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_ancestors(path: Path, boundary: Path) -> None:
    """Persist each directory from the installed path through its owning boundary."""
    while path.is_relative_to(boundary):
        fsync_directory(path)
        if path == boundary:
            break
        path = path.parent


def atomic_json(path: Path, value: dict) -> None:
    """Replace private JSON only after its complete bytes reach the local disk."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.json-')
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class PinnedBody:
    """Keep a version resident until an opened object or streaming body closes."""

    def __init__(self, stream: ClosableReader, release: Callable[[], None]) -> None:
        """Bind an open binary reader to the callback that releases its version pin."""
        self.stream, self.release = stream, release
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        """Read bytes while retaining the pin until the response body is closed."""
        return self.stream.read(size)

    def close(self) -> None:
        """Close the underlying reader once and release its pin even if close fails."""
        if not self.closed:
            self.closed = True
            try:
                self.stream.close()
            finally:
                self.release()

    def __enter__(self) -> Self:
        """Keep this body and its version pin owned by the surrounding context."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Close the body on normal exit or failure without suppressing the failure."""
        self.close()


class LocalFiles:
    """Own cache paths; no method infers a publication from file existence alone."""

    def __init__(self, config: Config) -> None:
        """Create private cache, marker and staging roots with independent pin accounting."""
        self.config = config
        self.root = config.runtime / 'local-cache'
        self.metadata = config.runtime / 'local-metadata'
        self.temporary = config.runtime / 'local-staging'
        for path in (self.root, self.metadata, self.temporary):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.readers: dict[Path, int] = {}
        self.reserved = 0
        self.pending_reserved = 0

    def version_path(self, identity: BundleIdentity, installation_id: uuid.UUID | None = None) -> Path:
        """Locate immutable archive bytes and extracted files for one generation."""
        name = f'{identity.generation}-{identity.archive_sha256}'
        if installation_id is not None:
            name += f'-{installation_id}'
        return self.root / identity.owner / identity.run_id / name

    def marker_path(self, identity: BundleIdentity) -> Path:
        """Locate the durable marker retained independently of the evictable cache."""
        return (
            self.metadata / identity.owner / identity.run_id / f'{identity.generation}-{identity.archive_sha256}.json'
        )

    def marker(self, identity: BundleIdentity) -> dict:
        """Read the retained publication marker and reject non-object data."""
        value = parse_json(self.marker_path(identity).read_bytes())
        if not isinstance(value, dict):
            raise ViewerError('invalid_local_marker', 'Local marker must be an object.')
        return value

    @contextmanager
    def reserve(
        self, size: int, *, pending_bytes: int = 0, pending_limit: bool = True, account_disk: bool = True
    ) -> Iterator[None]:
        """Reserve bounded physical space before a concurrent operation writes it."""
        with self.lock:
            if (
                size < 0
                or (
                    pending_limit
                    and pending_bytes + self.pending_reserved + size > self.config.pending_backup_max_bytes
                )
                or (
                    account_disk
                    and shutil.disk_usage(self.root).free - self.reserved - size < self.config.disk_reserve_bytes
                )
            ):
                raise ViewerError('local_capacity', 'Insufficient local storage capacity; retry later.', 503)
            self.reserved += size if account_disk else 0
            self.pending_reserved += size if pending_limit else 0
        try:
            yield
        finally:
            with self.lock:
                self.reserved -= size if account_disk else 0
                self.pending_reserved -= size if pending_limit else 0

    def install(
        self,
        imported: ImportedRun,
        identity: BundleIdentity,
        marker: dict,
        *,
        replace_existing: bool = False,
        installation_id: uuid.UUID | None = None,
    ) -> Path:
        """Copy verified bytes to private inodes, fsync, and atomically install them."""
        target = self.version_path(identity, installation_id)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if replace_existing and installation_id is None and target.exists():
            target = target.with_name(target.name + '-' + str(uuid.uuid4()))
        stage = Path(tempfile.mkdtemp(dir=self.temporary, prefix='version-'))
        try:
            shutil.copyfile(imported.archive, stage / 'archive.tar.gz')
            shutil.copytree(imported.directory, stage / 'data')
            (stage / 'publication.json').write_bytes(json_bytes(marker))
            directories = [stage]
            for path in stage.rglob('*'):
                if path.is_dir():
                    os.chmod(path, 0o700)
                    directories.append(path)
                else:
                    os.chmod(path, 0o600)
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
                fsync_directory(directory)
            # A normal publish gets a new generation. Cold restoration of the
            # same generation is serialized by its single-flight owner.
            with self.lock:
                if target.exists():
                    raise ViewerError('local_version_exists', 'Local version already exists.', 409)
                os.rename(stage, target)
                fsync_ancestors(target.parent, self.root)
            atomic_json(self.marker_path(identity), marker)
            fsync_ancestors(self.marker_path(identity).parent, self.metadata)
            return target
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def pin(self, path: str | Path) -> None:
        """Acquire a reader reference only for an existing version under the cache root."""
        with self.lock:
            path = Path(path)
            if not path.is_dir() or not path.is_relative_to(self.root):
                raise ViewerError('run_cache_miss', 'Run data is not available locally.', 503)
            self.readers[path] = self.readers.get(path, 0) + 1

    def unpin(self, path: str | Path) -> None:
        """Release one reader reference and remove empty pin counters."""
        with self.lock:
            path = Path(path)
            count = self.readers.get(path, 0)
            if count <= 1:
                self.readers.pop(path, None)
            else:
                self.readers[path] = count - 1

    def open(self, path: Path, relative: str) -> PinnedBody:
        """Pin a version before opening its requested file; undo the pin if opening fails."""
        relative = safe_path(relative)
        with self.lock:
            self.pin(path)
            try:
                stream = (path / relative).open('rb')
            except OSError:
                self.unpin(path)
                raise ViewerError('not_found', 'Object not found.', 404) from None
        return PinnedBody(stream, lambda: self.unpin(path))

    def evict(self, path: str | Path) -> bool:
        """Delete only an unpinned version; durable catalogue and marker stay."""
        path = Path(path)
        with self.lock:
            if not path.is_relative_to(self.root) or path == self.root:
                raise ViewerError('unsafe_path', 'Invalid local version path.')
            if self.readers.get(path, 0):
                return False
            if not path.exists():
                return True
            discarded = self.temporary / ('evicted-' + str(uuid.uuid4()))
            os.rename(path, discarded)
        # Recursive unlink is outside the common pin lock: unrelated readers
        # and installers do not wait for a large tree's cleanup.
        fsync_directory(path.parent)
        shutil.rmtree(discarded)
        fsync_directory(self.temporary)
        return True

    def size(self, path: str | Path) -> int:
        """Measure allocated filesystem bytes for cache and reservation accounting."""
        return sum(item.stat().st_blocks * 512 for item in Path(path).rglob('*') if item.is_file())


class VersionStore(Store):
    """Present a pinned disk version through the unchanged publication key contract."""

    def __init__(self, files: LocalFiles, path: Path, prefix: str) -> None:
        """Bind logical publication keys to one pinned local version."""
        self.files, self.path, self.prefix = files, path, prefix

    def open(self, key: str) -> PinnedBody:
        """Open a publication object only beneath this version's logical prefix."""
        safe_path(key)
        if not key.startswith(self.prefix):
            raise ViewerError('not_found', 'Object not found.', 404)
        return self.files.open(self.path, key[len(self.prefix) :])

    def list(self, prefix: str) -> Iterator[str]:
        """Enumerate the exact version prefix without exposing unrelated local paths."""
        if prefix != self.prefix:
            raise ViewerError('not_found', 'Object not found.', 404)
        return iter(
            sorted(
                self.prefix + item.relative_to(self.path).as_posix() for item in self.path.rglob('*') if item.is_file()
            )
        )
