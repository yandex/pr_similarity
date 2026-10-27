"""Validate collector exports independently and hash the exact frozen input."""

import re
from collections.abc import Sequence
from datetime import datetime

from viewer.errors import ViewerError
from viewer.models import Basket, BasketV1, BasketV2, BasketV3, JsonObject, Report, WireModel
from viewer.models.basket_v1 import Producer as LegacyProducerV1
from viewer.models.basket_v2 import BasketMeta as BasketMetaV2
from viewer.models.basket_v2 import BasketRecord as BasketRecordV2
from viewer.models.basket_v2 import Producer as LegacyProducerV2
from viewer.models.basket_v3 import BasketMeta, BasketRecord
from viewer.models.evidence import parse_evidence
from viewer.models.report import ReportSourceDescriptor
from viewer.util import digest, json_bytes
from viewer.validation.basket_sources import (
    SourceDescriptor,
    canonical_server,
    https_url,
    task_identity,
)
from viewer.validation.basket_sources import source_path as safe_path
from viewer.validation.schema import validate_document

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def sha256(value: object) -> str:
    """Hash canonical JSON using the frozen producer encoding."""
    return digest(json_bytes(value))


def _require(condition: bool, field: str = '<root>') -> None:
    """Reject a failed frozen-basket invariant with a safe message."""
    if not condition:
        raise ViewerError('invalid_basket', f'Basket field at {field} is inconsistent; re-export the basket.')


def _utc(value: str | None) -> None:
    """Validate an exported UTC timestamp."""
    if value is not None:
        _require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', value)))
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ViewerError('invalid_basket', 'Basket contains an invalid UTC timestamp.') from exc


def _url(value: str) -> None:
    """Check a public repository URL against its expected identity."""
    https_url(value)


def validate_basket(data: object) -> Basket:
    """Keep malformed saved baskets inside the catalogue quarantine boundary."""
    try:
        return _validate_basket(data)
    except ViewerError as error:
        meta = data.get('meta') if isinstance(data, dict) else None
        version = (
            data.meta.schema_version
            if isinstance(data, (BasketV1, BasketV2, BasketV3))
            else meta.get('schema_version') if isinstance(meta, dict) else None
        )
        label = str(version) if type(version) is int and version in (1, 2, 3) else 'unknown'
        raise ViewerError('invalid_basket', f'Basket v{label}: {error.message}') from error


def _validate_basket(data: object) -> Basket:
    """Validate an autonomous basket before using it in an archive or catalogue."""
    if isinstance(data, (BasketV1, BasketV2, BasketV3)):
        data = data.to_wire()
    validate_document('basket', data)
    assert isinstance(data, dict)
    meta = data['meta']
    assert isinstance(meta, dict)
    version = meta['schema_version']
    assert isinstance(version, int)
    model_type = {1: BasketV1, 2: BasketV2, 3: BasketV3}[version]
    model = model_type.model_validate(data)
    _validate_model(model)
    return model


def _validate_model(data: Basket) -> None:
    """Check arithmetic and provenance on the structurally validated basket."""
    meta, rows = data.meta, data.tasks
    if 'stage' not in meta.model_fields_set:
        raise KeyError('stage')
    _require(meta.stage == 'export' and meta.status == 'completed' and meta.error is None)
    counts = meta.counts
    _require(counts.input == counts.succeeded == len(rows))
    _require(counts.failed == counts.rejected == counts.pending == 0)
    _require(meta.settings_hash == sha256(meta.settings))
    _require([item.stage for item in meta.lineage] == ['collect', 'filter', 'generate'])
    _require(meta.input is not None and meta.input.stage == 'generate' and meta.input.sha256 == meta.lineage[-1].sha256)
    _require(meta.repository.default_branch is not None and meta.completed_at is not None)
    for value in (meta.started_at, meta.updated_at, meta.completed_at):
        _utc(value)
    repository = meta.repository
    _require(repository.server_url == canonical_server(repository.server_url))
    _require(bool(repository.project_id.strip()) and repository.project_id != 'unresolved')
    _require('/' in repository.path and all(part not in ('', '.', '..') for part in repository.path.split('/')))
    _require(not any(character.isspace() for character in repository.path) and '\\' not in repository.path)
    _url(repository.web_url)
    for item in [meta, *meta.lineage]:
        _require(bool(_HEX64.fullmatch(item.settings_hash)) and item.settings_hash == sha256(item.settings))
        if isinstance(item.producer, (LegacyProducerV1, LegacyProducerV2)):
            for checksum in (item.producer.implementation_sha256, item.producer.dependencies_sha256):
                _require(bool(_HEX64.fullmatch(checksum)))
    for item in meta.lineage:
        _require(bool(_HEX64.fullmatch(item.sha256)))
    _validate_population(data)
    include = meta.settings.get('include_golden_diff')
    _require(type(include) is bool)
    for row in rows:
        request = row.change_request
        _require(request.repository == repository.path)
        kind = 'pull_request' if repository.provider == 'github' else 'merge_request'
        _require(request.kind == kind and row.id == task_identity(repository.to_wire(), kind, request.number))
        _url(request.url)
        _utc(request.merged_at)
        _require(
            all(
                _HEX40.fullmatch(revision) is not None
                for revision in (
                    row.revisions.before,
                    row.revisions.after,
                    row.revisions.before_tree,
                    row.revisions.after_tree,
                )
            )
        )
        source = SourceDescriptor.from_mapping(row.source.to_wire())
        proof = row.provenance.retrieval
        _utc(proof.checked_at)
        _require(proof.source_sha256 == sha256(source.as_mapping()))
        _require(
            (proof.before, proof.after, proof.before_tree, proof.after_tree)
            == (row.revisions.before, row.revisions.after, row.revisions.before_tree, row.revisions.after_tree)
        )
        evidence = row.provenance.source_revisions
        _require(evidence.head_sha == row.revisions.after)
        _require(evidence.expected_base_sha is None or evidence.expected_base_sha == row.revisions.before)
        safe_path(row.workspace, allow_root=True)
        changed = row.changes.files
        paths = [safe_path(file.path) for file in changed]
        _require(paths == sorted(set(paths)))
        stats = row.changes.stats
        _require(stats.files_count == len(changed))
        _require(stats.additions == sum(file.additions for file in changed))
        _require(stats.deletions == sum(file.deletions for file in changed))
        _require(stats.changed_lines == stats.additions + stats.deletions)
        for file in changed:
            for blob in (file.before_blob, file.after_blob):
                _require(blob is None or _HEX40.fullmatch(blob) is not None)
            for mode in (file.before_mode, file.after_mode):
                _require(mode is None or mode in ('100644', '100755'))
            status = file.status
            _require((file.before_blob is None) == (status == 'added'))
            _require((file.after_blob is None) == (status == 'deleted'))
            _require((file.before_mode is None) == (status == 'added'))
            _require((file.after_mode is None) == (status == 'deleted'))
        task = row.task
        _require(bool(task.task.strip()) and all(value.strip() for value in task.subtasks))
        expected = f'Task: {task.task}'
        if task.subtasks:
            expected += '\n\nSubtasks:\n' + '\n'.join('- ' + value.replace('\n', '\n  ') for value in task.subtasks)
        _require(row.agent_task == expected)
        _require(row.provenance.generation.issue_ids == [item.id for item in row.provenance.issue_refs])
        _require(
            ('golden_diff' in row.model_fields_set) == include and (not include or isinstance(row.golden_diff, str))
        )


def _validate_population(data: Basket) -> None:
    """Validate known omissions and ordered generation inputs without external receipts."""
    meta, rows = data.meta, data.tasks
    if isinstance(data, BasketV1):
        omitted = data.meta.omitted_failed_ids
    else:
        omitted = [item.id for item in data.meta.omissions]
        _validate_selection(data.meta, data.tasks)
    identities = [row.id for row in rows] + omitted
    _require(len({value.casefold() for value in identities}) == len(identities))
    _require(meta.partial == bool(omitted))


def _validate_selection(meta: BasketMetaV2 | BasketMeta, rows: Sequence[BasketRecordV2 | BasketRecord]) -> None:
    """Bind v2/v3 coverage arithmetic, omissions and task provenance to selection."""
    coverage, selection, omissions = meta.coverage, meta.selection, meta.omissions
    discovery = coverage.discovery
    _require(discovery.selected <= discovery.matched, 'meta.coverage')
    _require(discovery.capped == (discovery.selected < discovery.matched), 'meta.coverage')
    for stage, counts in (('collect', coverage.collect), ('filter', coverage.filter), ('generate', coverage.generate)):
        _require(
            counts.input == counts.succeeded + counts.failed + counts.rejected + counts.pending,
            f'meta.coverage.{stage}',
        )
        _require(counts.pending == 0, f'meta.coverage.{stage}')
        _require(
            counts.failed == sum(item.stage == stage and item.kind == 'failed' for item in omissions),
            f'meta.coverage.{stage}',
        )
    _require(coverage.collect.input == discovery.selected, 'meta.coverage')
    _require(coverage.filter.input == coverage.collect.succeeded, 'meta.coverage')
    _require(coverage.selected <= coverage.filter.succeeded, 'meta.coverage')
    _require(coverage.generate.input == coverage.selected, 'meta.coverage')
    _require(coverage.generate.succeeded == len(rows), 'meta.coverage')
    _require(coverage.generate.rejected == sum(item.kind == 'rejected' for item in omissions), 'meta.coverage')
    selected, keys = selection.selected_ids, selection.generation_keys
    _require(len(selected) == coverage.selected, 'meta.coverage')
    _require(len({value.casefold() for value in selected}) == len(selected), 'meta.selection')
    _require(all(re.fullmatch(r'change:[0-9a-f]{64}', value) for value in selected), 'meta.selection')
    _require(set(selected) == set(keys), 'meta.selection')
    for value in (selection.sha256, selection.input_sha256, selection.fingerprint, *keys.values()):
        _require(bool(_HEX64.fullmatch(value)), 'meta.selection')
    _require(
        selection.fingerprint == sha256({'tasks': [[identity, keys[identity]] for identity in selected]}),
        'meta.selection.fingerprint',
    )
    generated = set()
    for item in omissions:
        _require(bool(re.fullmatch(r'change:[0-9a-f]{64}', item.id)), 'meta.omissions')
        _require(bool(_HEX64.fullmatch(item.result_sha256)), 'meta.omissions')
        _require(item.kind != 'rejected' or item.stage == 'generate', 'meta.omissions')
        if item.stage == 'generate':
            _require(item.id in selected, 'meta.omissions')
            generated.add(item.id)
        else:
            _require(item.id not in selected, 'meta.omissions')
    _require([row.id for row in rows] == [identity for identity in selected if identity not in generated], 'tasks')
    for row in rows:
        _require(
            row.provenance.generation.input_sha256 == keys[row.id],
            'tasks.provenance.generation.input_sha256',
        )


def basket_summary(data: object) -> JsonObject:
    """Project validated population counts; v1 coverage remains unknown."""
    data = validate_basket(data)
    meta = data.meta
    coverage: JsonObject | None = None
    if isinstance(data, BasketV1):
        failed, rejected = len(data.meta.omitted_failed_ids), 0
    else:
        failed = sum(item.kind == 'failed' for item in data.meta.omissions)
        rejected = sum(item.kind == 'rejected' for item in data.meta.omissions)
        coverage = {
            stage: {
                'input': counts.input,
                'succeeded': counts.succeeded,
                'rejected': counts.rejected,
                'failed': counts.failed,
                'pending': counts.pending,
            }
            for stage, counts in (
                ('collect', data.meta.coverage.collect),
                ('filter', data.meta.coverage.filter),
                ('generate', data.meta.coverage.generate),
            )
        }
    return {
        'schema_version': meta.schema_version,
        'partial': meta.partial,
        'total_tasks': len(data.tasks),
        'failed_count': failed,
        'rejected_count': rejected,
        'coverage': coverage,
    }


class _Population(WireModel):
    """Fields needed to bind a report population to its frozen basket."""

    partial: bool
    total_tasks: int | float
    omitted_failed_count: int | float


class _TaskLink(WireModel):
    """Immutable basket fields projected into each public report task."""

    id: str
    source: ReportSourceDescriptor
    workspace: str
    agent_task: str
    diff_policy: str
    repository: str
    number: int | float
    url: str
    title: str
    kind: str
    before: str
    after: str
    before_tree: str
    after_tree: str


class _ReportLinks(WireModel):
    """Accept the independently usable basket-report binding boundary."""

    basket: _Population
    tasks: list[_TaskLink]


def validate_basket_report(basket: object, report: object) -> None:
    """Check the same basket/report population links on import and stored reads."""
    basket = validate_basket(basket)
    report = parse_evidence(_ReportLinks, report.to_wire() if isinstance(report, Report) else report, 'invalid_basket')
    summary = basket_summary(basket)
    _require(report.basket.partial == summary['partial'])
    _require(report.basket.total_tasks == summary['total_tasks'])
    _require(report.basket.omitted_failed_count == summary['failed_count'])
    _require([row.id for row in basket.tasks] == [row.id for row in report.tasks])
    for task, source in zip(report.tasks, basket.tasks, strict=True):
        _require(task.source.to_wire() == source.source.to_wire())
        _require(
            (task.workspace, task.agent_task, task.diff_policy)
            == (source.workspace, source.agent_task, source.diff_policy)
        )
        _require(
            (task.repository, task.number, task.url, task.title, task.kind)
            == (
                source.change_request.repository,
                source.change_request.number,
                source.change_request.url,
                source.change_request.title,
                source.change_request.kind,
            )
        )
        _require(
            (task.before, task.after, task.before_tree, task.after_tree)
            == (
                source.revisions.before,
                source.revisions.after,
                source.revisions.before_tree,
                source.revisions.after_tree,
            )
        )
