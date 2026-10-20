"""Bounded streaming tar.gz import; never extract attacker paths with tarfile."""

import gzip
import hashlib
import logging
import os
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from viewer.config import Limits
from viewer.contracts import require, validate_identity
from viewer.errors import ViewerError
from viewer.util import parse_json, safe_path

CHUNK = 64 << 10


@dataclass(frozen=True)
class ImportedRun:
    archive: Path
    directory: Path
    archive_sha256: str
    report: dict
    basket: dict
    manifest: dict


class BoundedGzip:
    def __init__(self, stream, compressed_size, limits, deadline):
        self.stream, self.limits, self.deadline = stream, limits, deadline
        self.cap = min(limits.unpacked_bytes, compressed_size * limits.expansion_ratio)
        self.total = 0

    def read(self, size=-1):
        if time.monotonic() > self.deadline:
            raise ViewerError('transfer_timeout', 'Archive validation deadline exceeded.', 413)
        if size < 0 or size > self.limits.file_bytes:
            raise ViewerError('archive_limit', 'Archive read exceeds member limit.', 413)
        data = self.stream.read(size)
        self.total += len(data)
        if self.total > self.cap:
            raise ViewerError('archive_limit', 'Archive expansion exceeds configured limits.', 413)
        return data


def validate_archive(path: Path, destination: Path, limits: Limits, *, deadline=None) -> ImportedRun:
    deadline = deadline if deadline is not None else time.monotonic() + limits.transfer_seconds
    if destination.exists():
        raise ViewerError('staging_exists', 'Staging destination already exists.', 409)
    size = path.stat().st_size
    if size > limits.archive_bytes:
        raise ViewerError('archive_limit', 'Compressed archive exceeds limit.', 413)
    archive_hash = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(CHUNK):
            if time.monotonic() > deadline:
                raise ViewerError('transfer_timeout', 'Archive validation deadline exceeded.', 413)
            archive_hash.update(data)
    inventory = {}
    destination.mkdir(mode=0o700, parents=True)

    class BoundedTarInfo(tarfile.TarInfo):
        def _proc_member(self, tar):
            # tarfile reads PAX/GNU extended headers before yielding a member.
            extended = (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)
            if self.type not in (*extended, tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.CONTTYPE):
                raise ViewerError('unsafe_archive', 'Archive contains non-regular members.')
            if self.size > limits.file_bytes or (self.type in extended and self.size > limits.tar_header_bytes):
                raise ViewerError('archive_limit', 'Tar header exceeds configured limit.', 413)
            return super()._proc_member(tar)

    try:
        with path.open('rb') as compressed, gzip.GzipFile(fileobj=compressed) as inflated:
            bounded = BoundedGzip(inflated, size, limits, deadline)
            with tarfile.open(fileobj=bounded, mode='r|', tarinfo=BoundedTarInfo) as archive:
                for member in archive:
                    name = safe_path(member.name, limits.path_bytes)
                    if name in inventory or not member.isfile() or member.issparse():
                        raise ViewerError('unsafe_archive', 'Archive contains duplicate or non-regular members.')
                    if len(inventory) >= limits.files or member.size > limits.file_bytes:
                        raise ViewerError('archive_limit', 'Archive members exceed configured limits.', 413)
                    if name.endswith('.json') and member.size > limits.json_bytes:
                        raise ViewerError('archive_limit', 'JSON artifact exceeds configured limit.', 413)
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    require(source is not None)
                    hasher, count = hashlib.sha256(), 0
                    with target.open('xb') as output:
                        os.chmod(target, 0o600)
                        while data := source.read(CHUNK):
                            count += len(data)
                            if count > limits.file_bytes:
                                raise ViewerError('archive_limit', 'Member exceeds configured limit.', 413)
                            output.write(data)
                            hasher.update(data)
                    require(count == member.size)
                    inventory[name] = {'path': name, 'size_bytes': count, 'sha256': hasher.hexdigest()}
            # Enforce limits on the entire gzip stream, including trailing data.
            while bounded.read(min(CHUNK, limits.file_bytes)):
                pass
        for name in ('report.json', 'basket.json', 'manifest.json'):
            require(name in inventory, 'missing_artifact')

        def read_file(name):
            if inventory[name]['size_bytes'] > limits.json_bytes:
                raise ViewerError('archive_limit', 'Structured artifact exceeds configured limit.', 413)
            return (destination / name).read_bytes()

        manifest = parse_json(read_file('manifest.json'))
        from viewer.contracts import validate_document

        validate_document('archive', manifest)
        declared = {f['path']: f for f in manifest['files']}
        require(len(declared) == len(manifest['files']))
        require(list(declared) == sorted(declared))
        require(set(inventory) == set(declared) | {'manifest.json'})
        require(all(declared[k] == inventory[k] for k in declared), 'archive_digest_mismatch')
        report, basket = parse_json(read_file('report.json')), parse_json(read_file('basket.json'))
        validate_identity(report, basket, read_file('basket.json'), manifest, inventory, read_file)
        if basket['meta']['schema_version'] == 1:
            logging.getLogger('viewer.basket').warning(
                'Basket schema v1 is deprecated; use a v2 export for new runs. This frozen v1 basket remains supported.'
            )
        return ImportedRun(path, destination, archive_hash.hexdigest(), report, basket, manifest)
    except (ViewerError, OSError, ValueError, tarfile.TarError, EOFError, OverflowError, RecursionError) as error:
        shutil.rmtree(destination)
        if isinstance(error, ViewerError):
            raise
        raise ViewerError('invalid_archive', 'Invalid or unreadable archive.') from None
