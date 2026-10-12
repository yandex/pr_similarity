"""Configurable deployment and resource limits; no hosting provider defaults."""

from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

from viewer.errors import ViewerError
from viewer.util import parse_json, safe_path


@dataclass(frozen=True)
class Limits:
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

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value < 0 or (value == 0 and item.name != 'staging_free_bytes'):
                raise ViewerError('invalid_config', 'Limits must be positive integers.')


@dataclass(frozen=True)
class Config:
    runtime: Path = Path('runtime')
    admin_token_file: Path = Path('runtime/admin-token')
    host: str = '127.0.0.1'
    port: int = 8000
    bucket: str = ''
    endpoint: str | None = None
    region: str | None = None
    prefix: str = ''
    storage: str = 's3'
    tls_cert: str | None = None
    tls_key: str | None = None
    grant_ttl_seconds: int = 60
    public_origin: str | None = None
    session_ttl_seconds: int = 43200
    login_attempts: int = 10
    login_window_seconds: int = 300
    password_checks: int = 1
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self):
        try:
            object.__setattr__(self, 'runtime', Path(self.runtime).expanduser().resolve())
            object.__setattr__(self, 'admin_token_file', Path(self.admin_token_file).expanduser().resolve())
        except (TypeError,ValueError):
            raise ViewerError('invalid_config', 'Runtime and token paths must be valid local paths.') from None
        if not isinstance(self.host,str) or not self.host or any(c.isspace() for c in self.host):
            raise ViewerError('invalid_config', 'Invalid bind host.')
        if not isinstance(self.prefix,str) or not isinstance(self.bucket,str) or not isinstance(self.storage,str):
            raise ViewerError('invalid_config', 'Storage settings must be strings.')
        if self.prefix:
            safe_path(self.prefix.rstrip('/'))
        if self.storage not in ('s3', 'mock') or (self.storage == 's3' and not self.bucket):
            raise ViewerError('invalid_config', 'Select S3 with a bucket or explicit mock storage.')
        if self.endpoint:
            parts = urlsplit(self.endpoint)
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ViewerError('invalid_config', 'Invalid storage endpoint.')
        if type(self.port) is not int or not 0 < self.port < 65536 or type(self.grant_ttl_seconds) is not int or self.grant_ttl_seconds < 1:
            raise ViewerError('invalid_config', 'Invalid server port or grant TTL.')
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ViewerError('invalid_config', 'TLS needs a certificate and a key.')
        if self.public_origin is not None:
            if not isinstance(self.public_origin,str):raise ViewerError('invalid_config','Invalid public origin.')
            try:
                origin=urlsplit(self.public_origin)
                origin.port
            except (TypeError,ValueError):
                raise ViewerError('invalid_config','Invalid public origin.') from None
            if origin.scheme not in ('http','https') or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('','/'):
                raise ViewerError('invalid_config','Invalid public origin.')
            object.__setattr__(self,'public_origin',self.public_origin.rstrip('/'))
            if origin.scheme=='http' and origin.hostname not in ('localhost','127.0.0.1','::1'):
                raise ViewerError('invalid_config','Browser authentication requires HTTPS except on loopback.')
        for value in (self.session_ttl_seconds,self.login_attempts,self.login_window_seconds,self.password_checks):
            if type(value) is not int or value<1:raise ViewerError('invalid_config','Authentication limits must be positive.')

    @classmethod
    def load(cls, path: Path):
        value = parse_json(path.read_bytes())
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ViewerError('invalid_config', 'Unknown configuration field.')
        try:
            value['limits'] = Limits(**value.get('limits', {}))
        except TypeError:
            raise ViewerError('invalid_config', 'Invalid or unknown limit field.') from None
        return cls(**value)
