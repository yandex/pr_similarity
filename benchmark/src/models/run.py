"""Typed run-v2 artifacts after authoritative JSON Schema validation."""

from typing import ClassVar, Literal

from .base import ArtifactReceipt as RunAgentInputConfig
from .base import WireModel
from .basket import BasketRecord
from .report import ReportAgent as RunBaselineAgent
from .report import ReportAgentInput as RunAgentInput
from .report import ReportBasket as RunBaselineBasket
from .report import ReportConfig as RunBaselineConfig
from .report import ReportInference as RunBaselineInference
from .report import ReportLastReprice as RunBaselineLastReprice
from .report import ReportLastRescore as RunBaselineLastRescore
from .report import ReportMetric as RunBaselineMetric
from .report import ReportRun as RunBaselineRun
from .report import ReportState as RunBaselineState


class RunBaseline(WireModel):
    """Represent the RunBaseline object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/baseline'
    schema_family: Literal['pr_similarity_os.report']
    schema_version: int
    run: RunBaselineRun
    basket: RunBaselineBasket
    agent: RunBaselineAgent
    config: RunBaselineConfig
    metric: RunBaselineMetric
    last_rescore: RunBaselineLastRescore | None
    last_reprice: RunBaselineLastReprice | None
    inference: RunBaselineInference
    state: RunBaselineState | None = None


class RunTaskItem(WireModel):
    """Represent the RunTaskItem object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#/properties/tasks/items'
    id: str
    ordinal: int
    key: str
    path: str
    basket_row: BasketRecord


RunAgentInputResourceItem = RunAgentInputConfig


class Run(WireModel):
    """Represent the Run object in the run-v2 wire contract."""

    schema_pointer: ClassVar[str] = '#'
    schema_family: Literal['pr_similarity_os.run']
    schema_version: int
    id: str
    baseline: RunBaseline
    tasks: list[RunTaskItem]
    agent_input: RunAgentInput


Run.model_rebuild()
