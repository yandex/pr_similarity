"""Explicit Python provenance for scheduler tests that replace native execution."""

import hashlib
from pathlib import Path

from benchmark.models.base import ArtifactReceipt
from benchmark.models.runtime import (
    PythonAttemptReceipt,
    PythonFile,
    PythonProbe,
    PythonReceipt,
)
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.storage import write_json


def python_receipt() -> PythonReceipt:
    """Identify a synthetic interpreter without pretending to prepare a real sandbox."""
    entry = PythonFile(path='bin/python3.12', sha256='a' * 64, size_bytes=1)
    signature = hashlib.sha256(f'{entry.path}\0{entry.sha256}\0{entry.size_bytes}\n'.encode()).hexdigest()
    return PythonReceipt(
        version='3.12.0',
        implementation='cpython',
        platform='fixture',
        origin='benchmark_venv',
        content_sha256=signature,
        files=[entry],
        preflight='passed',
    )


def python_probe() -> PythonProbe:
    """Return the controlled child observation used by native-process substitutes."""
    return PythonProbe(
        version='3.12.0',
        executable='/sandbox-home/.venv/bin/python',
        prefix='/sandbox-home/.venv',
        base_prefix='/benchmark-python',
    )


def freeze_python(directory: Path) -> ArtifactReceipt:
    """Freeze the synthetic runtime through the same reference writer as production."""
    write_json(directory / '_meta/python.json', python_receipt())
    return reference(directory, '_meta/python.json')


def write_python_probe(directory: Path, relative: Path, runtime: ArtifactReceipt) -> ArtifactReceipt:
    """Sign successful synthetic probes for a replaced native attempt."""
    path = (relative / 'python.json').as_posix()
    write_json(
        directory / path,
        PythonAttemptReceipt(
            runtime=runtime,
            before_setup=python_probe(),
            before_agent=python_probe(),
        ),
    )
    return reference(directory, path)
