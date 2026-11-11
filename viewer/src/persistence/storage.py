"""Small S3 object contract and explicit offline mocks; no provider APIs."""

import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ParamValidationError,
    PartialCredentialsError,
)
from viewer.config import Config
from viewer.errors import StorageError, ViewerError
from viewer.util import safe_path

LOGGER = logging.getLogger(__name__)


def _identifier(value: object, pattern: str, limit: int) -> str | None:
    """Accept bounded provider identifiers without arbitrary text or URL syntax."""
    return value if isinstance(value, str) and len(value) <= limit and re.fullmatch(pattern, value) else None


def _run_id(key: str) -> str | None:
    """Recognize canonical run UUIDs only at known storage layout positions."""
    parts = key.split('/')
    for index, part in enumerate(parts):
        offset = 2 if part == 'runs' else 3 if part in ('bundle-index', 'bundle-data') else 0
        if not offset or index + offset >= len(parts):
            continue
        if offset == 3 and parts[index + 1] != 'v1':
            continue
        candidate = parts[index + offset]
        try:
            if str(UUID(candidate)) == candidate:
                return candidate
        except ValueError:
            pass
    return None


def _diagnostic_fields(key: object, response: dict | None = None) -> dict:
    """Project an explicit allowlist; never retain SDK messages, bodies or headers."""
    response = response or {}
    metadata = response.get('ResponseMetadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    error = response.get('Error')
    error = error if isinstance(error, dict) else {}
    status, retries = metadata.get('HTTPStatusCode'), metadata.get('RetryAttempts')
    return {
        's3_key_sha256': hashlib.sha256(key.encode()).hexdigest() if isinstance(key, str) else None,
        's3_run_id': _run_id(key) if isinstance(key, str) else None,
        's3_http_status': status if type(status) is int and 100 <= status <= 599 else None,
        's3_retry_attempts': retries if type(retries) is int and 0 <= retries <= 10000 else None,
        's3_error_code': _identifier(error.get('Code'), r'[A-Za-z0-9_]+', 64),
        's3_request_id': _identifier(metadata.get('RequestId'), r'[A-Za-z0-9_+=/.-]+', 128),
    }


def _log_storage(method: str, fields: dict, started: float, error: BaseException | None = None) -> None:
    """Log structured classifications without exception formatting or traceback data."""
    details = {
        **fields,
        's3_method': method,
        's3_duration_seconds': max(0.0, time.monotonic() - started),
        's3_exception_type': type(error).__name__ if error is not None else None,
    }
    LOGGER.log(
        logging.WARNING if error is not None else logging.DEBUG,
        'S3 operation failed: %s' if error is not None else 'S3 operation completed: %s',
        json.dumps(details, sort_keys=True),
        extra=details,
    )


def _retryable_response(status: object, code: object) -> bool:
    """Keep throttling transient while rejecting authorization and malformed requests."""
    if code in (
        'SlowDown',
        'Throttling',
        'ThrottlingException',
        'RequestLimitExceeded',
        'RequestTimeout',
    ):
        return True
    if code in (
        'AccessDenied',
        'InvalidAccessKeyId',
        'SignatureDoesNotMatch',
        'ExpiredToken',
        'InvalidToken',
        'NotImplemented',
        'UnsupportedOperation',
    ):
        return False
    return not (type(status) is int and 400 <= status < 500 and status not in (408, 429))


class _UnconfirmedWrite(StorageError):
    """A conditional PUT may have committed before its retryable failure."""


class _ObjectExists(Exception):
    """Signal a conditional-create collision without changing public storage errors."""


class ByteReader(Protocol):
    """Read binary chunks without requiring seekable or writable storage."""

    def read(self, size: int = -1, /) -> bytes:
        """Read up to the requested number of bytes."""
        ...


class ClosableReader(ByteReader, Protocol):
    """Allow SDK and test bodies to release their underlying connection."""

    def close(self) -> None:
        """Close the response stream."""
        ...


class StoredBody(ClosableReader, Protocol):
    """Expose the lifetime operations shared by file and object-store bodies."""

    def close(self) -> None:
        """Release the underlying stream."""
        ...

    def __enter__(self) -> Self:
        """Enter the stream's ownership scope."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> bool | None:
        """Release the stream when its ownership scope ends."""
        ...


class Store:
    """Define the bounded object operations used by the viewer service."""

    def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
        """List current object metadata only when the backend supports inventories."""
        raise ViewerError('storage_stats_unavailable', 'Object inventory is unavailable.', 503)

    def get(self, key: str, limit: int = 64 << 20) -> bytes:
        """Read one object and reject content larger than the supplied limit."""
        with self.open(key) as body:
            data = body.read(limit + 1)
        if len(data) > limit:
            raise ViewerError('object_limit', 'Stored object exceeds configured limit.', 413)
        return data

    def put(self, key: str, data: bytes) -> None:
        """Store the complete byte content under a validated object key."""
        raise NotImplementedError

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Create an immutable object; return false without changing an existing key."""
        raise NotImplementedError

    def put_file_if_absent(self, key: str, path: str | Path, *, deadline: float | None = None) -> bool:
        """Create an immutable object from a file without replacing an existing key."""
        raise NotImplementedError

    def compare_and_swap(self, key: str, expected: bytes | None, data: bytes) -> bool:
        """Replace a small registry object only when its current bytes match expected."""
        raise NotImplementedError

    def open(self, key: str) -> StoredBody:
        """Open an object as a closable binary stream."""
        raise NotImplementedError

    def list(self, prefix: str) -> Iterator[str]:
        """Iterate object keys beneath the requested prefix."""
        raise NotImplementedError

    def put_file(self, key: str, path: str | Path, *, deadline: float | None = None) -> None:
        """Upload a file while enforcing the optional transfer deadline."""
        raise NotImplementedError

    def copy(self, source: str, target: str) -> None:
        """Copy an existing object to the target key."""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        """Remove the object if it exists."""
        raise NotImplementedError


class TimedReader:
    """Enforce a transfer deadline while the SDK reads upload chunks."""

    def __init__(self, stream: ByteReader, deadline: float | None = None) -> None:
        """Bind the storage resources used by this instance."""
        self.stream, self.deadline = stream, deadline

    def read(self, size: int = -1) -> bytes:
        """Read a bounded chunk while preserving storage error handling."""
        if size < 0:
            raise StorageError()
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise StorageError()
        return self.stream.read(size)

    def __getattr__(self, name: str) -> object:
        """Forward additional file operations required by the SDK."""
        return getattr(self.stream, name)


class S3Body:
    """Translate SDK body failures without exposing service internals."""

    def __init__(
        self,
        body: ClosableReader,
        *,
        key: str | None = None,
        response: dict | None = None,
    ) -> None:
        """Bind the storage resources used by this instance."""
        self.body = body
        self.diagnostic_fields = _diagnostic_fields(key, response)

    def read(self, size: int = -1) -> bytes:
        """Read a bounded chunk while preserving storage error handling."""
        started = time.monotonic()
        try:
            return self.body.read(size)
        except (BotoCoreError, OSError) as error:
            _log_storage('get_object.read', self.diagnostic_fields, started, error)
            raise StorageError() from None

    def close(self) -> None:
        """Close the underlying response body."""
        self.body.close()

    def __enter__(self) -> Self:
        """Keep this body open for the surrounding context."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close the body when its context exits."""
        self.close()


class S3Store(Store):
    """Implement the object contract using the configured S3 client."""

    def __init__(self, config: Config, client: object | None = None) -> None:
        """Bind the storage resources used by this instance."""
        self.bucket, self.prefix = config.bucket, config.prefix.strip('/')
        self.client = client or boto3.client(
            's3',
            endpoint_url=config.endpoint,
            region_name=config.region,
            config=BotoConfig(
                connect_timeout=10,
                read_timeout=30,
                retries={'max_attempts': 3, 'mode': 'standard'},
            ),
        )

    def _key(self, key: str) -> str:
        """Prefix a validated object key with the configured namespace."""
        safe_path(key.rstrip('/'))
        return self.prefix + '/' + key if self.prefix else key

    def _call(self, method: str, **kwargs: object) -> dict:
        """Invoke the SDK and translate storage failures to public errors."""
        started = time.monotonic()
        key = kwargs.get('Key', kwargs.get('Prefix'))
        conditional = method == 'put_object' and (kwargs.get('IfNoneMatch') == '*' or 'IfMatch' in kwargs)
        try:
            response = getattr(self.client, method)(Bucket=self.bucket, **kwargs)
        except ClientError as error:
            _log_storage(method, _diagnostic_fields(key, error.response), started, error)
            status = error.response.get('ResponseMetadata', {}).get('HTTPStatusCode')
            code = error.response.get('Error', {}).get('Code')
            if conditional and status == 412:
                raise _ObjectExists() from None
            if conditional and (status == 501 or code in ('NotImplemented', 'UnsupportedOperation')):
                raise ViewerError(
                    'conditional_write_unsupported',
                    'Storage does not support conditional writes.',
                    503,
                ) from None
            retryable = _retryable_response(status, code)
            if conditional and retryable:
                raise _UnconfirmedWrite() from None
            if error.response['Error']['Code'] in ('NoSuchKey', '404', 'NotFound'):
                raise ViewerError('not_found', 'Object not found.', 404) from None
            raise StorageError(retryable=retryable) from None
        except BotoCoreError as error:
            _log_storage(method, _diagnostic_fields(key), started, error)
            if isinstance(error, (NoCredentialsError, PartialCredentialsError)):
                raise StorageError(retryable=False) from None
            if conditional:
                if isinstance(error, ParamValidationError):
                    raise ViewerError(
                        'conditional_write_unsupported',
                        'SDK cannot perform this conditional write.',
                        503,
                    ) from None
                raise _UnconfirmedWrite() from None
            raise StorageError(retryable=not isinstance(error, ParamValidationError)) from None
        _log_storage(method, _diagnostic_fields(key, response), started)
        return response

    def open(self, key: str) -> StoredBody:
        """Open an object as a closable binary stream."""
        object_key = self._key(key)
        response = self._call('get_object', Key=object_key)
        return S3Body(response['Body'], key=object_key, response=response)

    def put(self, key: str, data: bytes) -> None:
        """Store the complete byte content under a validated object key."""
        self._call('put_object', Key=self._key(key), Body=data)

    def _put_if_absent(self, key: str, **kwargs: object) -> bool:
        """Use the provider's atomic condition; errors never fall back to overwrite."""
        try:
            self._call('put_object', Key=self._key(key), IfNoneMatch='*', **kwargs)
        except _ObjectExists:
            return False
        return True

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Create only when the server confirms that the key is absent."""
        return self._put_if_absent(key, Body=data)

    def put_file_if_absent(self, key: str, path: str | Path, *, deadline: float | None = None) -> bool:
        """Stream a conditional single PUT with the same deadline as ordinary upload."""
        with Path(path).open('rb') as body:
            return self._put_if_absent(
                key,
                Body=TimedReader(body, deadline),
                ContentLength=os.fstat(body.fileno()).st_size,
            )

    def compare_and_swap(self, key: str, expected: bytes | None, data: bytes) -> bool:
        """Fence registry updates by observed ETag; verify an ambiguous write by readback."""
        condition = {'IfNoneMatch': '*'}
        if expected is not None:
            try:
                response = self._call('get_object', Key=self._key(key))
            except ViewerError as error:
                if error.status == 404:
                    return False
                raise
            with S3Body(response['Body'], key=self._key(key), response=response) as body:
                if body.read(len(expected) + 1) != expected:
                    return False
            etag = response.get('ETag')
            if not isinstance(etag, str) or not etag or len(etag) > 1024 or any(c in etag for c in '\r\n'):
                raise StorageError()
            condition = {'IfMatch': etag}
        try:
            self._call('put_object', Key=self._key(key), Body=data, **condition)
        except _ObjectExists:
            return False
        except _UnconfirmedWrite:
            # A committed PUT can lose its reply. Equality confirms only these exact bytes.
            try:
                if self.get(key, len(data)) == data:
                    return True
            except (ViewerError, OSError):
                pass
            raise
        return True

    def put_file(self, key: str, path: str | Path, *, deadline: float | None = None) -> None:
        # Limits keep individual uploads <=1 GiB, below the single PUT limit.
        """Upload a file while enforcing the optional transfer deadline."""
        with Path(path).open('rb') as body:
            self._call(
                'put_object',
                Key=self._key(key),
                Body=TimedReader(body, deadline),
                ContentLength=Path(path).stat().st_size,
            )

    def list(self, prefix: str) -> Iterator[str]:
        """Iterate object keys beneath the requested prefix."""
        continuation = None
        while True:
            kwargs = {'Prefix': self._key(prefix)}
            if continuation:
                kwargs['ContinuationToken'] = continuation
            response = self._call('list_objects_v2', **kwargs)
            for entry in response.get('Contents', []):
                key = entry['Key']
                yield key[len(self.prefix) + 1 :] if self.prefix else key
            if not response.get('IsTruncated'):
                break
            continuation = response['NextContinuationToken']

    def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
        """Enumerate sizes under the configured prefix without downloading bodies."""
        prefix = self.prefix + '/' if self.prefix else ''
        continuation = None
        while True:
            if check is not None:
                check()
            kwargs = {'Prefix': prefix}
            if continuation:
                kwargs['ContinuationToken'] = continuation
            response = self._call('list_objects_v2', **kwargs)
            for entry in response.get('Contents', []):
                if not entry['Key'].startswith(prefix):
                    raise ValueError('Inventory key is outside the configured prefix')
                yield entry['Key'][len(prefix) :], entry['Size']
            if not response.get('IsTruncated'):
                break
            continuation = response['NextContinuationToken']

    def copy(self, source: str, target: str) -> None:
        """Copy an existing object to the target key."""
        self._call(
            'copy_object',
            Key=self._key(target),
            CopySource={'Bucket': self.bucket, 'Key': self._key(source)},
        )

    def delete(self, key: str) -> None:
        """Remove the object if it exists."""
        self._call('delete_object', Key=self._key(key))


class MemoryStore(Store):
    """Offline test object store; failure injection subclasses use the same contract."""

    def __init__(self) -> None:
        """Bind the storage resources used by this instance."""
        self.objects: dict[str, bytes] = {}
        self.lock = threading.RLock()

    def open(self, key: str) -> StoredBody:
        """Open an object as a closable binary stream."""
        with self.lock:
            if key not in self.objects:
                raise ViewerError('not_found', 'Object not found.', 404)
            return io.BytesIO(self.objects[key])

    def put(self, key: str, data: bytes) -> None:
        """Store the complete byte content under a validated object key."""
        safe_path(key)
        with self.lock:
            self.objects[key] = bytes(data)

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Keep the existence check and fault-injectable write in one critical section."""
        safe_path(key)
        with self.lock:
            if key in self.objects:
                return False
            self.put(key, data)
            return True

    def put_file_if_absent(self, key: str, path: str | Path, *, deadline: float | None = None) -> bool:
        """Read the complete source before atomically attempting immutable creation."""
        parts = []
        with Path(path).open('rb') as stream:
            reader = TimedReader(stream, deadline)
            while data := reader.read(64 << 10):
                parts.append(data)
        return self.put_if_absent(key, b''.join(parts))

    def compare_and_swap(self, key: str, expected: bytes | None, data: bytes) -> bool:
        """Compare and write under the same reentrant lock, retaining fault injection."""
        safe_path(key)
        with self.lock:
            if self.objects.get(key) != expected:
                return False
            self.put(key, data)
            return True

    def put_file(self, key: str, path: str | Path, *, deadline: float | None = None) -> None:
        """Upload a file while enforcing the optional transfer deadline."""
        parts = []
        with Path(path).open('rb') as stream:
            reader = TimedReader(stream, deadline)
            while data := reader.read(64 << 10):
                parts.append(data)
        self.put(key, b''.join(parts))

    def list(self, prefix: str) -> Iterator[str]:
        """Iterate object keys beneath the requested prefix."""
        with self.lock:
            return iter(sorted(k for k in self.objects if k.startswith(prefix)))

    def copy(self, source: str, target: str) -> None:
        """Copy an existing object to the target key."""
        self.put(target, self.get(source, 1 << 40))

    def delete(self, key: str) -> None:
        """Remove the object if it exists."""
        with self.lock:
            self.objects.pop(key, None)


class MockFileStore(Store):
    """Persist an offline S3 mock on the current host for local HTTP/UI checks."""

    def __init__(self, root: str | Path) -> None:
        """Bind the storage resources used by this instance."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        """Resolve a validated object key beneath the mock storage root."""
        return self.root / safe_path(key)

    def open(self, key: str) -> StoredBody:
        """Open an object as a closable binary stream."""
        try:
            return self.path(key).open('rb')
        except FileNotFoundError:
            raise ViewerError('not_found', 'Object not found.', 404) from None

    def put(self, key: str, data: bytes) -> None:
        """Store the complete byte content under a validated object key."""
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _create_from_stream(self, key: str, source: ByteReader) -> bool:
        """Publish fully synced bytes with atomic link; a race never exposes partial data."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.root.parent, prefix='.' + self.root.name + '.conditional-') as output:
            shutil.copyfileobj(source, output, 64 << 10)
            output.flush()
            os.fsync(output.fileno())
            try:
                os.link(output.name, target)
            except FileExistsError:
                return False
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return True

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Create a complete object atomically without replacing an existing inode."""
        return self._create_from_stream(key, io.BytesIO(data))

    def put_file_if_absent(self, key: str, path: str | Path, *, deadline: float | None = None) -> bool:
        """Stream a file to a private temporary inode before publishing its key."""
        with Path(path).open('rb') as source:
            return self._create_from_stream(key, TimedReader(source, deadline))

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        """Coordinate processes through a stable sibling lock outside listed payloads."""
        root = self.root.resolve()
        lock_path = root.parent / ('.' + root.name + '.store.lock')
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def compare_and_swap(self, key: str, expected: bytes | None, data: bytes) -> bool:
        """Compare under an interprocess lock and atomically replace a synced registry file."""
        target = self.path(key)
        with self._registry_lock():
            if expected is None:
                return self.put_if_absent(key, data)
            try:
                with target.open('rb') as current:
                    if current.read(len(expected) + 1) != expected:
                        return False
            except FileNotFoundError:
                return False
            with NamedTemporaryFile(
                dir=self.root.parent,
                prefix='.' + self.root.name + '.conditional-',
                delete=False,
            ) as output:
                temporary = Path(output.name)
                try:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    os.replace(temporary, target)
                    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                finally:
                    temporary.unlink(missing_ok=True)
            return True

    def put_file(self, key: str, path: str | Path, *, deadline: float | None = None) -> None:
        """Upload a file while enforcing the optional transfer deadline."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open('rb') as source, target.open('wb') as output:
            shutil.copyfileobj(TimedReader(source, deadline), output, 64 << 10)

    def list(self, prefix: str) -> Iterator[str]:
        """Iterate object keys beneath the requested prefix."""
        return iter(
            sorted(
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob('*')
                if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix)
            )
        )

    def copy(self, source: str, target: str) -> None:
        """Copy an existing object to the target key."""
        self.put_file(target, self.path(source))

    def delete(self, key: str) -> None:
        """Remove the object if it exists."""
        self.path(key).unlink(missing_ok=True)
