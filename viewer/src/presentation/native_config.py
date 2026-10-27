"""Safe native config display shared by independently installed report consumers."""

import copy
import math
import re
from urllib.parse import parse_qsl, urlsplit

_TOP_LEVEL = frozenset(
    {
        '$schema',
        'model',
        'small_model',
        'default_agent',
        'permission',
        'provider',
        'skills',
        'instructions',
        'mcp',
        'agent',
        'tools',
        'plugin',
        'command',
        'formatter',
        'lsp',
        'tui',
        'server',
        'keybinds',
        'theme',
        'share',
        'autoupdate',
        'disabled_providers',
        'enabled_providers',
        'compaction',
        'watcher',
        'snapshot',
    }
)
_SECRET_KEYS = frozenset(
    {
        'apikey',
        'api_key',
        'api-key',
        'x-api-key',
        'password',
        'token',
        'access_token',
        'refresh_token',
        'authorization',
        'secret',
    }
)
_ENV = re.compile(r'\{env:[A-Za-z_][A-Za-z_0-9]*\}')
_SANDBOX_ROOTS = (
    '/repo',
    '/sandbox-home',
    '/benchmark-runtime',
    '/benchmark-bin',
    '/usr',
    '/bin',
    '/sbin',
    '/etc',
    '/lib',
    '/lib64',
    '/tmp',
)


def native_projection(native: object) -> dict:
    """Preserve native structure and order; refuse unsupported or private values."""
    if not isinstance(native, dict) or set(native) - _TOP_LEVEL:
        raise ValueError('unsupported_native_fields')

    def inspect(value: object, depth: int = 0) -> None:
        """Reject unsupported or private values anywhere in native config."""
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
