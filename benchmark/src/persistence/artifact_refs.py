"""Exact-byte references for immutable owned artifacts."""

from pathlib import Path

from pydantic import ValidationError

from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonObject
from benchmark.models.base import ArtifactReceipt, WireModel
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.storage import bytes_sha256, parse_json

ArtifactReference = ArtifactReceipt


def reference(directory: Path, relative: str) -> ArtifactReference:
    """Record a complete owned artifact after its atomic write has succeeded."""
    path = artifact_path(directory, relative, must_exist=True)
    content = path.read_bytes()
    return ArtifactReference(path=relative, size_bytes=len(content), sha256=bytes_sha256(content))


def read_reference_json(directory: Path, receipt: ArtifactReference) -> JsonObject:
    """Verify an exact immutable receipt before decoding an open JSON document."""
    if set(receipt.to_wire()) != {'path', 'size_bytes', 'sha256'}:
        raise BenchmarkError('invalid_artifact_reference', 'Artifact reference has unsupported fields.')
    path = artifact_path(directory, receipt.path, must_exist=True)
    content = path.read_bytes()
    if type(receipt.size_bytes) is not int or len(content) != receipt.size_bytes:
        raise BenchmarkError('artifact_digest_mismatch', 'Artifact size differs from its immutable receipt.')
    if bytes_sha256(content) != receipt.sha256:
        raise BenchmarkError('artifact_digest_mismatch', 'Artifact content differs from its immutable receipt.')
    return parse_json(content)


def read_reference[T: WireModel](
    directory: Path, receipt: ArtifactReference, model: type[T], *, error_code: str = 'invalid_artifact_reference'
) -> T:
    """Read one typed immutable fact after size and content digest agree."""
    value = read_reference_json(directory, receipt)
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise BenchmarkError(error_code, 'Artifact does not match its expected object contract.') from error
