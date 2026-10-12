"""Collector-owned object operations, independent of hosting API discovery."""

from typing import Protocol

from basket_collector.contracts import Changes, ResolvedRevisions, SourceRevisions
from basket_collector.sources import RetrievalReceipt, SourceDescriptor


class VCS(Protocol):
    """Require only the object/revision operations needed by the filter stage."""

    def prepare(self) -> None:
        """Create or verify an isolated host object cache."""
        ...

    def fetch_commit(self, sha: str, fetch_refs: list[str] | None = None) -> None:
        """Deliver an exact commit using ordered full-ref hints."""
        ...

    def resolve_revisions(
        self, source: SourceRevisions, api_commits: list[str], fetch_refs: list[str] | None = None
    ) -> ResolvedRevisions:
        """Verify complete frozen history and return commit/tree identities."""
        ...

    def read_changes(self, resolved: ResolvedRevisions) -> Changes:
        """Read the exact supported file manifest, patch and statistics."""
        ...

    def verify_retrieval(self, source: SourceDescriptor, resolved: ResolvedRevisions) -> RetrievalReceipt:
        """Prove declared source delivery from an empty cache."""
        ...
