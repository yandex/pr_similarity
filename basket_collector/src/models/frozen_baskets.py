"""Read-only published basket contracts, independent of current stage checkpoints."""

import re
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from basket_collector.models.artifacts import (
    BasketDocument,
    BasketMeta,
    BasketRecord,
    LineageReceipt,
    Meta,
    Producer,
    SelectionReceipt,
    StrictModel,
    _check_ids,
    _check_records,
    _sha,
    _validate_selected_basket,
)


class HistoricalProducer(Producer):
    """Retain old producer digests as metadata without inspecting installed code."""

    implementation_sha256: StrictStr
    dependencies_sha256: StrictStr

    @field_validator('implementation_sha256', 'dependencies_sha256')
    @classmethod
    def check_digest(cls, value: str) -> str:
        """Require the recorded historical digests to retain their original shape."""
        return _sha(value, 64)


class HistoricalLineage(LineageReceipt):
    """Validate historical ancestor receipts without loading their stage documents."""

    producer: HistoricalProducer


class SelectionV2(SelectionReceipt):
    """Read the selection receipt embedded in a previously published v2 basket."""

    schema_version: Literal[2]

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Require the exact historical selection version without coercion."""
        if type(value) is not int or value != 2:
            raise ValueError('published v2 selection requires integer schema_version 2')
        return value


class BasketMetaV2(BasketMeta):
    """Read v2 coverage and provenance without accepting a resumable v2 stage."""

    schema_version: Literal[2]
    producer: HistoricalProducer
    lineage: list[HistoricalLineage]
    selection: SelectionV2

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Require an exact integer v2 frozen export."""
        if type(value) is not int or value != 2:
            raise ValueError('published basket requires integer schema_version 2')
        return value


class BasketV2(StrictModel):
    """Validate a published v2 basket without converting it to the writer format."""

    meta: BasketMetaV2
    tasks: list[BasketRecord] = Field(min_length=1)

    @model_validator(mode='after')
    def check_records(self) -> 'BasketV2':
        """Keep current record and coverage invariants for the unchanged v2 task contract."""
        _validate_selected_basket(self.meta, self.tasks)
        return self


class BasketMetaV1(Meta):
    """Retain v1 omissions without inventing unavailable selection or coverage data."""

    schema_family: Literal['pr_similarity_os.basket'] = 'pr_similarity_os.basket'
    schema_version: Literal[1]
    stage: Literal['export'] = 'export'
    producer: HistoricalProducer
    lineage: list[HistoricalLineage]
    name: StrictStr
    description: StrictStr
    maintainers: list[StrictStr]
    partial: StrictBool
    omitted_failed_ids: list[StrictStr]

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Require an exact integer v1 frozen export."""
        if type(value) is not int or value != 1:
            raise ValueError('published basket requires integer schema_version 1')
        return value

    @model_validator(mode='after')
    def check_export(self) -> 'BasketMetaV1':
        """Validate known omissions and export counts without newer stage fingerprints."""
        if self.partial != bool(self.omitted_failed_ids):
            raise ValueError('partial must agree with omitted failed IDs')
        if any(re.fullmatch(r'change:[0-9a-f]{64}', item) is None for item in self.omitted_failed_ids):
            raise ValueError('invalid omitted change identity')
        if self.counts.input != self.counts.succeeded or any(
            (self.counts.failed, self.counts.rejected, self.counts.pending)
        ):
            raise ValueError('invalid export counts')
        return self


class BasketV1(StrictModel):
    """Validate original v1 frozen records while preserving their absent v2 fields."""

    meta: BasketMetaV1
    tasks: list[BasketRecord] = Field(min_length=1)

    @field_validator('tasks', mode='before')
    @classmethod
    def check_generation_shape(cls, value: object) -> object:
        """Reject v2-only generation fields rather than silently admitting mixed formats."""
        if isinstance(value, list):
            for task in value:
                provenance = task.get('provenance') if isinstance(task, dict) else None
                generation = provenance.get('generation') if isinstance(provenance, dict) else None
                if isinstance(generation, dict) and {'input_sha256', 'reused_from'} & generation.keys():
                    raise ValueError('v1 generation cannot contain v2 selection fields')
        return value

    @model_validator(mode='after')
    def check_records(self) -> 'BasketV1':
        """Bind historical records to their repository, omissions and optional diff policy."""
        _check_ids([item.id for item in self.tasks] + self.meta.omitted_failed_ids)
        _check_records(self.meta.repository, [(item.id, item.change_request) for item in self.tasks])
        if self.meta.counts.succeeded != len(self.tasks):
            raise ValueError('export count does not match records')
        include = self.meta.settings.get('include_golden_diff')
        if type(include) is not bool:
            raise ValueError('export settings need include_golden_diff bool')
        for task in self.tasks:
            present = 'golden_diff' in task.model_fields_set
            if present != include or (present and task.golden_diff is None):
                raise ValueError('golden_diff presence must match export setting')
        return self


type FrozenBasketDocument = BasketV1 | BasketV2 | BasketDocument


def validate_frozen_basket(value: object) -> FrozenBasketDocument:
    """Read a completed exported basket without opening old checkpoints or rewriting JSON."""
    meta = value.get('meta') if isinstance(value, dict) else None
    version = meta.get('schema_version') if isinstance(meta, dict) else None
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError('published basket requires integer schema_version 1, 2 or 3')
    document = (
        BasketV1.model_validate(value)
        if version == 1
        else BasketV2.model_validate(value) if version == 2 else BasketDocument.model_validate(value)
    )
    if document.meta.status != 'completed':
        raise ValueError('published basket must be a completed export')
    return document
