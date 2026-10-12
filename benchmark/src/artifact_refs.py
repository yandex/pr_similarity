"""Exact-byte references for immutable owned artifacts."""

from pathlib import Path
from typing import TypedDict

from benchmark.errors import BenchmarkError
from benchmark.layout import artifact_path
from benchmark.storage import bytes_sha256, parse_json


class ArtifactReference(TypedDict):
    """Bind an immutable file to its exact stored bytes."""

    path: str
    size_bytes: int
    sha256: str


def reference(directory: Path, relative: str) -> ArtifactReference:
    """Record a complete owned artifact after its atomic write has succeeded."""
    path = artifact_path(directory, relative, must_exist=True)
    content = path.read_bytes()
    return {'path': relative, 'size_bytes': len(content), 'sha256': bytes_sha256(content)}


def read_reference(directory: Path, receipt: ArtifactReference | dict) -> dict:
    """Read an immutable JSON result only after size and content digest agree."""
    if set(receipt) != {'path', 'size_bytes', 'sha256'}:
        raise BenchmarkError('invalid_artifact_reference', 'Artifact reference has unsupported fields.')
    path = artifact_path(directory, receipt['path'], must_exist=True)
    content = path.read_bytes()
    if type(receipt['size_bytes']) is not int or len(content) != receipt['size_bytes']:
        raise BenchmarkError('artifact_digest_mismatch', 'Artifact size differs from its immutable receipt.')
    if bytes_sha256(content) != receipt['sha256']:
        raise BenchmarkError('artifact_digest_mismatch', 'Artifact content differs from its immutable receipt.')
    return parse_json(content)
