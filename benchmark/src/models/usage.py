"""Typed usage-v1 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import WireModel
from .report import ReportTelemetrySessionDb as UsageSourceDb
from .report import ReportTelemetryTokens as UsageRequestItemUsage


class UsageSessionItem(WireModel):
    """Represent the UsageSessionItem object in the usage-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/sessions/items'
    id: str
    parent_id: str | None
    agent_name: str | None
    kind: Literal['root', 'subagent', 'detached']


class UsageRequestItemBillingTokens(WireModel):
    """Represent the UsageRequestItemBillingTokens object in the usage-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/requests/items/properties/billing_tokens'
    input: int | None
    cache_read: int | None
    cache_write: int | None
    output: int | None


class UsageRequestItem(WireModel):
    """Represent the UsageRequestItem object in the usage-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/requests/items'
    id: str
    session_id: str
    provider: str
    model: str
    complete: bool
    usage: UsageRequestItemUsage
    billing_tokens: UsageRequestItemBillingTokens
    input_context_tokens: int | None
    reported_cost_usd: int | float | None


class Usage(WireModel):
    """Represent the Usage object in the usage-v1 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_family: Literal['pr_similarity_os.usage']
    schema_version: int
    run_id: str
    task_id: str
    attempt_id: str
    runtime: str
    adapter_version: str
    source_db: UsageSourceDb
    complete: bool
    sessions: list[UsageSessionItem]
    requests: list[UsageRequestItem]


Usage.model_rebuild()
