"""Typed archive-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import ArtifactReceipt as ArchiveManifestFileItem
from .base import WireModel


class ArchiveManifestMissingArtifactItem(WireModel):
    """Represent the ArchiveManifestMissingArtifactItem object in the archive-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/missing_artifacts/items'
    path: str | None
    reason: str
    required: bool


class ArchiveManifest(WireModel):
    """Represent the ArchiveManifest object in the archive-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_family: Literal['pr_similarity_os.archive']
    schema_version: int
    run_id: str
    created_at: str
    report_revision: int
    report_sha256: str
    complete: bool
    missing_artifacts: list[ArchiveManifestMissingArtifactItem]
    files: list[ArchiveManifestFileItem]


ArchiveManifest.model_rebuild()
