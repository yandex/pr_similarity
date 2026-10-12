"""Independent consumer proof for the run-owned portable native input."""

import re
from collections.abc import Callable

from viewer.errors import ViewerError
from viewer.util import digest, parse_json, safe_path

_SECRET_KEYS = frozenset({'apikey', 'api_key', 'api-key', 'x-api-key', 'password',
                         'token', 'access_token', 'refresh_token', 'authorization', 'secret'})


def _native_secrets(value: object) -> None:
    """Reject literal credential fields even if semantic identity omits them."""
    if isinstance(value, dict):
        for key, child in value.items():
            if (key.lower() in _SECRET_KEYS and child not in (None, '')
                    and (not isinstance(child, str) or re.fullmatch(r'\{env:[A-Za-z_][A-Za-z_0-9]*\}', child) is None)):
                raise ViewerError('invalid_agent_input', 'Native credentials must be environment references.')
            _native_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _native_secrets(child)


def _identity_native(value: object) -> object:
    """Exclude credential fields while retaining model, endpoint and native order."""
    if isinstance(value, dict):
        return {
            key: _ordered_rules(child) if key == 'permission' else _identity_native(child)
            for key, child in value.items()
            if key.lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_identity_native(child) for child in value]
    return value


def _ordered_rules(value: object) -> object:
    """Represent permission mappings as ordered pairs before canonical key sorting."""
    if isinstance(value, dict):
        return {'ordered_entries': [[key, _ordered_rules(child)] for key, child in value.items()]}
    if isinstance(value, list):
        return [_ordered_rules(child) for child in value]
    return value


def _groups(native: dict) -> list[tuple[dict, str, bool]]:
    """Keep the same native resource group ordering as the config loader."""
    return [(native, 'instructions', False), (native, 'plugin', True),
            (native.get('skills', {}), 'paths', False)]


def _verify_file(receipt: dict, inventory: dict, read_file: Callable[[str], bytes]) -> bytes:
    """Bind the shared reference to verified archive inventory and exact bytes."""
    if set(receipt) != {'path', 'size_bytes', 'sha256'}:
        raise ViewerError('invalid_agent_input', 'Invalid shared input receipt.')
    path = safe_path(receipt['path'])
    if inventory.get(path) != receipt:
        raise ViewerError('invalid_agent_input', 'Shared receipt differs from archive inventory.')
    content = read_file(path)
    if (type(receipt['size_bytes']) is not int or len(content) != receipt['size_bytes']
            or digest(content) != receipt['sha256']):
        raise ViewerError('invalid_agent_input', 'Shared input bytes differ from their receipt.')
    return content


def validate_agent_input(selected: dict, identity: dict, inventory: dict, read_file: Callable[[str], bytes]) -> dict:
    """Prove portable native input has the frozen semantic config and resources."""
    if (set(selected) != {'config', 'resources'}
            or selected['config'].get('path') != 'agent/opencode.json'
            or not isinstance(selected['resources'], list)):
        raise ViewerError('invalid_agent_input', 'Run has no valid frozen native input.')
    native = parse_json(_verify_file(selected['config'], inventory, read_file))
    if not isinstance(native, dict):
        raise ViewerError('invalid_agent_input', 'Native input must be a JSON object.')
    _native_secrets(native)
    if (not isinstance(identity.get('instructions'), dict)
            or not isinstance(identity.get('resources'), list)
            or 'setup' not in identity or not isinstance(identity.get('native_config'), dict)):
        raise ViewerError('invalid_agent_input', 'Shared input requires a complete semantic snapshot.')
    files = {}
    for receipt in selected['resources']:
        path = receipt['path']
        if (path in files or not (path.startswith('agent/resources/')
                or path in {'agent/task_instructions.txt', 'agent/setup.sh'})):
            raise ViewerError('invalid_agent_input', 'Shared resource has an unexpected owner path.')
        files[path] = _verify_file(receipt, inventory, read_file)
    used = {'agent/task_instructions.txt'}
    if digest(files.get('agent/task_instructions.txt', b'')) != identity['instructions']['sha256']:
        raise ViewerError('resource_changed', 'Frozen task instructions differ from identity.')
    if identity['setup'] is not None:
        used.add('agent/setup.sh')
        if digest(files.get('agent/setup.sh', b'')) != identity['setup']['sha256']:
            raise ViewerError('resource_changed', 'Frozen setup differs from identity.')
    semantic = _identity_native(native)
    assert isinstance(semantic, dict)
    expected = {(r['field'], r['index']): r for r in identity['resources']}
    found = set()
    for group_index, (mapping, key, plugin) in enumerate(_groups(semantic)):
        values = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(values, list):
            continue
        normalized, groups = [], {}
        for value in values:
            raw = value.removeprefix('file://') if plugin and isinstance(value, str) else value
            match = re.fullmatch(r'resources/(\d+)-(\d+)-(\d+)/[^/]+', raw) if isinstance(raw, str) else None
            if match is None:
                normalized.append(value)
                continue
            group, index, _ = map(int, match.groups())
            if group != group_index:
                raise ViewerError('invalid_agent_input', 'Native resource refers to another field.')
            owner = (key, index)
            if owner not in expected:
                raise ViewerError('invalid_agent_input', 'Native resource has no semantic owner.')
            if owner not in groups:
                groups[owner] = []
                normalized.append({'resource': {'field': key, 'index': index, 'files': groups[owner]}})
            root = 'agent/' + raw
            matches = sorted(p for p in files if p == root or p.startswith(root + '/'))
            if not matches:
                raise ViewerError('resource_missing', 'Native resource is absent from frozen input.')
            for path in matches:
                used.add(path)
                groups[owner].append({'path': '' if path == root else path.removeprefix(root + '/'),
                                     'sha256': digest(files[path])})
            found.add(owner)
        mapping[key] = normalized
    if semantic != identity['native_config'] or found != set(expected) or used != set(files):
        raise ViewerError('agent_input_mismatch', 'Native input or resources differ from the frozen configuration.')
    return native


