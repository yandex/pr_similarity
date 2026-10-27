"""Small S3 object contract and explicit offline mocks; no provider APIs."""

import io
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from viewer.config import Config
from viewer.errors import StorageError, ViewerError
from viewer.util import safe_path


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
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None, /
    ) -> bool | None:
        """Release the stream when its ownership scope ends."""
        ...


class Store:
    """Define the bounded object operations used by the viewer service."""

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

    def __init__(self, body: ClosableReader) -> None:
        """Bind the storage resources used by this instance."""
        self.body = body

    def read(self, size: int = -1) -> bytes:
        """Read a bounded chunk while preserving storage error handling."""
        try:
            return self.body.read(size)
        except (BotoCoreError, OSError):
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
            config=BotoConfig(connect_timeout=10, read_timeout=30, retries={'max_attempts': 3, 'mode': 'standard'}),
        )

    def _key(self, key: str) -> str:
        """Prefix a validated object key with the configured namespace."""
        safe_path(key.rstrip('/'))
        return self.prefix + '/' + key if self.prefix else key

    def _call(self, method: str, **kwargs: object) -> dict:
        """Invoke the SDK and translate storage failures to public errors."""
        try:
            return getattr(self.client, method)(Bucket=self.bucket, **kwargs)
        except ClientError as e:
            if e.response['Error']['Code'] in ('NoSuchKey', '404', 'NotFound'):
                raise ViewerError('not_found', 'Object not found.', 404) from None
            raise StorageError() from None
        except BotoCoreError:
            raise StorageError() from None

    def open(self, key: str) -> StoredBody:
        """Open an object as a closable binary stream."""
        return S3Body(self._call('get_object', Key=self._key(key))['Body'])

    def put(self, key: str, data: bytes) -> None:
        """Store the complete byte content under a validated object key."""
        self._call('put_object', Key=self._key(key), Body=data)

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

    def copy(self, source: str, target: str) -> None:
        """Copy an existing object to the target key."""
        self._call('copy_object', Key=self._key(target), CopySource={'Bucket': self.bucket, 'Key': self._key(source)})

    def delete(self, key: str) -> None:
        """Remove the object if it exists."""
        self._call('delete_object', Key=self._key(key))


class MemoryStore(Store):
    """Offline test object store; failure injection subclasses use the same contract."""

    def __init__(self) -> None:
        """Bind the storage resources used by this instance."""
        self.objects: dict[str, bytes] = {}

    def open(self, key: str) -> StoredBody:
        """Open an object as a closable binary stream."""
        if key not in self.objects:
            raise ViewerError('not_found', 'Object not found.', 404)
        return io.BytesIO(self.objects[key])

    def put(self, key: str, data: bytes) -> None:
        """Store the complete byte content under a validated object key."""
        safe_path(key)
        self.objects[key] = bytes(data)

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
        return iter(sorted(k for k in self.objects if k.startswith(prefix)))

    def copy(self, source: str, target: str) -> None:
        """Copy an existing object to the target key."""
        self.put(target, self.get(source, 1 << 40))

    def delete(self, key: str) -> None:
        """Remove the object if it exists."""
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
