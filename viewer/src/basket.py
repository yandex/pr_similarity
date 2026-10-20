"""Validate collector exports independently and hash the exact frozen input."""

import re
from datetime import datetime

from viewer.basket_sources import (
    SourceDescriptor,
    canonical_server,
    https_url,
    task_identity,
)
from viewer.basket_sources import source_path as safe_path
from viewer.errors import ViewerError
from viewer.schema import validate_document
from viewer.util import digest, json_bytes

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


def validate_basket(data: dict) -> None:
    """Keep malformed saved baskets inside the catalogue quarantine boundary."""
    try:
        _validate_basket(data)
    except ViewerError as error:
        meta = data.get('meta') if isinstance(data, dict) else None
        version = meta.get('schema_version') if isinstance(meta, dict) else None
        label = str(version) if type(version) is int and version in (1, 2) else 'unknown'
        raise ViewerError('invalid_basket', f'Basket v{label}: {error.message}') from error


def _validate_basket(data: dict) -> None:
    """Validate an autonomous basket before using it in an archive or catalogue."""
    validate_document('basket', data)
    meta, rows = data['meta'], data['tasks']
    _require(meta['stage'] == 'export' and meta['status'] == 'completed' and meta['error'] is None)
    counts = meta['counts']
    _require(counts['input'] == counts['succeeded'] == len(rows))
    _require(all(counts[key] == 0 for key in ('failed', 'rejected', 'pending')))
    _require(meta['settings_hash'] == sha256(meta['settings']))
    _require([item['stage'] for item in meta['lineage']] == ['collect', 'filter', 'generate'])
    _require(meta['input'] == {'stage': 'generate', 'sha256': meta['lineage'][-1]['sha256']})
    _require(meta['repository']['default_branch'] is not None and meta['completed_at'] is not None)
    for value in (meta['started_at'], meta['updated_at'], meta['completed_at']):
        _utc(value)
    repository = meta['repository']
    _require(repository['server_url'] == canonical_server(repository['server_url']))
    _require(bool(repository['project_id'].strip()) and repository['project_id'] != 'unresolved')
    _require('/' in repository['path'] and all(part not in ('', '.', '..') for part in repository['path'].split('/')))
    _require(not any(character.isspace() for character in repository['path']) and '\\' not in repository['path'])
    _url(repository['web_url'])
    for item in [meta, *meta['lineage']]:
        _require(bool(_HEX64.fullmatch(item['settings_hash'])) and item['settings_hash'] == sha256(item['settings']))
        for key in ('implementation_sha256', 'dependencies_sha256'):
            _require(bool(_HEX64.fullmatch(item['producer'][key])))
    for item in meta['lineage']:
        _require(bool(_HEX64.fullmatch(item['sha256'])))
    _validate_population(data)
    include = meta['settings'].get('include_golden_diff')
    _require(type(include) is bool)
    for row in rows:
        request = row['change_request']
        _require(request['repository'] == repository['path'])
        kind = 'pull_request' if repository['provider'] == 'github' else 'merge_request'
        _require(request['kind'] == kind and row['id'] == task_identity(repository, kind, request['number']))
        _url(request['url'])
        _utc(request['merged_at'])
        _require(
            all(
                _HEX40.fullmatch(row['revisions'][key]) is not None
                for key in ('before', 'after', 'before_tree', 'after_tree')
            )
        )
        source = SourceDescriptor.from_mapping(row['source'])
        proof = row['provenance']['retrieval']
        _utc(proof['checked_at'])
        _require(proof['source_sha256'] == sha256(source.as_mapping()))
        _require(all(proof[key] == row['revisions'][key] for key in ('before', 'after', 'before_tree', 'after_tree')))
        evidence = row['provenance']['source_revisions']
        _require(evidence['head_sha'] == row['revisions']['after'])
        _require(evidence['expected_base_sha'] is None or evidence['expected_base_sha'] == row['revisions']['before'])
        safe_path(row['workspace'], allow_root=True)
        changed = row['changes']['files']
        paths = [safe_path(file['path']) for file in changed]
        _require(paths == sorted(set(paths)))
        stats = row['changes']['stats']
        _require(stats['files_count'] == len(changed))
        _require(stats['additions'] == sum(file['additions'] for file in changed))
        _require(stats['deletions'] == sum(file['deletions'] for file in changed))
        _require(stats['changed_lines'] == stats['additions'] + stats['deletions'])
        for file in changed:
            for key in ('before_blob', 'after_blob'):
                _require(file[key] is None or _HEX40.fullmatch(file[key]) is not None)
            for key in ('before_mode', 'after_mode'):
                _require(file[key] is None or file[key] in ('100644', '100755'))
            status = file['status']
            _require((file['before_blob'] is None) == (status == 'added'))
            _require((file['after_blob'] is None) == (status == 'deleted'))
            _require((file['before_mode'] is None) == (status == 'added'))
            _require((file['after_mode'] is None) == (status == 'deleted'))
        task = row['task']
        _require(bool(task['task'].strip()) and all(value.strip() for value in task['subtasks']))
        expected = f'Task: {task['task']}'
        if task['subtasks']:
            expected += '\n\nSubtasks:\n' + '\n'.join('- ' + value.replace('\n', '\n  ') for value in task['subtasks'])
        _require(row['agent_task'] == expected)
        _require(
            row['provenance']['generation']['issue_ids'] == [item['id'] for item in row['provenance']['issue_refs']]
        )
        _require(('golden_diff' in row) == include and (not include or isinstance(row['golden_diff'], str)))


def _validate_population(data: dict) -> None:
    """Validate known omissions and ordered generation inputs without external receipts."""
    meta, rows = data['meta'], data['tasks']
    if meta['schema_version'] == 1:
        omitted = meta['omitted_failed_ids']
    else:
        omitted = [item['id'] for item in meta['omissions']]
        _validate_selection(meta, rows)
    identities = [row['id'] for row in rows] + omitted
    _require(len({value.casefold() for value in identities}) == len(identities))
    _require(meta['partial'] == bool(omitted))


def _validate_selection(meta: dict, rows: list[dict]) -> None:
    """Bind v2 coverage arithmetic, omissions and task provenance to selection."""
    coverage, selection, omissions = meta['coverage'], meta['selection'], meta['omissions']
    discovery = coverage['discovery']
    _require(discovery['selected'] <= discovery['matched'], 'meta.coverage')
    _require(discovery['capped'] == (discovery['selected'] < discovery['matched']), 'meta.coverage')
    for stage in ('collect', 'filter', 'generate'):
        counts = coverage[stage]
        _require(
            counts['input'] == sum(counts[key] for key in ('succeeded', 'failed', 'rejected', 'pending')),
            f'meta.coverage.{stage}',
        )
        _require(counts['pending'] == 0, f'meta.coverage.{stage}')
        _require(
            counts['failed'] == sum(item['stage'] == stage and item['kind'] == 'failed' for item in omissions),
            f'meta.coverage.{stage}',
        )
    _require(coverage['collect']['input'] == discovery['selected'], 'meta.coverage')
    _require(coverage['filter']['input'] == coverage['collect']['succeeded'], 'meta.coverage')
    _require(coverage['selected'] <= coverage['filter']['succeeded'], 'meta.coverage')
    _require(coverage['generate']['input'] == coverage['selected'], 'meta.coverage')
    _require(coverage['generate']['succeeded'] == len(rows), 'meta.coverage')
    _require(coverage['generate']['rejected'] == sum(item['kind'] == 'rejected' for item in omissions), 'meta.coverage')
    selected, keys = selection['selected_ids'], selection['generation_keys']
    _require(len(selected) == coverage['selected'], 'meta.coverage')
    _require(len({value.casefold() for value in selected}) == len(selected), 'meta.selection')
    _require(all(re.fullmatch(r'change:[0-9a-f]{64}', value) for value in selected), 'meta.selection')
    _require(set(selected) == set(keys), 'meta.selection')
    for value in (selection['sha256'], selection['input_sha256'], selection['fingerprint'], *keys.values()):
        _require(bool(_HEX64.fullmatch(value)), 'meta.selection')
    _require(
        selection['fingerprint'] == sha256({'tasks': [[identity, keys[identity]] for identity in selected]}),
        'meta.selection.fingerprint',
    )
    generated = set()
    for item in omissions:
        _require(bool(re.fullmatch(r'change:[0-9a-f]{64}', item['id'])), 'meta.omissions')
        _require(bool(_HEX64.fullmatch(item['result_sha256'])), 'meta.omissions')
        _require(item['kind'] != 'rejected' or item['stage'] == 'generate', 'meta.omissions')
        if item['stage'] == 'generate':
            _require(item['id'] in selected, 'meta.omissions')
            generated.add(item['id'])
        else:
            _require(item['id'] not in selected, 'meta.omissions')
    _require([row['id'] for row in rows] == [identity for identity in selected if identity not in generated], 'tasks')
    for row in rows:
        _require(
            row['provenance']['generation'].get('input_sha256') == keys[row['id']],
            'tasks.provenance.generation.input_sha256',
        )


def basket_summary(data: dict) -> dict:
    """Project only validated public counts; unavailable v1 coverage stays unknown."""
    meta = data['meta']
    if meta['schema_version'] == 1:
        failed, rejected, coverage = len(meta['omitted_failed_ids']), 0, None
    else:
        failed = sum(item['kind'] == 'failed' for item in meta['omissions'])
        rejected = sum(item['kind'] == 'rejected' for item in meta['omissions'])
        coverage = {
            stage: {key: counts[key] for key in ('input', 'succeeded', 'rejected', 'failed', 'pending')}
            for stage, counts in meta['coverage'].items()
            if stage in ('collect', 'filter', 'generate')
        }
    return {
        'schema_version': meta['schema_version'],
        'partial': meta['partial'],
        'total_tasks': len(data['tasks']),
        'failed_count': failed,
        'rejected_count': rejected,
        'coverage': coverage,
    }


def validate_basket_report(basket: dict, report: dict) -> None:
    """Check the same basket/report population links on import and stored reads."""
    validate_basket(basket)
    summary = basket_summary(basket)
    _require(report['basket']['partial'] == summary['partial'])
    _require(report['basket']['total_tasks'] == summary['total_tasks'])
    _require(report['basket']['omitted_failed_count'] == summary['failed_count'])
    _require([row['id'] for row in basket['tasks']] == [row['id'] for row in report['tasks']])
    for task, source in zip(report['tasks'], basket['tasks'], strict=True):
        _require(all(task[key] == source[key] for key in ('workspace', 'agent_task', 'source', 'diff_policy')))
        _require(
            all(task[key] == source['change_request'][key] for key in ('repository', 'number', 'url', 'title', 'kind'))
        )
        _require(all(task[key] == source['revisions'][key] for key in ('before', 'after', 'before_tree', 'after_tree')))
