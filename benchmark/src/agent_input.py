"""Portable frozen native inputs and shared effective snapshots owned by one run."""

import copy
import os
import re
from pathlib import Path

from benchmark.artifact_refs import read_reference, reference
from benchmark.config import RunConfig, _identity_native, _native_secrets
from benchmark.errors import BenchmarkError
from benchmark.layout import artifact_path
from benchmark.storage import atomic_write, bytes_sha256, document_bytes, write_json


def _groups(native: dict) -> list[tuple[dict, str, bool]]:
    """Keep the same native resource group ordering as the config loader."""
    return [(native, 'instructions', False), (native, 'plugin', True),
            (native.get('skills', {}), 'paths', False)]


def portable_native(native: dict) -> dict:
    """Rewrite only launcher-managed native resource references."""
    result = copy.deepcopy(native)
    for mapping, key, plugin in _groups(result):
        values = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            if not isinstance(value, str):
                continue
            prefix = 'file:///benchmark-runtime/' if plugin else '/benchmark-runtime/'
            if value.startswith(prefix + 'resources/'):
                values[index] = ('file://' if plugin else '') + value.removeprefix(prefix)
    return result


def freeze_agent_input(directory: Path, config: RunConfig) -> dict:
    """Freeze declared resources before workers start, preserving native rule order."""
    # Local import avoids a module cycle when Invocation consumes these helpers.
    from benchmark.opencode import _native_resources

    agent = directory / 'agent'
    if agent.exists() or agent.is_symlink():
        raise BenchmarkError('immutable_input_conflict', 'Frozen agent input already exists.')
    agent.mkdir()
    native = portable_native(_native_resources(config, agent))
    identity = config.identity()
    instructions = config.data['agent']['instructions_file']
    if instructions is None:
        from importlib.resources import files
        content = files('benchmark').joinpath('resources/task_instructions.txt').read_bytes()
    else:
        content = Path(instructions).read_bytes()
    if bytes_sha256(content) != identity['instructions']['sha256']:
        raise BenchmarkError('resource_changed', 'Task instructions changed after config validation.')
    atomic_write(agent / 'task_instructions.txt', content)
    setup = config.data['agent']['setup']
    if setup is not None:
        content = Path(setup['script']).read_bytes()
        if bytes_sha256(content) != identity['setup']['sha256']:
            raise BenchmarkError('resource_changed', 'Setup resource changed after config validation.')
        atomic_write(agent / 'setup.sh', content)
    write_json(agent / 'opencode.json', native, preserve_order=True)
    resources = []
    secrets = [os.environ.get(source, '') for source in config.data['agent']['credentials'].values()]
    for path in sorted(agent.rglob('*')):
        if path.is_symlink():
            raise BenchmarkError('resource_symlink', 'Frozen input contains a symlink.')
        if not path.is_file():
            continue
        if any(value and value.encode() in path.read_bytes() for value in secrets):
            raise BenchmarkError('resource_secret', 'A declared input contains a credential; use environment bindings.')
        if path.name != 'opencode.json' or path.parent != agent:
            resources.append(reference(directory, path.relative_to(directory).as_posix()))
    selected = {'config': reference(directory, 'agent/opencode.json'), 'resources': resources}
    validate_agent_input(directory, selected, identity)
    return selected


def _verify_file(directory: Path, receipt: dict) -> bytes:
    """Bind a regular file's bytes without requiring it to contain JSON."""
    if set(receipt) != {'path', 'size_bytes', 'sha256'}:
        raise BenchmarkError('invalid_artifact_reference', 'Invalid shared input receipt.')
    content = artifact_path(directory, receipt['path'], must_exist=True).read_bytes()
    if (type(receipt['size_bytes']) is not int or len(content) != receipt['size_bytes']
            or bytes_sha256(content) != receipt['sha256']):
        raise BenchmarkError('artifact_digest_mismatch', 'Frozen agent input is damaged.')
    return content


def validate_agent_input(directory: Path, selected: dict, identity: dict) -> dict:
    """Prove portable native input has the frozen semantic config and resources."""
    if (set(selected) != {'config', 'resources'}
            or selected['config'].get('path') != 'agent/opencode.json'
            or not isinstance(selected['resources'], list)):
        raise BenchmarkError('invalid_agent_input', 'Run has no valid frozen native input.')
    native = read_reference(directory, selected['config'])
    if not isinstance(native, dict):
        raise BenchmarkError('invalid_agent_input', 'Native input must be a JSON object.')
    _native_secrets(native)
    files = {}
    for receipt in selected['resources']:
        path = receipt['path']
        if (path in files or not (path.startswith('agent/resources/')
                or path in {'agent/task_instructions.txt', 'agent/setup.sh'})):
            raise BenchmarkError('invalid_agent_input', 'Shared resource has an unexpected owner path.')
        files[path] = _verify_file(directory, receipt)
    used = {'agent/task_instructions.txt'}
    if bytes_sha256(files.get('agent/task_instructions.txt', b'')) != identity['instructions']['sha256']:
        raise BenchmarkError('resource_changed', 'Frozen task instructions differ from identity.')
    if identity['setup'] is not None:
        used.add('agent/setup.sh')
        if bytes_sha256(files.get('agent/setup.sh', b'')) != identity['setup']['sha256']:
            raise BenchmarkError('resource_changed', 'Frozen setup differs from identity.')
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
                raise BenchmarkError('invalid_agent_input', 'Native resource refers to another field.')
            owner = (key, index)
            if owner not in expected:
                raise BenchmarkError('invalid_agent_input', 'Native resource has no semantic owner.')
            if owner not in groups:
                groups[owner] = []
                normalized.append({'resource': {'field': key, 'index': index, 'files': groups[owner]}})
            root = 'agent/' + raw
            matches = sorted(p for p in files if p == root or p.startswith(root + '/'))
            if not matches:
                raise BenchmarkError('resource_missing', 'Native resource is absent from frozen input.')
            for path in matches:
                used.add(path)
                groups[owner].append({'path': '' if path == root else path.removeprefix(root + '/'),
                                     'sha256': bytes_sha256(files[path])})
            found.add(owner)
        mapping[key] = normalized
    if semantic != identity['native_config'] or found != set(expected) or used != set(files):
        raise BenchmarkError('agent_input_mismatch', 'Native input or resources differ from the frozen configuration.')
    return native


def save_effective(directory: Path, native: dict) -> dict:
    """Save one unique portable effective snapshot with atomic concurrent publication."""
    content = document_bytes(portable_native(native))
    relative = 'agent/effective/' + bytes_sha256(content) + '.json'
    path = artifact_path(directory, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise BenchmarkError('invalid_artifact_reference', 'Effective snapshot is a symlink.')
    if path.exists():
        if artifact_path(directory, relative, must_exist=True).read_bytes() != content:
            raise BenchmarkError('artifact_digest_mismatch', 'Effective snapshot digest does not match its contents.')
    else:
        atomic_write(path, content)
    return reference(directory, relative)


def install_agent_input(directory: Path, selected: dict, identity: dict, runtime: Path) -> dict:
    """Copy verified frozen inputs into one attempt's private readonly launcher mount."""
    native = validate_agent_input(directory, selected, identity)
    for receipt in selected['resources']:
        content = _verify_file(directory, receipt)
        relative = receipt['path'].removeprefix('agent/')
        target = artifact_path(runtime, relative)
        atomic_write(target, content)
    for mapping, key, plugin in _groups(native):
        values = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            raw = value.removeprefix('file://') if plugin and isinstance(value, str) else value
            if isinstance(raw, str) and raw.startswith('resources/'):
                values[index] = ('file://' if plugin else '') + '/benchmark-runtime/' + raw
    write_json(runtime / 'config.json', native, preserve_order=True)
    return native


def benchmark_settings(identity: dict) -> dict:
    """Expose only frozen harness controls for publication metadata readback."""
    setup = identity['setup']
    return {'runtime': identity['runtime'], 'timeout_seconds': identity['timeout_seconds'],
            'setup_timeout_seconds': setup['timeout_seconds'] if setup is not None else None,
            'retries': copy.deepcopy(identity['retries']), 'diff_excludes': list(identity['diff_excludes'])}
