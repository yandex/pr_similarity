"""Benchmark-owned object operations needed for frozen workspaces and triplets."""

from threading import Event
from typing import Protocol

from benchmark.models.basket import BasketRecord
from benchmark.vcs.git import TreeEntry


class VCS(Protocol):
    """Keep runtime consumers independent of hosting discovery and collector code."""

    timeout: int
    stop: Event | None

    def ensure(self, revisions: list[str], *, fetch_refs: list[str] | None = None) -> None:
        """Deliver the exact frozen commits using declared full-ref hints."""
        ...

    def tree(self, revision: str) -> dict[str, TreeEntry]:
        """Return immutable path, mode and object identities for one frozen commit."""
        ...

    def blob(self, entry: TreeEntry) -> bytes:
        """Read verified bytes for an immutable blob."""
        ...

    def verify_reference(self, row: BasketRecord) -> None:
        """Verify exported trees and the complete change manifest before agent preparation."""
        ...
