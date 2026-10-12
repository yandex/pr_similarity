"""One safe native OpenCode document, separately from harness metadata."""

import copy
import math
import re
from urllib.parse import parse_qsl, urlsplit

from viewer.contracts import require
from viewer.errors import ViewerError
from viewer.util import digest, json_bytes, parse_json

_TOP_LEVEL = frozenset({
    '$schema', 'model', 'small_model', 'default_agent', 'permission', 'provider', 'skills',
    'instructions', 'mcp', 'agent', 'tools', 'plugin', 'command', 'formatter',
    'lsp', 'tui', 'server', 'keybinds', 'theme', 'share', 'autoupdate',
    'disabled_providers', 'enabled_providers', 'compaction', 'watcher', 'snapshot',
})
_SECRET_KEYS = frozenset({'apikey', 'api_key', 'api-key', 'x-api-key', 'password',
                         'token', 'access_token', 'refresh_token', 'authorization', 'secret'})
_ENV = re.compile(r'\{env:[A-Za-z_][A-Za-z_0-9]*\}')
_SANDBOX_ROOTS = ('/repo', '/sandbox-home', '/benchmark-runtime', '/benchmark-bin',
                  '/usr', '/bin', '/sbin', '/etc', '/lib', '/lib64', '/tmp')


def native_projection(native: dict) -> dict:
    """Preserve native structure and order; refuse unsupported or private values."""
    if not isinstance(native, dict) or set(native) - _TOP_LEVEL:
        raise ValueError('unsupported_native_fields')

    def inspect(value: object, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError('native_depth_limit')
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError('unsupported_native_value')
                sensitive = key.lower() in _SECRET_KEYS or key.lower().endswith(
                    ('_api_key', '_token', '_password', '_secret', '-api-key', '-token', '-password', '-secret')
                )
                if sensitive and child not in (None, ''):
                    if not isinstance(child, str) or _ENV.fullmatch(child) is None:
                        raise ValueError('private_native_value')
                inspect(key, depth + 1)
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
        elif isinstance(value, str):
            if len(value) > 16384 or any(ord(c) < 32 and c not in '\n\t' for c in value):
                raise ValueError('unsupported_native_value')
            path = value.removeprefix('file://')
            if path.startswith(('~/', '\\')) or re.match(r'^[A-Za-z]:[\\/]', path):
                raise ValueError('private_native_path')
            if path.startswith('/') and not any(path == root or path.startswith(root + '/') for root in _SANDBOX_ROOTS):
                raise ValueError('private_native_path')
            if value.startswith(('http://', 'https://')):
                parsed = urlsplit(value)
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError('private_native_value')
                if any(key.lower() in _SECRET_KEYS for key, _ in parse_qsl(parsed.query)):
                    raise ValueError('private_native_value')
        elif type(value) not in (type(None), bool, int, float) or (type(value) is float and not math.isfinite(value)):
            raise ValueError('unsupported_native_value')

    inspect(native)
    return copy.deepcopy(native)


def projection(identity, limits, inference=None):
    """Project harness controls for the run page, independently of native config."""
    def integer(value, minimum=0):
        if value is None:
            return None
        if type(value) is not int or not minimum <= value <= 2**31:
            raise ValueError('unsupported_metadata')
        return value
    runtime = identity.get('runtime')
    if runtime is not None and (not isinstance(runtime, str) or not runtime or len(runtime) > limits.runtime_chars):
        raise ValueError('unsupported_metadata')
    setup, retries = identity.get('setup'), identity.get('retries')
    if setup is not None and not isinstance(setup, dict):
        raise ValueError('unsupported_metadata')
    if retries is not None and not isinstance(retries, dict):
        raise ValueError('unsupported_metadata')
    excludes = identity.get('diff_excludes')
    categories = retries.get('categories') if retries is not None else None
    for values in (excludes, categories):
        if values is not None and (not isinstance(values, list) or len(values) > 1024
                                  or any(not isinstance(v, str) or not v or len(v) > 4096 for v in values)):
            raise ValueError('unsupported_metadata')
    return {'runtime': runtime, 'timeout_seconds': integer(identity.get('timeout_seconds'), 1),
            'setup_timeout_seconds': integer(setup.get('timeout_seconds'), 1) if setup else None,
            'retries': {'max_retries': integer(retries.get('max_retries')), 'categories': categories} if retries else None,
            'diff_excludes': excludes}


def public_config(service, rid, expected=None, publication=None):
    """Read one digest-bound input while publication mutations hold the same lock."""
    with service.lock:
        report_sha = service.check_report(rid, expected, publication)
        report = service.full_report(rid)
        row = service._require_run(rid)
        source = report['agent_input']['config']
        result = {'projection_version': 2, 'run_id': rid, 'report_sha256': report_sha,
                  'publication_sha256': row['marker']['archive_sha256'],
                  'identity_sha256': report['config']['identity_sha256'], 'source': source,
                  'supported': False, 'config': None, 'reason': None}
        require(source['path'] == 'agent/opencode.json', 'invalid_agent_input')
        try:
            raw = service.store.get(row['prefix'] + 'data/' + source['path'], service.config.limits.json_bytes)
        except ViewerError as error:
            if error.status in (404, 413):
                return {**result, 'reason': 'snapshot_unavailable'}
            raise
        require(len(raw) == source['size_bytes'] and digest(raw) == source['sha256'], 'stored_digest_mismatch')
        try:
            value = native_projection(parse_json(raw))
        except ValueError as error:
            return {**result, 'reason': str(error)}
        if len(json_bytes(value)) > service.config.limits.config_display_bytes:
            return {**result, 'reason': 'display_limit'}
        return {**result, 'supported': True, 'config': value}
