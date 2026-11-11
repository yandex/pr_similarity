"""Immutable object creation refuses replacement, including concurrent writers."""

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import BinaryIO

import boto3
import pytest
import viewer.persistence.storage as storage_module
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from viewer.config import Config
from viewer.errors import StorageError, ViewerError
from viewer.persistence.storage import MemoryStore, MockFileStore, S3Store, Store


@pytest.mark.parametrize('file_backed', [False, True])
def test_create_only_preserves_existing_bytes(tmp_path: Path, file_backed: bool) -> None:
    """Keep original bytes when conditional creation loses to an existing object."""
    store = MockFileStore(tmp_path / 'objects') if file_backed else MemoryStore()
    source = tmp_path / 'archive'
    source.write_bytes(b'second')
    assert store.put_if_absent('runs/item', b'first') is True
    assert store.put_if_absent('runs/item', b'second') is False
    assert store.put_file_if_absent('runs/item', source) is False
    assert store.get('runs/item') == b'first'
    assert store.put_file_if_absent('runs/new', source) is True
    assert store.get('runs/new') == b'second'


@pytest.mark.parametrize('file_backed', [False, True])
def test_conditional_writers_have_one_winner(tmp_path: Path, file_backed: bool) -> None:
    """Publish exactly one complete object when two writers race."""
    first = MockFileStore(tmp_path / 'objects') if file_backed else MemoryStore()
    second = MockFileStore(tmp_path / 'objects') if file_backed else first
    barrier = Barrier(2)

    def write(store: Store, value: bytes) -> bool:
        """Release both writers together and attempt create-only publication."""
        barrier.wait(timeout=5)
        return store.put_if_absent('runs/item', value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, store, value) for store, value in ((first, b'one'), (second, b'two'))]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(results) == [False, True]
    assert first.get('runs/item') == (b'one' if results[0] else b'two')


def test_s3_uses_conditional_put_without_fallback(tmp_path: Path) -> None:
    """Require S3 preconditions and reject providers that cannot enforce them."""
    client = boto3.client(
        's3',
        region_name='us-east-1',
        aws_access_key_id='mock',
        aws_secret_access_key='mock',
    )
    store = S3Store(Config(bucket='bucket', prefix='viewer'), client)
    source = tmp_path / 'archive'
    source.write_bytes(b'archive')
    with Stubber(client) as stub:
        params = {
            'Bucket': 'bucket',
            'Key': 'viewer/runs/item',
            'Body': b'one',
            'IfNoneMatch': '*',
        }
        stub.add_response('put_object', {}, params)
        assert store.put_if_absent('runs/item', b'one') is True
        stub.add_client_error(
            'put_object',
            'PreconditionFailed',
            http_status_code=412,
            expected_params=params,
        )
        assert store.put_if_absent('runs/item', b'one') is False
        stub.add_client_error('put_object', 'NotImplemented', http_status_code=501, expected_params=params)
        with pytest.raises(ViewerError) as caught:
            store.put_if_absent('runs/item', b'one')
        assert caught.value.code == 'conditional_write_unsupported'
        stub.add_response(
            'put_object',
            {},
            {
                'Bucket': 'bucket',
                'Key': 'viewer/runs/new',
                'Body': ANY,
                'ContentLength': 7,
                'IfNoneMatch': '*',
            },
        )
        assert store.put_file_if_absent('runs/new', source) is True
        stub.assert_no_pending_responses()


@pytest.mark.parametrize('file_backed', [False, True])
def test_registry_compare_and_swap_has_no_lost_update(tmp_path: Path, file_backed: bool) -> None:
    """Fence concurrent registry updates against the same observed bytes."""
    first = MockFileStore(tmp_path / 'objects') if file_backed else MemoryStore()
    second = MockFileStore(tmp_path / 'objects') if file_backed else first
    assert first.compare_and_swap('registry/head', None, b'initial') is True
    barrier = Barrier(2)

    def swap(store: Store, value: bytes) -> bool:
        """Race a replacement against the same expected registry value."""
        barrier.wait(timeout=5)
        return store.compare_and_swap('registry/head', b'initial', value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(swap, store, data) for store, data in ((first, b'one'), (second, b'two'))]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(results) == [False, True]
    assert first.get('registry/head') == (b'one' if results[0] else b'two')
    assert first.compare_and_swap('registry/head', b'initial', b'wrong') is False
    assert first.compare_and_swap('registry/head', None, b'wrong') is False
    assert list(first.list('')) == ['registry/head']


def test_mock_failed_write_never_publishes_partial_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep failed staging writes outside the visible mock object namespace."""
    store = MockFileStore(tmp_path / 'objects')

    def fail(source: BinaryIO, target: BinaryIO, length: int) -> None:
        """Write partial staging bytes before simulating a local I/O failure."""
        target.write(b'partial')
        raise OSError('injected write failure')

    monkeypatch.setattr(storage_module.shutil, 'copyfileobj', fail)
    with pytest.raises(OSError):
        store.put_if_absent('runs/new', b'complete')
    assert list(store.list('runs/')) == []


def test_memory_condition_preserves_fault_injection() -> None:
    """Route conditional memory writes through the injected failure boundary."""

    class FaultStore(MemoryStore):
        """Inject failures at the normal memory-store write boundary."""

        def put(self, key: str, data: bytes) -> None:
            """Fail before persisting any memory-store bytes."""
            raise StorageError()

    store = FaultStore()
    with pytest.raises(StorageError):
        store.put_if_absent('key', b'data')
    assert list(store.list('')) == []


def test_s3_cas_uses_observed_etag_and_never_writes_on_mismatch() -> None:
    """Bind each S3 replacement to the ETag read with its expected bytes."""
    client = boto3.client(
        's3',
        region_name='us-east-1',
        aws_access_key_id='mock',
        aws_secret_access_key='mock',
    )
    store = S3Store(Config(bucket='bucket'), client)
    with Stubber(client) as stub:
        get = {'Bucket': 'bucket', 'Key': 'registry/head'}
        stub.add_response(
            'get_object',
            {'Body': StreamingBody(io.BytesIO(b'old'), 3), 'ETag': '"etag-old"'},
            get,
        )
        stub.add_response('put_object', {}, {**get, 'Body': b'new', 'IfMatch': '"etag-old"'})
        assert store.compare_and_swap('registry/head', b'old', b'new') is True
        stub.add_response(
            'get_object',
            {'Body': StreamingBody(io.BytesIO(b'new'), 3), 'ETag': '"etag-new"'},
            get,
        )
        assert store.compare_and_swap('registry/head', b'old', b'wrong') is False
        stub.add_response(
            'get_object',
            {'Body': StreamingBody(io.BytesIO(b'new'), 3), 'ETag': '"etag-new"'},
            get,
        )
        stub.add_client_error(
            'put_object',
            'PreconditionFailed',
            http_status_code=412,
            expected_params={**get, 'Body': b'next', 'IfMatch': '"etag-new"'},
        )
        assert store.compare_and_swap('registry/head', b'new', b'next') is False
        stub.assert_no_pending_responses()


@pytest.mark.parametrize('committed', [True, False])
def test_s3_cas_ambiguous_put_checks_exact_readback(committed: bool) -> None:
    """Accept a lost PUT reply only when a read confirms the exact new bytes."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        value = b'old'
        writes = 0

        def get_object(self, **kwargs: object) -> dict[str, object]:
            """Expose current bytes so ambiguous writes can be verified by readback."""
            return {'Body': io.BytesIO(self.value), 'ETag': '"old"'}

        def put_object(self, **kwargs: object) -> dict[str, object]:
            """Optionally commit the bytes, then lose the PUT response."""
            assert kwargs['IfMatch'] == '"old"'
            self.writes += 1
            if committed:
                body = kwargs['Body']
                assert isinstance(body, bytes)
                self.value = body
            raise ReadTimeoutError(endpoint_url='https://mock.invalid')

    client = Client()
    store = S3Store(Config(bucket='bucket'), client)
    if committed:
        assert store.compare_and_swap('registry/head', b'old', b'new') is True
    else:
        with pytest.raises(StorageError):
            store.compare_and_swap('registry/head', b'old', b'new')
    assert client.writes == 1


def test_two_s3_instances_fence_concurrent_registry_updates() -> None:
    """Use provider preconditions to coordinate independent S3Store instances."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        value = b'initial'
        barrier = Barrier(2)
        lock = Lock()

        def get_object(self, **kwargs: object) -> dict[str, object]:
            """Synchronize both readers after capturing the same registry generation."""
            with self.lock:
                value = self.value
            self.barrier.wait(timeout=5)
            return {'Body': io.BytesIO(value), 'ETag': '"' + value.decode() + '"'}

        def put_object(self, **kwargs: object) -> dict[str, object]:
            """Reject a writer whose observed ETag no longer matches the current bytes."""
            with self.lock:
                if kwargs['IfMatch'] != '"' + self.value.decode() + '"':
                    raise ClientError(
                        {
                            'Error': {'Code': 'PreconditionFailed'},
                            'ResponseMetadata': {'HTTPStatusCode': 412},
                        },
                        'PutObject',
                    )
                body = kwargs['Body']
                assert isinstance(body, bytes)
                self.value = body
            return {}

    client = Client()
    stores = [S3Store(Config(bucket='bucket'), client) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(store.compare_and_swap, 'registry/head', b'initial', data)
            for store, data in zip(stores, (b'one', b'two'))
        ]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(results) == [False, True]
    assert client.value == (b'one' if results[0] else b'two')


def test_mock_pending_bytes_are_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide in-progress conditional writes from object enumeration."""
    store = MockFileStore(tmp_path / 'objects')
    original = storage_module.shutil.copyfileobj

    def copy(source: BinaryIO, target: BinaryIO, length: int) -> None:
        """Inspect enumeration before the complete staged object is promoted."""
        original(source, target, length)
        target.flush()
        assert list(store.list('')) == []

    monkeypatch.setattr(storage_module.shutil, 'copyfileobj', copy)
    assert store.put_if_absent('registry/head', b'data') is True
    assert list(store.list('')) == ['registry/head']
    assert not list(tmp_path.glob('.objects.conditional-*'))


def test_s3_cas_get_failure_cannot_be_mistaken_for_committed_put() -> None:
    """Reject a failed comparison read before attempting any registry write."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        reads = 0

        def get_object(self, **kwargs: object) -> dict[str, object]:
            """Fail the comparison read before any write can be attempted."""
            self.reads += 1
            raise ReadTimeoutError(endpoint_url='https://mock.invalid')

        def put_object(self, **kwargs: object) -> dict[str, object]:
            """Reject any unexpected write after the comparison read failed."""
            raise AssertionError('GET failure must not start a PUT')

    client = Client()
    with pytest.raises(StorageError):
        S3Store(Config(bucket='bucket'), client).compare_and_swap('registry/head', b'old', b'new')
    assert client.reads == 1
