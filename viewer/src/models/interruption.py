"""Recovery observations kept separately from native execution results."""

from typing import Literal

from pydantic import ConfigDict, field_validator

from .base import WireModel
from .evidence import ArtifactReceipt


class Interruption(WireModel):
    """Bind a recovery observation to one immutable start and optional native seal."""

    model_config = ConfigDict(extra='forbid', strict=True)

    schema_family: Literal['pr_similarity_os.interruption']
    schema_version: Literal[1]
    run_id: str
    task_id: str
    attempt_id: str
    start: ArtifactReceipt
    seal: ArtifactReceipt | None
    observed_at: str
    status: Literal['interrupted']
    missing: list[Literal['seal', 'outcome']]

    @field_validator('schema_version', mode='before')
    @classmethod
    def version_is_integer(cls, value: object) -> int:
        """Reject scalar coercion before checking the supported version literal."""
        if type(value) is not int:
            raise ValueError('Interruption schema version must be an integer.')
        return value
