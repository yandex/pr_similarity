"""Benchmark-owned validation of frozen Git sources and opaque task identity."""

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from benchmark.errors import BenchmarkError


def https_url(value: str) -> str:
    """Require a credential-free transport/metadata URL without request components."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == 'https'
            and bool(parsed.hostname)
            and parsed.port != 0
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise BenchmarkError('invalid_source', 'Expected an HTTPS source URL without credentials, query or fragment.')
    return value


def canonical_server(value: str) -> str:
    """Canonicalize host casing while preserving deployment path and namespace casing."""
    parsed = urlsplit(https_url(value))
    hostname = parsed.hostname
    assert hostname is not None
    host = '[' + hostname.lower() + ']' if ':' in hostname else hostname.lower()
    if parsed.port is not None and parsed.port != 443:
        host += ':' + str(parsed.port)
    if any(part in ('.', '..') for part in parsed.path.split('/')):
        raise BenchmarkError('invalid_source', 'Source server prefix must not contain dot segments.')
    return urlunsplit(('https', host, parsed.path.rstrip('/'), '', ''))


def fetch_ref(value: str) -> str:
    """Validate full Git refs before they can become arguments to fetch."""
    if (
        not value.startswith('refs/')
        or value.endswith('/')
        or '..' in value
        or '@{' in value
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
        or re.search(r'[:~^?*\[\\]', value) is not None
        or any(
            part in ('', '.', '..') or part.startswith('.') or part.endswith(('.', '.lock'))
            for part in value.split('/')
        )
    ):
        raise BenchmarkError('invalid_source', 'Basket contains an unsafe or malformed Git fetch ref.')
    return value


def task_identity(repository: dict, kind: str, number: int) -> str:
    """Compute a provider/server/project identity without parsing a display label."""
    encoded = json.dumps(
        [repository['provider'], repository['server_url'], repository['project_id'], kind, number],
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return 'change:' + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SourceDescriptor:
    """Deliver exact objects using Git only; provider metadata never selects transport logic."""

    fetch_url: str
    fetch_refs: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: dict) -> 'SourceDescriptor':
        """Validate the independently versioned basket descriptor."""
        if value.get('vcs') != 'git' or set(value) != {'vcs', 'fetch_url', 'fetch_refs'}:
            raise BenchmarkError('invalid_source', 'Basket requires a complete supported Git source descriptor.')
        references = value['fetch_refs']
        if (
            not isinstance(references, list)
            or any(not isinstance(item, str) for item in references)
            or len(set(references)) != len(references)
        ):
            raise BenchmarkError('invalid_source', 'Basket Git ref hints must be a unique ordered list.')
        return cls(https_url(value['fetch_url']), tuple(fetch_ref(reference) for reference in references))

    def as_mapping(self) -> dict:
        """Keep the declared descriptor unchanged in saved report provenance."""
        return {'vcs': 'git', 'fetch_url': self.fetch_url, 'fetch_refs': list(self.fetch_refs)}
