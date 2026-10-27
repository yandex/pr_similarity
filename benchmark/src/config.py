"""Strict run configuration and content-based resume identity."""

import copy
import glob
import math
import os
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from benchmark.errors import BenchmarkError
from benchmark.execution.python_runtime import PYTHON_CONTROLS
from benchmark.json_types import JsonObject, JsonValue
from benchmark.models.config import Configuration
from benchmark.models.evidence import (
    ConfigIdentity,
    IdentityInstructions,
    IdentityResource,
    IdentitySetup,
    ResourceFile,
)
from benchmark.persistence.storage import bytes_sha256
from benchmark.validation.contracts import validate_document

_RESERVED = (
    frozenset({'HOME', 'PATH', 'TMPDIR', 'PYTHONHASHSEED', 'PWD', 'OLDPWD', 'BASH_ENV', 'ENV', 'SHELLOPTS'})
    | PYTHON_CONTROLS
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
_ENV_REF = re.compile(r'^\{env:[A-Za-z_][A-Za-z_0-9]*\}$')


def _defaults() -> dict[str, object]:
    """Return fresh default configuration values for one load."""
    return {
        'workers': 1,
        'tags': [],
        'agent': {
            'auth': {'mode': 'api_key', 'profile': None, 'refresh_timeout_seconds': 30, 'expiry_margin_seconds': 60},
            'timeout_seconds': 1800,
            'setup': None,
            'instructions_file': None,
            'retries': {'max_retries': 0, 'categories': []},
            'diff_excludes': [],
            'env': {},
            'credentials': {},
        },
        'sandbox': {'backend': 'bubblewrap', 'executable': 'bwrap', 'auto_install': True},
        'shutdown': {'grace_seconds': 10},
        'provisioning': {'cache_dir': '~/.cache/prsimbm', 'download_timeout_seconds': 120},
        'git': {'timeout_seconds': 120, 'initial_depth': 1, 'max_depth': 4096, 'max_transfer_bytes': 536870912},
    }


class _UniqueLoader(yaml.SafeLoader):
    """Require each YAML mapping to have unique string keys."""


def _mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict[str, object]:
    """Require a configuration mapping with a safe field error."""
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise BenchmarkError('invalid_config', 'YAML keys must be unique strings.')
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _json_values(value: object, visiting: set[int] | None = None) -> None:
    """Reject YAML-only values, cycles and nonfinite numbers."""
    visiting = set() if visiting is None else visiting
    if type(value) in (dict, list):
        assert isinstance(value, (dict, list))
        identity = id(value)
        if identity in visiting:
            raise BenchmarkError('invalid_config', 'Recursive YAML aliases are not supported.')
        visiting.add(identity)
        values = value.values() if isinstance(value, dict) else value
        for child in values:
            _json_values(child, visiting)
        visiting.remove(identity)
    elif type(value) not in (type(None), str, bool, int, float) or (type(value) is float and not math.isfinite(value)):
        raise BenchmarkError('invalid_config', 'Configuration must contain finite JSON-compatible values.')


def _fill_defaults(data: dict[str, object], defaults: dict[str, object]) -> None:
    """Copy and normalize missing configuration defaults."""
    for key, default in defaults.items():
        if key not in data:
            data[key] = copy.deepcopy(default)
        else:
            value = data[key]
            if isinstance(value, dict) and isinstance(default, dict):
                _fill_defaults(value, default)


def _path(value: str, directory: Path) -> Path:
    """Resolve a configured path relative to its owning YAML file."""
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else directory / candidate).resolve()


def _resource_file(path: Path) -> str:
    """Hash a declared local resource with stable relative file identity."""
    try:
        return bytes_sha256(path.read_bytes())
    except OSError as exc:
        raise BenchmarkError(
            'resource_missing', 'Cannot read a configured resource; check its path and permissions.'
        ) from exc


def _native_secrets(value: object) -> None:
    """Reject embedded credentials in native OpenCode configuration."""
    if isinstance(value, list):
        for child in value:
            _native_secrets(child)
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key.lower() in _SECRET_KEYS
                and child not in (None, '')
                and (not isinstance(child, str) or _ENV_REF.fullmatch(child) is None)
            ):
                raise BenchmarkError('embedded_secret', 'Use an environment reference for native credentials.')
            _native_secrets(child)


def _reserved(key: str) -> bool:
    """Identify environment names controlled by the private launcher."""
    return key in _RESERVED or key.startswith(('XDG_', 'OPENCODE_', 'GIT_', 'LD_'))


def _identity_native(value: JsonValue) -> JsonValue:
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


def _ordered_rules(value: JsonValue) -> JsonValue:
    """Represent permission mappings as ordered pairs before canonical key sorting."""
    if isinstance(value, dict):
        return {'ordered_entries': [[key, _ordered_rules(child)] for key, child in value.items()]}
    if isinstance(value, list):
        return [_ordered_rules(child) for child in value]
    return value


@dataclass(frozen=True)
class RunConfig:
    """Normalized nonsensitive config and immutable content identity."""

    data: Configuration
    directory: Path
    _identity: ConfigIdentity
    source_path: Path | None = None

    def identity(self) -> ConfigIdentity:
        """Return a defensive copy of the frozen semantic inputs."""
        return self._identity.model_copy(deep=True)

    def credential_environment(self) -> dict[str, str]:
        """Read current bindings without retaining them in config or identity."""
        result = {}
        for destination, source in self.data.agent.credentials.items():
            value = os.environ.get(source)
            if not value:
                raise BenchmarkError('credentials_missing', f'Set environment variable {source} before running.')
            result[destination] = value
        return result


def _native_resources(native: JsonObject, directory: Path) -> tuple[JsonObject, list[IdentityResource]]:
    """Content-bind explicitly local instructions, skills and plugin resources."""
    semantic = _identity_native(native)
    assert isinstance(semantic, dict)
    receipts: list[IdentityResource] = []
    groups: list[tuple[JsonObject, str, bool]] = [(semantic, 'instructions', False), (semantic, 'plugin', True)]
    skills = semantic.get('skills')
    if isinstance(skills, dict):
        groups.append((skills, 'paths', False))
    for mapping, key, plugin in groups:
        values = mapping.get(key)
        if not isinstance(values, list):
            continue
        normalized = []
        for index, value in enumerate(values):
            if not isinstance(value, str):
                normalized.append(value)
                continue
            parsed = urlsplit(value)
            if parsed.scheme in ('http', 'https') or value.startswith('{env:'):
                normalized.append(value)
                continue
            if plugin and not (parsed.scheme == 'file' or value.startswith(('.', '/', '~'))):
                normalized.append(value)  # A package version remains a native dependency input.
                continue
            raw_path = parsed.path if parsed.scheme == 'file' else value
            pattern = str(_path(raw_path, directory))
            matches = sorted(Path(item) for item in glob.glob(pattern))
            if not matches:
                raise BenchmarkError('resource_missing', 'An explicitly configured local resource is missing.')
            items = []
            for path in matches:
                if path.is_dir():
                    for child in sorted(path.rglob('*')):
                        if child.is_symlink():
                            raise BenchmarkError(
                                'resource_symlink', 'Local resource directories must not contain symlinks.'
                            )
                        if child.is_file():
                            items.append(
                                ResourceFile(path=child.relative_to(path).as_posix(), sha256=_resource_file(child))
                            )
                else:
                    items.append(ResourceFile(path='', sha256=_resource_file(path)))
            receipt = IdentityResource(field=key, index=index, files=items)
            receipts.append(receipt)
            normalized.append({'resource': receipt.to_wire()})
        mapping[key] = normalized
    return semantic, receipts


def load_config(path: Path, *, workers: int | None = None) -> RunConfig:
    """Load YAML and freeze the meaningful agent inputs before execution.

    Args:
        path: Configuration file; declared resources resolve from its directory.
        workers: Optional technical concurrency override, excluded from identity.

    Returns:
        Normalized nonsensitive configuration with content-bound input identity.

    Raises:
        BenchmarkError: If fields, paths, resources or credential bindings are invalid.

    """
    try:
        resolved = path.expanduser().resolve(strict=True)
        data = yaml.load(resolved.read_text(encoding='utf-8'), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise BenchmarkError('invalid_config', 'Cannot read run config; check its path and YAML format.') from exc
    if not isinstance(data, dict):
        raise BenchmarkError('invalid_config', 'Run config must contain a YAML mapping.')
    _json_values(data)
    _fill_defaults(data, _defaults())
    if workers is not None:
        data['workers'] = workers
    agent = data.get('agent')
    if isinstance(agent, dict) and isinstance(agent.get('setup'), dict):
        agent['setup'].setdefault('timeout_seconds', 300)
    validate_document('config', data)
    settings = Configuration.model_validate(data)
    if any(not tag.strip() or any(ord(char) < 32 or ord(char) == 127 for char in tag) for tag in settings.tags):
        raise BenchmarkError('invalid_tags', 'YAML tags must be nonempty strings without control characters.')
    directory = resolved.parent
    agent = settings.agent
    overlap = set(agent.env) & set(agent.credentials)
    secret_env = any(re.search(r'(?:API_KEY|TOKEN|PASSWORD|SECRET)$', key) for key in agent.env)
    if overlap or secret_env or any(_reserved(key) for key in (*agent.env, *agent.credentials)):
        raise BenchmarkError(
            'invalid_environment', 'Environment bindings overlap or override reserved launcher settings.'
        )
    _native_secrets(agent.config.opencode_json)
    if agent.auth.mode == 'chatgpt_oauth' and 'OPENAI_API_KEY' in agent.credentials:
        raise BenchmarkError('auth_conflict', 'Remove the OpenAI API-key binding when using ChatGPT OAuth.')
    agent.retries.categories = sorted(set(agent.retries.categories))
    if settings.git.initial_depth > settings.git.max_depth:
        raise BenchmarkError('invalid_config', 'git.initial_depth must not exceed git.max_depth.')
    agent.diff_excludes = sorted(set(agent.diff_excludes))
    setup = None
    if agent.setup is not None:
        script = _path(agent.setup.script, directory)
        setup = IdentitySetup(sha256=_resource_file(script), timeout_seconds=agent.setup.timeout_seconds)
        agent.setup.script = str(script)
    if agent.instructions_file is None:
        content = files('benchmark').joinpath('resources', 'task_instructions.txt').read_bytes()
    else:
        instructions = _path(agent.instructions_file, directory)
        agent.instructions_file = str(instructions)
        try:
            content = instructions.read_bytes()
            content.decode('utf-8')
        except (OSError, UnicodeError) as exc:
            raise BenchmarkError('resource_missing', 'Instructions must be a readable UTF-8 file.') from exc
    if agent.auth.profile is not None:
        agent.auth.profile = str(_path(agent.auth.profile, directory))
    settings.provisioning.cache_dir = str(_path(settings.provisioning.cache_dir, directory))
    executable = settings.sandbox.executable
    if '/' in executable or executable.startswith('~'):
        settings.sandbox.executable = str(_path(executable, directory))
    native, resources = _native_resources(agent.config.opencode_json, directory)
    identity = ConfigIdentity(
        identity_version=1,
        runtime=agent.runtime,
        auth_mode=agent.auth.mode,
        native_config=native,
        timeout_seconds=agent.timeout_seconds,
        setup=setup,
        instructions=IdentityInstructions(protocol='benchmark_task_v1', sha256=bytes_sha256(content)),
        resources=resources,
        retries=agent.retries.model_copy(deep=True),
        diff_excludes=agent.diff_excludes.copy(),
        env=agent.env.copy(),
    )
    return RunConfig(settings, directory, identity, source_path=resolved)
