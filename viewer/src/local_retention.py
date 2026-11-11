"""Conservative local history retention; closed remote backups remain intact."""

from collections.abc import Callable

from viewer.config import Config
from viewer.local_files import LocalFiles, fsync_directory
from viewer.local_repository import LocalRepository
from viewer.util import safe_path


class LocalRetention:
    """Prune expired local history without modifying checkpoint dependencies.

    Remote history is retained. Its deletion requires an authoritative checkpoint
    dependency registry; a potentially incomplete object LIST cannot authorize GC.
    """

    def __init__(
        self,
        config: Config,
        repository: LocalRepository,
        files: LocalFiles,
        *,
        busy: Callable[[str], bool] | None = None,
    ) -> None:
        """Bind local history, pin-aware file cleanup and the service's busy-run predicate."""
        self.config, self.repository, self.files = config, repository, files
        self.busy = busy or (lambda run_id: False)

    def run(self) -> dict:
        """Protect current heads, pending backups and reader pins while pruning."""
        self.repository.observe_retired_versions()
        before = self.repository.database.clock() - self.config.version_retention_seconds
        removed_versions = 0
        removed_bytes = 0
        for version in self.repository.versions():
            if self.busy(version.run_id):
                continue
            record = self.repository.retention_candidate(version.run_id, version.generation, before=before)
            if record is None:
                continue
            if record.local_path and not self.files.evict(record.local_path):
                continue
            if not self.repository.prune_version(record, before=before):
                continue
            marker = self.files.metadata / safe_path(
                f'{record.owner}/{record.run_id}/{record.generation}-{record.archive_sha256}.json'
            )
            if marker.exists():
                marker.unlink()
                fsync_directory(marker.parent)
            removed_versions += 1
            removed_bytes += record.cache_bytes
        return {'removed_versions': removed_versions, 'removed_bytes': removed_bytes, 'remote_history': 'retained'}
