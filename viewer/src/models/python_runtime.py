"""Consumer-owned Python provenance and attempt isolation evidence."""

import hashlib
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .base import WireModel
from .evidence import ArtifactReceipt


class PythonFile(WireModel):
    """Measure one portable base-runtime file without retaining host paths."""

    model_config = ConfigDict(strict=True, extra='forbid')
    path: str = Field(pattern=r'^(?:bin/python3\.12|(?:lib|include)/[A-Za-z0-9_+./-]+)$')
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)


class PythonReceipt(WireModel):
    """Identify the setup interpreter and its package-free standard library."""

    model_config = ConfigDict(strict=True, extra='forbid')
    version: str = Field(pattern=r'^3\.12\.[0-9]+$')
    implementation: Literal['cpython']
    platform: str
    origin: Literal['benchmark_venv']
    content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    files: list[PythonFile]
    preflight: Literal['passed'] | None = None

    @model_validator(mode='after')
    def verify_manifest(self) -> 'PythonReceipt':
        """Reject unsafe paths, duplicates and identities inconsistent with their files."""
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)) or 'bin/python3.12' not in paths:
            raise ValueError('Python manifest must be sorted, unique and contain its interpreter.')
        identity = hashlib.sha256()
        for item in self.files:
            if any(part in {'', '.', '..'} for part in item.path.split('/')):
                raise ValueError('Unsafe Python manifest path.')
            identity.update(f'{item.path}\0{item.sha256}\0{item.size_bytes}\n'.encode())
        if identity.hexdigest() != self.content_sha256:
            raise ValueError('Python manifest identity differs.')
        return self


class PythonProbe(WireModel):
    """Record interpreter paths observed inside the attempt namespace."""

    model_config = ConfigDict(strict=True, extra='forbid')
    version: str = Field(pattern=r'^3\.12\.[0-9]+$')
    executable: Literal['/sandbox-home/.venv/bin/python']
    prefix: Literal['/sandbox-home/.venv']
    base_prefix: Literal['/benchmark-python']


class PythonAttemptReceipt(WireModel):
    """Bind pre-setup and pre-agent probes to the frozen run runtime."""

    runtime: ArtifactReceipt
    before_setup: PythonProbe
    before_agent: PythonProbe | None
