"""Validate collector exports independently and hash the exact frozen input."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError
from benchmark.git import safe_path
from benchmark.sources import (
    SourceDescriptor,
    canonical_server,
    https_url,
    task_identity,
)
from benchmark.storage import bytes_sha256, parse_json, sha256

_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class Basket:
    """One nonempty whole basket; the original bytes travel with the run."""

    data: dict
    content: bytes
    sha256: str

    @property
    def rows(self) -> list[dict]:
        """Return the complete exported task queue."""
        return self.data['tasks']


def _require(condition: bool) -> None:
    """Reject a failed frozen-basket invariant with a safe message."""
    if not condition:
        raise BenchmarkError(
            'invalid_basket', 'Basket metadata and task records are inconsistent; re-export the basket.'
        )


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


def load_basket(path: Path) -> Basket:
    """Accept exported v1 tasks, never a partial collector checkpoint."""
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(
            'basket_unreadable', 'Cannot read the frozen basket; check its path and permissions.'
        ) from exc
    data = parse_json(content)
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
    identities = [row['id'] for row in rows] + meta['omitted_failed_ids']
    _require(len({value.casefold() for value in identities}) == len(identities))
    _require(meta['partial'] == bool(meta['omitted_failed_ids']))
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
    return Basket(data, content, bytes_sha256(content))
