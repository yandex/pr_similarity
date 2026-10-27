"""Normalized harness configuration after the existing YAML/schema boundary."""

from typing import Annotated, Literal

from pydantic import Field

from .base import ValidatedJsonObject, WireModel
from .evidence import IdentityRetries


class AgentNativeConfig(WireModel):
    """Keep the native OpenCode document opaque to the harness."""

    opencode_json: ValidatedJsonObject


class ApiKeyAuth(WireModel):
    """Use environment credentials without an OAuth profile."""

    mode: Literal['api_key']
    profile: None
    refresh_timeout_seconds: int
    expiry_margin_seconds: int


class ChatGPTAuth(WireModel):
    """Use the explicit profile required by the existing OAuth config schema."""

    mode: Literal['chatgpt_oauth']
    profile: str
    refresh_timeout_seconds: int
    expiry_margin_seconds: int


type AgentAuth = Annotated[ApiKeyAuth | ChatGPTAuth, Field(discriminator='mode')]


class AgentSetup(WireModel):
    """Bind a setup script to its independent timeout."""

    script: str
    timeout_seconds: int


class AgentConfiguration(WireModel):
    """Separate harness controls from the native agent configuration."""

    name: str
    runtime: str
    config: AgentNativeConfig
    timeout_seconds: int
    setup: AgentSetup | None
    instructions_file: str | None
    retries: IdentityRetries
    diff_excludes: list[str]
    env: dict[str, str]
    credentials: dict[str, str]
    auth: AgentAuth


class SandboxConfiguration(WireModel):
    """Configure the existing source-only bubblewrap boundary."""

    backend: Literal['bubblewrap']
    executable: str
    auto_install: bool


class ShutdownConfiguration(WireModel):
    """Bound graceful process shutdown before forced cleanup."""

    grace_seconds: int


class ProvisioningConfiguration(WireModel):
    """Locate the pinned runtime cache and bound downloads."""

    cache_dir: str
    download_timeout_seconds: int


class GitConfiguration(WireModel):
    """Bound source transport time, history depth and transferred bytes."""

    timeout_seconds: int
    initial_depth: int
    max_depth: int
    max_transfer_bytes: int


class Configuration(WireModel):
    """Hold normalized nonsecret configuration using the unchanged YAML schema."""

    schema_version: Literal[1]
    workers: int
    tags: list[str]
    agent: AgentConfiguration
    sandbox: SandboxConfiguration
    shutdown: ShutdownConfiguration
    provisioning: ProvisioningConfiguration
    git: GitConfiguration
