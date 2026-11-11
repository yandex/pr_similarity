"""Storage diagnostics preserve provider classifications without recording secrets."""

import hashlib
import logging

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError
from viewer.config import Config
from viewer.errors import StorageError, ViewerError
from viewer.persistence.storage import S3Body, S3Store

RUN = '11111111-1111-4111-8111-111111111111'
KEY = f'runs/private-owner/{RUN}/archive.tar.gz'
SECRET = 'private-token?Authorization=Bearer%20secret\nINJECTED'
LOGGER = 'viewer.persistence.storage'


@pytest.mark.parametrize('code,status', [('SlowDown', 503), ('AccessDenied', 403), ('NoSuchKey', 404)])
def test_client_error_records_only_allowlisted_metadata(
    caplog: pytest.LogCaptureFixture, code: str, status: int
) -> None:
    """Keep retry and provider classifications while discarding payloads and headers."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        def get_object(self, **kwargs: object) -> dict[str, object]:
            """Raise a provider error containing private data in its message and headers."""
            raise ClientError(
                {
                    'Error': {'Code': code, 'Message': SECRET},
                    'ResponseMetadata': {
                        'HTTPStatusCode': status,
                        'RequestId': 'request-123',
                        'RetryAttempts': 2,
                        'HTTPHeaders': {'authorization': SECRET},
                    },
                },
                'GetObject',
            )

    store = S3Store(Config(bucket='private-bucket', prefix='private-prefix'), Client())
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        pytest.raises(ViewerError) as caught,
    ):
        store.open(KEY)
    assert caught.value.status == (404 if status == 404 else 503)
    record = next(record for record in caplog.records if record.name == LOGGER)
    assert record.__dict__['s3_method'] == 'get_object'
    assert record.__dict__['s3_key_sha256'] == hashlib.sha256(('private-prefix/' + KEY).encode()).hexdigest()
    assert record.__dict__['s3_run_id'] == RUN
    assert record.__dict__['s3_http_status'] == status
    assert record.__dict__['s3_error_code'] == code
    assert record.__dict__['s3_request_id'] == 'request-123'
    assert record.__dict__['s3_retry_attempts'] == 2
    duration = record.__dict__['s3_duration_seconds']
    assert isinstance(duration, float) and duration >= 0
    assert record.__dict__['s3_exception_type'] == 'ClientError'
    assert record.exc_info is None
    serialized = repr(record.__dict__)
    assert all(value not in serialized for value in (SECRET, 'private-owner', 'private-bucket', 'private-prefix', KEY))


@pytest.mark.parametrize(
    'error',
    [
        ReadTimeoutError(endpoint_url='https://private.example/?' + SECRET),
        OSError(SECRET),
    ],
)
def test_stream_errors_keep_only_safe_identity(caplog: pytest.LogCaptureFixture, error: Exception) -> None:
    """Stream exceptions do not expose endpoints or OS exception messages."""

    class Body:
        """Provide a readable body whose I/O failure contains private diagnostics."""

        def read(self, size: int = -1) -> bytes:
            """Raise the configured stream failure instead of returning bytes."""
            raise error

        def close(self) -> None:
            """Release the synthetic body without owning external resources."""
            pass

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(StorageError):
        S3Body(Body(), key=KEY).read(1)
    record = next(record for record in caplog.records if record.name == LOGGER)
    assert record.__dict__['s3_method'] == 'get_object.read'
    assert record.__dict__['s3_run_id'] == RUN
    assert record.__dict__['s3_exception_type'] == type(error).__name__
    assert SECRET not in repr(record.__dict__)
    assert 'private.example' not in repr(record.__dict__)


def test_success_metadata_is_debug_only_and_visible_in_default_formatter(caplog: pytest.LogCaptureFixture) -> None:
    """Successful calls expose timing and SDK retries without object names or response data."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        def put_object(self, **kwargs: object) -> dict[str, object]:
            """Return SDK retry metadata alongside an untrusted ETag."""
            return {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                    'RetryAttempts': 1,
                    'RequestId': 'request-success',
                },
                'ETag': SECRET,
            }

    store = S3Store(Config(bucket='private-bucket'), Client())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        store.put(KEY, SECRET.encode())
    assert not [record for record in caplog.records if record.name == LOGGER]
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        store.put(KEY, SECRET.encode())
    record = next(record for record in caplog.records if record.name == LOGGER)
    assert record.levelno == logging.DEBUG
    assert record.__dict__['s3_http_status'] == 200
    assert record.__dict__['s3_retry_attempts'] == 1
    assert record.__dict__['s3_exception_type'] is None
    assert record.__dict__['s3_error_code'] is None
    assert 'request-success' in record.getMessage()
    assert SECRET not in repr(record.__dict__)


def test_untrusted_provider_metadata_is_bounded_and_filtered(caplog: pytest.LogCaptureFixture) -> None:
    """Provider identifiers containing message or URL syntax cannot inject log data."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        def delete_object(self, **kwargs: object) -> dict[str, object]:
            """Raise a provider error containing untrusted metadata."""
            raise ClientError(
                {
                    'Error': {'Code': SECRET, 'Message': SECRET},
                    'ResponseMetadata': {
                        'RequestId': SECRET,
                        'HTTPStatusCode': True,
                        'RetryAttempts': SECRET,
                    },
                },
                'DeleteObject',
            )

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(StorageError):
        S3Store(Config(bucket='bucket'), Client()).delete(KEY)
    record = next(record for record in caplog.records if record.name == LOGGER)
    assert (
        record.__dict__['s3_request_id']
        is record.__dict__['s3_http_status']
        is record.__dict__['s3_retry_attempts']
        is record.__dict__['s3_error_code']
        is None
    )
    assert SECRET not in repr(record.__dict__)


def test_sdk_connection_exception_never_logs_endpoint(caplog: pytest.LogCaptureFixture) -> None:
    """Transport failures preserve their type without rendering SDK exception details."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
            """Fail listing with a private endpoint in the SDK exception."""
            raise ReadTimeoutError(endpoint_url='https://private.example/?' + SECRET)

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(StorageError):
        list(S3Store(Config(bucket='bucket'), Client()).list('runs/'))
    record = next(record for record in caplog.records if record.name == LOGGER)
    assert record.__dict__['s3_method'] == 'list_objects_v2'
    assert record.__dict__['s3_exception_type'] == 'ReadTimeoutError'
    assert record.__dict__['s3_run_id'] is None
    assert 'private.example' not in repr(record.__dict__)
    assert SECRET not in repr(record.__dict__)


def test_open_stream_retains_safe_response_metadata(caplog: pytest.LogCaptureFixture) -> None:
    """Errors after a successful GET retain its request ID, without copying HTTP headers."""

    class Body:
        """Provide a readable body whose I/O failure contains private diagnostics."""

        def read(self, size: int = -1) -> bytes:
            """Raise the configured stream failure instead of returning bytes."""
            raise OSError(SECRET)

        def close(self) -> None:
            """Release the synthetic body without owning external resources."""
            pass

    class Client:
        """Emulate the S3 operation under test without network access."""

        def get_object(self, **kwargs: object) -> dict[str, object]:
            """Return a failing stream with safe request metadata and private headers."""
            return {
                'Body': Body(),
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                    'RequestId': 'stream-request',
                    'HTTPHeaders': {'authorization': SECRET},
                },
            }

    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(StorageError):
        S3Store(Config(bucket='bucket'), Client()).get(f'bundle-data/v1/owner/{RUN}/1/archive.tar.gz')
    record = next(record for record in caplog.records if record.name == LOGGER)
    assert record.__dict__['s3_request_id'] == 'stream-request'
    assert record.__dict__['s3_http_status'] == 200
    assert record.__dict__['s3_run_id'] == RUN
    assert SECRET not in repr(record.__dict__)


@pytest.mark.parametrize(
    'code,status,retryable',
    [
        ('AccessDenied', 403, False),
        ('InvalidAccessKeyId', 403, False),
        ('InvalidRequest', 400, False),
        ('SlowDown', 503, True),
        ('Throttling', 400, True),
        ('InternalError', 500, True),
    ],
)
def test_storage_error_retryability_preserves_public_response(code: str, status: int, retryable: bool) -> None:
    """Classify retryability internally while preserving the public storage error."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        def put_object(self, **kwargs: object) -> dict[str, object]:
            """Raise the selected provider failure for retry classification."""
            raise ClientError(
                {
                    'Error': {'Code': code},
                    'ResponseMetadata': {'HTTPStatusCode': status},
                },
                'PutObject',
            )

    with pytest.raises(StorageError) as caught:
        S3Store(Config(bucket='bucket'), Client()).put('object', b'data')
    assert caught.value.retryable is retryable
    assert caught.value.code == 'storage_unavailable'
    assert caught.value.status == 503
    assert caught.value.message == 'Storage is unavailable; retry the operation.'


def test_missing_credentials_are_not_retried() -> None:
    """Block missing credentials instead of scheduling an automatic retry."""

    class Client:
        """Emulate the S3 operation under test without network access."""

        def put_object(self, **kwargs: object) -> dict[str, object]:
            """Reject the operation before a provider request can be signed."""
            raise NoCredentialsError()

    with pytest.raises(StorageError) as caught:
        S3Store(Config(bucket='bucket'), Client()).put_if_absent('object', b'data')
    assert caught.value.retryable is False
    assert StorageError().retryable is True
