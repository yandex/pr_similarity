"""Process-local progress observations shared by HTTP and background workers."""

import threading
import time
from collections.abc import Callable
from copy import deepcopy


class ActivityTracker:
    """Observe active work without changing its durable state or taking service locks."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        """Create a lock-protected registry using the supplied observation clock."""
        self.clock = clock
        self.lock = threading.Lock()
        self.items: dict[str, dict] = {}

    def start(self, key: str, *, run_id: str, actor: str, total_bytes: int | None = None) -> None:
        """Register the identity and start time of an admitted operation."""
        with self.lock:
            self.items[key] = dict(
                id=key,
                run_id=run_id,
                actor=actor,
                started_at=self.clock(),
                phase='receiving',
                received_bytes=0,
                total_bytes=total_bytes,
            )

    def update(self, key: str, *, received_bytes: int | None = None, phase: str | None = None) -> None:
        """Record observed bytes or phase for an operation that is still active."""
        with self.lock:
            row = self.items.get(key)
            if row is not None:
                if received_bytes is not None:
                    row['received_bytes'] = received_bytes
                if phase is not None:
                    row['phase'] = phase

    def finish(self, key: str) -> None:
        """Remove a terminal operation from the active registry."""
        with self.lock:
            self.items.pop(key, None)

    def snapshot(self) -> dict[str, dict]:
        """Copy active observations with elapsed time at this read."""
        with self.lock:
            return {
                key: {**deepcopy(row), 'elapsed_seconds': max(0.0, self.clock() - row['started_at'])}
                for key, row in self.items.items()
            }
