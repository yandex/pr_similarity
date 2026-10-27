"""Validate collector exports independently and hash the exact frozen input."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonObject
from benchmark.models.basket import (
    BasketDocument,
    BasketMeta,
    BasketMetaV2,
    BasketRecord,
    BasketV1,
    BasketV2,
    BasketV3,
    Generation,
)
from benchmark.models.sources import (
    SourceDescriptor,
    canonical_server,
    https_url,
    task_identity,
)
from benchmark.persistence.storage import bytes_sha256, parse_json, sha256
from benchmark.validation.contracts import validate_document
from benchmark.vcs.git import safe_path

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class Basket:
    """One nonempty whole basket; the original bytes travel with the run."""

    data: BasketDocument
    content: bytes
    sha256: str

    @property
    def rows(self) -> list[BasketRecord]:
        """Return the complete exported task queue."""
        return self.data.tasks

    @property
    def schema_version(self) -> int:
        """Return the validated input format without rewriting the document."""
        version = self.data.meta.schema_version
        assert version is not None
        return version

    @property
    def failed_count(self) -> int:
        """Count known failed omissions, excluding selected generation rejections."""
        if isinstance(self.data, BasketV1):
            return len(self.data.meta.omitted_failed_ids)
        meta = self.data.meta
        return sum(item.kind == 'failed' for item in meta.omissions)

    @property
    def rejected_count(self) -> int:
        """Count recorded selected generation rejections."""
        if isinstance(self.data, BasketV1):
            return 0
        return sum(item.kind == 'rejected' for item in self.data.meta.omissions)

    def warnings(self) -> list[str]:
        """Describe deprecated input versions and known omissions per invocation."""
        messages = []
        if self.schema_version < 3:
            messages.append(
                f'Basket v{self.schema_version} is deprecated; use a v3 basket for new runs. '
                'Do not rewrite frozen inputs of existing runs.'
            )
        if self.data.meta.partial:
            message = (
                f'Partial basket: all {len(self.rows)} exported tasks are included; '
                f'{self.failed_count} failed and {self.rejected_count} rejected records stay outside this run.'
            )
            if not isinstance(self.data, BasketV1):
                coverage = self.data.meta.coverage
                message += (
                    ' Omissions: '
                    + '; '.join(
                        f'{stage}: {counts.failed} failed'
                        + (f', {counts.rejected} rejected' if stage == 'generate' else '')
                        for stage, counts in (
                            ('collect', coverage.collect),
                            ('filter', coverage.filter),
                            ('generate', coverage.generate),
                        )
                    )
                    + '.'
                )
            messages.append(message)
        return messages


def _require(condition: bool, field: str = '<root>') -> None:
    """Reject a failed frozen-basket invariant with a safe message."""
    if not condition:
        raise BenchmarkError('invalid_basket', f'Basket field at {field} is inconsistent; re-export the basket.')


def _utc(value: str | None) -> None:
    """Validate an exported UTC timestamp."""
    if value is not None:

        _require(bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', value)))
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise BenchmarkError('invalid_basket', 'Basket contains an invalid UTC timestamp.') from exc


def _url(value: str) -> None:
    """Check a public repository URL against its expected identity."""
    https_url(value)


def _parse_basket(raw: JsonObject) -> BasketDocument:
    """Parse the explicit variant after its original versioned schema has accepted it."""
    meta = raw['meta']
    assert isinstance(meta, dict)
    version = meta['schema_version']
    if version == 1:
        return BasketV1.model_validate(raw)
    if version == 2:
        return BasketV2.model_validate(raw)
    return BasketV3.model_validate(raw)


def load_basket(path: Path) -> Basket:
    """Accept frozen v1, v2 and v3 exports without changing original bytes or identities."""
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(
            'basket_unreadable', 'Cannot read the frozen basket; check its path and permissions.'
        ) from exc
    raw = parse_json(content)
    validate_document('basket', raw)
    data = _parse_basket(raw)
    meta, rows = data.meta, data.tasks
    _require(meta.stage == 'export' and meta.status == 'completed' and meta.error is None)
    counts = meta.counts
    _require(counts.input == counts.succeeded == len(rows))
    _require(all(value == 0 for value in (counts.failed, counts.rejected, counts.pending)))
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
    for item in meta.lineage:
        _require(bool(_HEX64.fullmatch(item.sha256)))
    _validate_population(data)
    include = meta.settings.get('include_golden_diff')
    _require(type(include) is bool)
    for row in rows:
        request = row.change_request
        _require(request.repository == repository.path)
        kind = 'pull_request' if repository.provider == 'github' else 'merge_request'
        _require(request.kind == kind and row.id == task_identity(repository, kind, request.number))
        _url(request.url)
        _utc(request.merged_at)
        _require(
            all(
                _HEX40.fullmatch(value) is not None
                for value in (
                    row.revisions.before,
                    row.revisions.after,
                    row.revisions.before_tree,
                    row.revisions.after_tree,
                )
            )
        )
        source = SourceDescriptor.from_model(row.source)
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
            for value in (file.before_blob, file.after_blob):
                _require(value is None or _HEX40.fullmatch(value) is not None)
            for value in (file.before_mode, file.after_mode):
                _require(value is None or value in ('100644', '100755'))
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
    return Basket(data, content, bytes_sha256(content))


def _validate_population(data: BasketDocument) -> None:
    """Validate known omissions and ordered generation inputs without external receipts."""
    meta, rows = data.meta, data.tasks
    if isinstance(data, BasketV1):
        omitted = data.meta.omitted_failed_ids
    else:
        omitted = [item.id for item in data.meta.omissions]
        _validate_selection(data.meta, rows)
    identities = [row.id for row in rows] + omitted
    _require(len({value.casefold() for value in identities}) == len(identities))
    _require(meta.partial == bool(omitted))


def _validate_selection(meta: BasketMeta | BasketMetaV2, rows: list[BasketRecord]) -> None:
    """Bind coverage arithmetic, omissions and task provenance to selection."""
    coverage, selection, omissions = meta.coverage, meta.selection, meta.omissions
    discovery = coverage.discovery
    _require(discovery.selected <= discovery.matched, 'meta.coverage')
    _require(discovery.capped == (discovery.selected < discovery.matched), 'meta.coverage')
    for stage, counts in (('collect', coverage.collect), ('filter', coverage.filter), ('generate', coverage.generate)):
        _require(
            counts.input == sum((counts.succeeded, counts.failed, counts.rejected, counts.pending)),
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
    for digest in (selection.sha256, selection.input_sha256, selection.fingerprint, *keys.values()):
        _require(bool(_HEX64.fullmatch(digest)), 'meta.selection')
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
        generation = row.provenance.generation
        _require(
            isinstance(generation, Generation) and generation.input_sha256 == keys[row.id],
            'tasks.provenance.generation.input_sha256',
        )
