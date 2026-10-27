"""Portable frozen native inputs and shared effective snapshots owned by one run."""

import copy
import glob
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from benchmark.config import RunConfig, _identity_native, _native_secrets
from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonObject, JsonValue
from benchmark.models.evidence import BenchmarkSettings, ConfigIdentity, ResourceFile
from benchmark.models.report import ReportAgentInput
from benchmark.persistence.artifact_refs import (
    ArtifactReference,
    read_reference_json,
    reference,
)
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.storage import (
    atomic_write,
    bytes_sha256,
    document_bytes,
    write_json,
)


def _native_resources(config: RunConfig, directory: Path) -> JsonObject:
    """Copy declared local resources; relative config paths resolve from its YAML."""
    native = copy.deepcopy(config.data.agent.config.opencode_json)
    expected = {(item.field, item.index): item.files for item in config.identity().resources}
    groups = [(native, 'instructions', False), (native, 'plugin', True)]
    skills = native.get('skills')
    if isinstance(skills, dict):
        groups.append((skills, 'paths', False))
    for group_index, (mapping, key, plugin) in enumerate(groups):
        values = mapping.get(key)
        if not isinstance(values, list):
            continue
        rewritten: list[JsonValue] = []
        for index, value in enumerate(values):
            if not isinstance(value, str):
                rewritten.append(value)
                continue
            parsed = urlsplit(value)
            if parsed.scheme in ('http', 'https') or value.startswith('{env:'):
                rewritten.append(value)
                continue
            if plugin and not (parsed.scheme == 'file' or value.startswith(('.', '/', '~'))):
                rewritten.append(value)
                continue
            pattern = Path(parsed.path if parsed.scheme == 'file' else value).expanduser()
            if not pattern.is_absolute():
                pattern = config.directory / pattern
            matches = sorted(Path(item) for item in glob.glob(str(pattern)))
            if not matches:
                raise BenchmarkError('resource_missing', 'A declared local OpenCode resource disappeared.', exit_code=1)
            copied = []
            for match_index, source in enumerate(matches):
                relative = f'resources/{group_index}-{index}-{match_index}/{source.name}'
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, symlinks=True)
                    for child in sorted(target.rglob('*')):
                        if child.is_symlink():
                            raise BenchmarkError(
                                'resource_changed', 'A declared resource now contains a symlink.', exit_code=1
                            )
                        if child.is_file():
                            copied.append(
                                ResourceFile(
                                    path=child.relative_to(target).as_posix(), sha256=bytes_sha256(child.read_bytes())
                                )
                            )
                else:
                    shutil.copyfile(source, target)
                    copied.append(ResourceFile(path='', sha256=bytes_sha256(target.read_bytes())))
                destination = '/benchmark-runtime/' + relative
                rewritten.append('file://' + destination if plugin else destination)
            if copied != expected.get((key, index)):
                raise BenchmarkError(
                    'resource_changed', 'A declared resource changed after config validation.', exit_code=1
                )
        mapping[key] = rewritten
    return native


def _groups(native: JsonObject) -> list[tuple[JsonObject, str, bool]]:
    """Keep the same native resource group ordering as the config loader."""
    skills = native.get('skills', {})
    return [
        (native, 'instructions', False),
        (native, 'plugin', True),
        (skills if isinstance(skills, dict) else {}, 'paths', False),
    ]


def portable_native(native: JsonObject) -> JsonObject:
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


def freeze_agent_input(directory: Path, config: RunConfig) -> ReportAgentInput:
    """Freeze declared resources before workers start, preserving native rule order."""
    agent = directory / 'agent'
    if agent.exists() or agent.is_symlink():
        raise BenchmarkError('immutable_input_conflict', 'Frozen agent input already exists.')
    agent.mkdir()
    native = portable_native(_native_resources(config, agent))
    identity = config.identity()
    instructions = config.data.agent.instructions_file
    if instructions is None:

        content = files('benchmark').joinpath('resources/task_instructions.txt').read_bytes()
    else:
        content = Path(instructions).read_bytes()
    if bytes_sha256(content) != identity.instructions.sha256:
        raise BenchmarkError('resource_changed', 'Task instructions changed after config validation.')
    atomic_write(agent / 'task_instructions.txt', content)
    setup = config.data.agent.setup
    if setup is not None:
        content = Path(setup.script).read_bytes()
        assert identity.setup is not None
        if bytes_sha256(content) != identity.setup.sha256:
            raise BenchmarkError('resource_changed', 'Setup resource changed after config validation.')
        atomic_write(agent / 'setup.sh', content)
    write_json(agent / 'opencode.json', native, preserve_order=True)
    resources = []
    secrets = [os.environ.get(source, '') for source in config.data.agent.credentials.values()]
    for path in sorted(agent.rglob('*')):
        if path.is_symlink():
            raise BenchmarkError('resource_symlink', 'Frozen input contains a symlink.')
        if not path.is_file():
            continue
        if any(value and value.encode() in path.read_bytes() for value in secrets):
            raise BenchmarkError('resource_secret', 'A declared input contains a credential; use environment bindings.')
        if path.name != 'opencode.json' or path.parent != agent:
            resources.append(reference(directory, path.relative_to(directory).as_posix()))
    selected = ReportAgentInput(config=reference(directory, 'agent/opencode.json'), resources=resources)
    validate_agent_input(directory, selected, identity)
    return selected


def _verify_file(directory: Path, receipt: ArtifactReference) -> bytes:
    """Bind a regular file's bytes without requiring it to contain JSON."""
    if set(receipt.to_wire()) != {'path', 'size_bytes', 'sha256'}:
        raise BenchmarkError('invalid_artifact_reference', 'Invalid shared input receipt.')
    content = artifact_path(directory, receipt.path, must_exist=True).read_bytes()
    if (
        type(receipt.size_bytes) is not int
        or len(content) != receipt.size_bytes
        or bytes_sha256(content) != receipt.sha256
    ):
        raise BenchmarkError('artifact_digest_mismatch', 'Frozen agent input is damaged.')
    return content


def validate_agent_input(directory: Path, selected: ReportAgentInput, identity: ConfigIdentity) -> JsonObject:
    """Prove portable native input has the frozen semantic config and resources."""
    if (
        set(selected.to_wire()) != {'config', 'resources'}
        or selected.config.path != 'agent/opencode.json'
        or not isinstance(selected.resources, list)
    ):
        raise BenchmarkError('invalid_agent_input', 'Run has no valid frozen native input.')
    native = read_reference_json(directory, selected.config)
    if not isinstance(native, dict):
        raise BenchmarkError('invalid_agent_input', 'Native input must be a JSON object.')
    _native_secrets(native)
    files = {}
    for receipt in selected.resources:
        path = receipt.path
        if path in files or not (
            path.startswith('agent/resources/') or path in {'agent/task_instructions.txt', 'agent/setup.sh'}
        ):
            raise BenchmarkError('invalid_agent_input', 'Shared resource has an unexpected owner path.')
        files[path] = _verify_file(directory, receipt)
    used = {'agent/task_instructions.txt'}
    if bytes_sha256(files.get('agent/task_instructions.txt', b'')) != identity.instructions.sha256:
        raise BenchmarkError('resource_changed', 'Frozen task instructions differ from identity.')
    if identity.setup is not None:
        used.add('agent/setup.sh')
        if bytes_sha256(files.get('agent/setup.sh', b'')) != identity.setup.sha256:
            raise BenchmarkError('resource_changed', 'Frozen setup differs from identity.')
    semantic = _identity_native(native)
    assert isinstance(semantic, dict)
    expected = {(r.field, r.index): r for r in identity.resources}
    found = set()
    for group_index, (mapping, key, plugin) in enumerate(_groups(semantic)):
        values = mapping.get(key) if isinstance(mapping, dict) else None
        if not isinstance(values, list):
            continue
        normalized: list[JsonValue] = []
        groups: dict[tuple[str, int], list[JsonValue]] = {}
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
            assert isinstance(raw, str)
            root = 'agent/' + raw
            matches = sorted(p for p in files if p == root or p.startswith(root + '/'))
            if not matches:
                raise BenchmarkError('resource_missing', 'Native resource is absent from frozen input.')
            for path in matches:
                used.add(path)
                groups[owner].append(
                    {'path': '' if path == root else path.removeprefix(root + '/'), 'sha256': bytes_sha256(files[path])}
                )
            found.add(owner)
        mapping[key] = normalized
    if semantic != identity.native_config or found != set(expected) or used != set(files):
        raise BenchmarkError('agent_input_mismatch', 'Native input or resources differ from the frozen configuration.')
    return native


def save_effective(directory: Path, native: JsonObject) -> ArtifactReference:
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


def install_agent_input(
    directory: Path, selected: ReportAgentInput, identity: ConfigIdentity, runtime: Path
) -> JsonObject:
    """Copy verified frozen inputs into one attempt's private readonly launcher mount."""
    native = validate_agent_input(directory, selected, identity)
    for receipt in selected.resources:
        content = _verify_file(directory, receipt)
        relative = receipt.path.removeprefix('agent/')
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


def benchmark_settings(identity: ConfigIdentity) -> BenchmarkSettings:
    """Expose only frozen harness controls for publication metadata readback."""
    setup = identity.setup
    return BenchmarkSettings(
        runtime=identity.runtime,
        timeout_seconds=identity.timeout_seconds,
        setup_timeout_seconds=setup.timeout_seconds if setup is not None else None,
        retries=identity.retries.model_copy(deep=True),
        diff_excludes=list(identity.diff_excludes),
    )
