"""Configurable deployment and resource limits; no hosting provider defaults."""

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from viewer.errors import ViewerError
from viewer.util import parse_json, safe_path


@dataclass(frozen=True)
class Limits:
    """Bound archive, display and request resource use."""

    archive_bytes: int = 1 << 30
    unpacked_bytes: int = 8 << 30
    files: int = 100_000
    file_bytes: int = 256 << 20
    json_bytes: int = 64 << 20
    tar_header_bytes: int = 64 << 10
    expansion_ratio: int = 1000
    path_bytes: int = 4096
    api_body_bytes: int = 1 << 20
    name_chars: int = 256
    basket_name_chars: int = 256
    basket_description_chars: int = 8192
    runtime_chars: int = 256
    task_title_chars: int = 1024
    tag_chars: int = 256
    tags: int = 128
    diff_bytes: int = 8 << 20
    diff_lines: int = 20_000
    config_display_bytes: int = 64 << 10
    transfer_seconds: int = 900
    staging_free_bytes: int = 10 << 30

    def __post_init__(self) -> None:
        """Validate configured limits before the service starts."""
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value < 0 or (value == 0 and item.name != 'staging_free_bytes'):
                raise ViewerError('invalid_config', 'Limits must be positive integers.')


@dataclass(frozen=True)
class Config:
    """Configure viewer storage, authentication and serving limits."""

    runtime: Path = Path('runtime')
    admin_token_file: Path = Path('runtime/admin-token')
    host: str = '127.0.0.1'
    port: int = 8000
    bucket: str = ''
    endpoint: str | None = None
    region: str | None = None
    prefix: str = ''
    storage: str = 's3'
    publication_storage: str = 'legacy'
    cache_idle_ttl_seconds: int = 172800
    cache_cleanup_interval_seconds: int = 300
    catalogue_cache_bytes: int = 8 << 20
    catalogue_cache_entries: int = 128
    report_cache_bytes: int = 64 << 20
    report_cache_entries: int = 16
    cache_max_bytes: int = 20 << 30
    pending_backup_max_bytes: int = 16 << 30
    disk_reserve_bytes: int = 1 << 30
    backup_workers: int = 2
    restore_workers: int = 2
    restore_queue_limit: int = 4
    publish_workers: int = 4
    backup_retry_seconds: int = 30
    checkpoint_interval_seconds: int = 86400
    checkpoint_retry_seconds: int = 300
    cold_read_timeout_seconds: int = 900
    version_retention_seconds: int = 90 * 86400
    admin_overwrite_token_ttl_seconds: int = 60
    tls_cert: str | None = None
    tls_key: str | None = None
    grant_ttl_seconds: int = 60
    public_origin: str | None = None
    session_ttl_seconds: int = 43200
    login_attempts: int = 10
    login_window_seconds: int = 300
    password_checks: int = 1
    recovery_retry_seconds: int = 5
    recovery_attempts: int = 3
    shutdown_seconds: int = 15
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        """Validate configured limits before the service starts."""
        try:
            object.__setattr__(self, 'runtime', Path(self.runtime).expanduser().resolve())
            object.__setattr__(self, 'admin_token_file', Path(self.admin_token_file).expanduser().resolve())
        except (TypeError, ValueError):
            raise ViewerError('invalid_config', 'Runtime and token paths must be valid local paths.') from None
        if not isinstance(self.host, str) or not self.host or any(c.isspace() for c in self.host):
            raise ViewerError('invalid_config', 'Invalid bind host.')
        if not isinstance(self.prefix, str) or not isinstance(self.bucket, str) or not isinstance(self.storage, str):
            raise ViewerError('invalid_config', 'Storage settings must be strings.')
        if self.prefix:
            safe_path(self.prefix.rstrip('/'))
        if self.storage not in ('s3', 'mock') or (self.storage == 's3' and not self.bucket):
            raise ViewerError('invalid_config', 'Select S3 with a bucket or explicit mock storage.')
        if self.publication_storage not in ('legacy', 'local'):
            raise ViewerError('invalid_config', 'Select legacy or local publication storage.')
        for value in (
            self.cache_idle_ttl_seconds,
            self.cache_cleanup_interval_seconds,
            self.cache_max_bytes,
            self.report_cache_bytes,
            self.report_cache_entries,
            self.catalogue_cache_bytes,
            self.catalogue_cache_entries,
            self.pending_backup_max_bytes,
            self.backup_retry_seconds,
            self.cold_read_timeout_seconds,
            self.version_retention_seconds,
            self.admin_overwrite_token_ttl_seconds,
            self.checkpoint_interval_seconds,
            self.checkpoint_retry_seconds,
        ):
            if type(value) is not int or value < 1:
                raise ViewerError('invalid_config', 'Local storage limits must be positive integers.')
        if type(self.restore_queue_limit) is not int or self.restore_queue_limit < 0:
            raise ViewerError('invalid_config', 'Restore queue limit must be a non-negative integer.')
        if type(self.disk_reserve_bytes) is not int or self.disk_reserve_bytes < 0:
            raise ViewerError('invalid_config', 'Disk reserve must be a non-negative integer.')
        for value in (self.backup_workers, self.restore_workers, self.publish_workers):
            if type(value) is not int or not 1 <= value <= 32:
                raise ViewerError('invalid_config', 'Worker limits must be integers from 1 to 32.')
        if self.endpoint:
            parts = urlsplit(self.endpoint)
            if (
                parts.scheme not in ('http', 'https')
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ViewerError('invalid_config', 'Invalid storage endpoint.')
        if (
            type(self.port) is not int
            or not 0 < self.port < 65536
            or type(self.grant_ttl_seconds) is not int
            or self.grant_ttl_seconds < 1
        ):
            raise ViewerError('invalid_config', 'Invalid server port or grant TTL.')
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ViewerError('invalid_config', 'TLS needs a certificate and a key.')
        if self.public_origin is not None:
            if not isinstance(self.public_origin, str):
                raise ViewerError('invalid_config', 'Invalid public origin.')
            try:
                origin = urlsplit(self.public_origin)
                origin.port
            except (TypeError, ValueError):
                raise ViewerError('invalid_config', 'Invalid public origin.') from None
            if (
                origin.scheme not in ('http', 'https')
                or not origin.hostname
                or origin.username
                or origin.password
                or origin.query
                or origin.fragment
                or origin.path not in ('', '/')
            ):
                raise ViewerError('invalid_config', 'Invalid public origin.')
            object.__setattr__(self, 'public_origin', self.public_origin.rstrip('/'))
            if origin.scheme == 'http' and origin.hostname not in ('localhost', '127.0.0.1', '::1'):
                raise ViewerError('invalid_config', 'Browser authentication requires HTTPS except on loopback.')
        for value in (
            self.session_ttl_seconds,
            self.login_attempts,
            self.login_window_seconds,
            self.password_checks,
            self.recovery_retry_seconds,
            self.recovery_attempts,
            self.shutdown_seconds,
        ):
            if type(value) is not int or value < 1:
                raise ViewerError('invalid_config', 'Authentication limits must be positive.')

    @classmethod
    def load(cls, path: Path) -> 'Config':
        """Load and validate the viewer configuration from its JSON file."""
        value = parse_json(path.read_bytes())
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ViewerError('invalid_config', 'Unknown configuration field.')
        try:
            limits = value.get('limits', {})
            if not isinstance(limits, dict):
                raise TypeError('Limits must be an object.')
            decoded_limits = _decode_config_dataclass(Limits, limits)
        except TypeError:
            raise ViewerError('invalid_config', 'Invalid or unknown limit field.') from None
        arguments: dict[str, object] = dict(value)
        arguments['limits'] = decoded_limits
        return _decode_config_dataclass(cls, arguments)


def _decode_config_dataclass[T: Config | Limits](model: type[T], values: Mapping[str, object]) -> T:
    """Decode deployment JSON through the dataclasses' existing validation boundary.

    The input has not yet earned the field annotations: __post_init__ owns the
    accepted values, normalization and diagnostic order. Constructing fields
    here preserves that contract, including legacy falsy optional values,
    without claiming that an unchecked JSON mapping is typed constructor data.
    This adapter is restricted to the two deployment configuration dataclasses.
    """
    declared = fields(model)
    if set(values) - {item.name for item in declared}:
        raise TypeError('Unknown configuration field.')
    instance = object.__new__(model)
    for item in declared:
        if item.name in values:
            value = values[item.name]
        elif item.default is not MISSING:
            value = item.default
        elif item.default_factory is not MISSING:
            value = item.default_factory()
        else:
            raise TypeError('Missing configuration field.')
        object.__setattr__(instance, item.name, value)
    instance.__post_init__()
    return instance
