"""Immutable usage capture metadata and closed receipt records."""

from .base import WireModel
from .report import ReportTelemetry


class TelemetryOwner(WireModel):
    """Bind native usage capture to one registered attempt."""

    run_id: str
    task_id: str
    attempt_id: str


class TelemetryMetadata(TelemetryOwner):
    """Allowlist the metadata persisted into the sanitized SQLite database."""

    format: str
    runtime: str
    adapter_version: str
    source_schema_sha256: str
    root_session_id: str | None
    complete: bool


class TelemetryReceipt(TelemetryOwner):
    """Describe saved usage bytes or an explicit unavailable capture."""

    status: str
    archive_complete: bool
    telemetry: ReportTelemetry | None = None
    format: str | None = None
    runtime: str | None = None
    adapter_version: str | None = None
    source_schema_sha256: str | None = None
    db_sha256: str | None = None
    db_size_bytes: int | None = None
    usage_sha256: str | None = None
    usage_path: str | None = None
