"""Provider-neutral repository identity and frozen Git retrieval descriptors."""

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class SourceModel(BaseModel):
    """Reject unknown source fields and implicit input coercion."""

    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


def https_url(value: str) -> str:
    """Require a credential-free HTTPS endpoint with no request components."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError('expected HTTPS URL without credentials, query or fragment')
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('invalid HTTPS port') from exc
    return value


def canonical_server(value: str) -> str:
    """Normalize host case and trailing slash while preserving deployment-prefix case."""
    https_url(value)
    parts = urlsplit(value)
    host = parts.hostname
    assert host is not None
    authority = host.lower()
    if ':' in authority:
        authority = '[' + authority + ']'
    if parts.port is not None and parts.port != 443:
        authority += ':' + str(parts.port)
    path = parts.path.rstrip('/')
    if any(part in ('.', '..') for part in path.split('/')):
        raise ValueError('server prefix must not contain dot segments')
    return urlunsplit(('https', authority, path, '', ''))


def change_id(provider: str, server_url: str, project_id: str, kind: str, number: int) -> str:
    """Bind an opaque change identity to its immutable provider/server/project tuple."""
    encoded = json.dumps(
        [provider, server_url, project_id, kind, number], ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')
    return 'change:' + hashlib.sha256(encoded).hexdigest()


def issue_id(provider: str, server_url: str, project_id: str, number: int) -> str:
    """Use the same immutable identity recipe for an issue in any source project."""
    return 'issue:' + change_id(provider, server_url, project_id, 'issue', number).removeprefix('change:')


def source_hash(source: 'SourceDescriptor') -> str:
    """Hash the exact declarative retrieval inputs independently of credentials."""
    content = json.dumps(source.model_dump(mode='json'), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


class Repository(SourceModel):
    """Describe one stable hosting project without privacy or transport credentials."""

    provider: StrictStr = Field(pattern='^(github|gitlab)$')
    server_url: StrictStr
    project_id: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=3)
    web_url: StrictStr
    default_branch: StrictStr | None

    @field_validator('server_url')
    @classmethod
    def check_server(cls, value: str) -> str:
        """Require the stored instance address to be canonical."""
        if canonical_server(value) != value:
            raise ValueError('server_url must be canonical')
        return value

    @field_validator('web_url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Keep display coordinates free of embedded authorization data."""
        return https_url(value)

    @field_validator('path')
    @classmethod
    def check_path(cls, value: str) -> str:
        """Preserve subgroup/case identity while rejecting empty and dot segments."""
        if len(value.split('/')) < 2 or any(part in ('', '.', '..') for part in value.split('/')):
            raise ValueError('expected normalized namespace/project path')
        if '\\' in value or '\x00' in value:
            raise ValueError('invalid project path')
        return value

    @field_validator('default_branch')
    @classmethod
    def check_branch(cls, value: str | None) -> str | None:
        """Allow unresolved metadata only while the stage is unfinished."""
        if value == '':
            raise ValueError('default branch must not be empty')
        return value


class SourceDescriptor(SourceModel):
    """Declare how a Git adapter can fetch the frozen task objects."""

    vcs: StrictStr = Field(pattern='^git$')
    fetch_url: StrictStr
    fetch_refs: list[StrictStr]

    @field_validator('fetch_url')
    @classmethod
    def check_url(cls, value: str) -> str:
        """Require the explicit Git endpoint, never an auth-bearing URL."""
        return https_url(value)

    @field_validator('fetch_refs')
    @classmethod
    def check_refs(cls, values: list[str]) -> list[str]:
        """Accept complete Git refs as object-delivery hints, never refspec commands."""
        if len(values) != len(set(values)):
            raise ValueError('duplicate fetch ref')
        for value in values:
            if (
                not value.startswith('refs/')
                or value.endswith(('/', '.'))
                or '..' in value
                or '@{' in value
                or '//' in value
                or re.search(r'[\x00-\x20\x7f:~^?*\[\\]', value)
                or any(part.startswith('.') or part.endswith('.lock') for part in value.split('/'))
            ):
                raise ValueError('invalid complete Git fetch ref')
        return values


class RetrievalReceipt(SourceModel):
    """Record fresh-cache availability of the exact frozen commit/tree pair."""

    checked_at: StrictStr
    source_sha256: StrictStr = Field(pattern='^[0-9a-f]{64}$')
    before: StrictStr = Field(pattern='^[0-9a-f]{40}$')
    after: StrictStr = Field(pattern='^[0-9a-f]{40}$')
    before_tree: StrictStr = Field(pattern='^[0-9a-f]{40}$')
    after_tree: StrictStr = Field(pattern='^[0-9a-f]{40}$')

    @field_validator('checked_at')
    @classmethod
    def check_timestamp(cls, value: str) -> str:
        """Require a valid UTC instant for the recorded availability observation."""
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', value) is None:
            raise ValueError('retrieval timestamp must be UTC RFC3339')
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value
