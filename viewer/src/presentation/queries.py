"""Public projections, catalogue filtering/sorting and bounded human/agent diffs."""

import difflib
from datetime import datetime
from functools import cmp_to_key

from starlette.datastructures import QueryParams

from viewer.errors import ViewerError
from viewer.models.report import ReportTask
from viewer.presentation import run_projection
from viewer.presentation.metrics import DEFAULT_STATUSES, aggregate, displayed_amount, task_status
from viewer.service import CatalogueEntry, ViewerService
from viewer.util import digest, utc_timestamp
from viewer.validation.contracts import require

basket_metadata = run_projection.basket_metadata
wall_time = run_projection.wall_time


def _compare_values(left: str | float | datetime, right: str | float | datetime) -> int:
    """Compare homogeneous sort values and reject inconsistent internal rows."""
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    if isinstance(left, datetime) and isinstance(right, datetime):
        return (left > right) - (left < right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (left > right) - (left < right)
    raise TypeError('Sort values must have compatible types.')


class Query:
    """Validate catalogue filters, pagination and aggregate selection."""

    def __init__(self, params: QueryParams, *, task_sort: bool = False) -> None:
        """Initialize the resources and limits owned by this instance."""

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            """Validate one bounded integer while preserving missing-value semantics."""
            try:
                value = int(params.get(name, default))
            except (ValueError, TypeError):
                raise ViewerError('invalid_query', 'Invalid pagination.') from None
            require(minimum <= value <= maximum, 'invalid_query')
            return value

        self.offset = integer('offset', 0, 0, 1 << 31)
        self.limit = integer('limit', 50, 1, 200)
        statuses = params.get('statuses')
        self.statuses = list(DEFAULT_STATUSES) if statuses is None else [s for s in statuses.split(',') if s]
        self.pr = params.get('pr', 'LCpr')
        self.file = params.get('file', 'LCf')
        aggregate([], self.statuses, self.pr, self.file)
        self.selection = {'statuses': self.statuses, 'pr_aggregation': self.pr, 'file_aggregation': self.file}
        self.filters = {k: params.getlist(k) for k in ('user', 'model', 'effort', 'setup', 'basket')}
        self.search = params.get('q', '').casefold()
        self.pinned = params.get('pinned', '0')
        require(self.pinned in ('0', '1'), 'invalid_query')
        self.dates = {}
        for key in ('from', 'to'):
            value = params.get(key)
            if value:
                try:
                    parsed = datetime.fromisoformat(value)
                    offset = parsed.utcoffset()
                    require(offset is not None and offset.total_seconds() == 0, 'invalid_query')
                    self.dates[key] = parsed
                except ValueError:
                    raise ViewerError('invalid_query', 'Dates must be UTC timestamps.') from None
        self.sort = params.get('sort', 'task_order' if task_sort else 'score')
        self.direction = params.get('direction', 'desc')
        sorts = (
            ('task_order', 'score', 'total_tokens', 'time')
            if task_sort
            else (
                'score',
                'f1_files',
                'display_name',
                'created_at',
                'selected_tasks',
                'time',
                'cost',
                'total_tokens',
                'cache_read',
                'input',
                'output',
                'cache_write',
                'reasoning',
            )
        )
        require(self.sort in sorts and self.direction in ('asc', 'desc'), 'invalid_query')

    def page(self, items: list[dict]) -> dict:
        """Return the requested slice and pagination metadata."""
        return {
            'items': items[self.offset : self.offset + self.limit],
            'total': len(items),
            'offset': self.offset,
            'limit': self.limit,
        }

    def accepts(self, row: dict) -> bool:
        """Test whether a catalogue row matches the active filters."""
        if self.pinned == '1' and not row['pinned']:
            return False
        for key, values in self.filters.items():
            if not values:
                continue
            candidates = (
                [row['user']]
                if key == 'user'
                else [row['basket_hash']] if key == 'basket' else row['tag_groups'].get(key, [])
            )
            if not set(values) & set(candidates):
                return False
        if (
            self.search
            and self.search
            not in ' '.join(
                [
                    row['display_name'],
                    row['user'],
                    row['run_id'],
                    *row['tags'],
                    *row['tag_groups']['model'],
                    *row['tag_groups'].get('effort', []),
                ]
            ).casefold()
        ):
            return False
        created = datetime.fromisoformat(row['created_at'])
        if 'from' in self.dates and created < self.dates['from']:
            return False
        if 'to' in self.dates and created >= self.dates['to']:
            return False
        return True

    def sorted(self, items: list[dict]) -> list[dict]:
        """Order rows using the requested key and deterministic tie breakers."""

        def value(row: dict) -> str | float | datetime | None:
            """Extract the requested sort value while preserving unknown values."""
            if self.sort == 'time':
                return row['time']['median_seconds']
            if self.sort == 'cost':
                return displayed_amount(row['cost'])
            if self.sort == 'total_tokens':
                return displayed_amount(row['tokens']['total'])
            if self.sort in ('cache_read', 'input', 'output', 'cache_write', 'reasoning'):
                return displayed_amount(row['tokens'][self.sort])
            if self.sort == 'created_at':
                return utc_timestamp(row['created_at'])
            selected = row[self.sort]
            return selected.casefold() if isinstance(selected, str) else selected

        def compare(a: dict, b: dict) -> int:
            """Compare sort values with unknown values ordered consistently."""
            av, bv = value(a), value(b)
            if av is None or bv is None:
                primary = (av is None) - (bv is None)
            else:
                primary = _compare_values(av, bv) * (1 if self.direction == 'asc' else -1)
            if primary:
                return primary
            at, bt = (utc_timestamp(a['created_at']), a['run_id']), (utc_timestamp(b['created_at']), b['run_id'])
            return (at > bt) - (at < bt)

        return sorted(items, key=cmp_to_key(compare))


class TaskQuery(Query):
    """Apply task-specific sorting with stable original ordering."""

    def __init__(self, params: QueryParams) -> None:
        """Initialize the resources and limits owned by this instance."""
        super().__init__(params, task_sort=True)
        if self.sort == 'task_order':
            self.direction = 'asc'

    def sorted(self, items: list[dict]) -> list[dict]:
        """Order rows using the requested key and deterministic tie breakers."""
        if self.sort == 'task_order':
            return list(items)

        def value(row: dict) -> float | None:
            """Extract the requested sort value while preserving unknown values."""
            if self.sort == 'total_tokens':
                return displayed_amount(row['tokens']['total'])
            if self.sort == 'time':
                return row['time']['total_seconds']
            return row['score']

        def compare(a: tuple[int, dict], b: tuple[int, dict]) -> int:
            """Compare sort values with unknown values ordered consistently."""
            av, bv = value(a[1]), value(b[1])
            if av is None or bv is None:
                primary = (av is None) - (bv is None)
            else:
                primary = ((av > bv) - (av < bv)) * (1 if self.direction == 'asc' else -1)
            if primary:
                return primary
            ak, bk = (a[0], a[1]['task_id']), (b[0], b[1]['task_id'])
            return (ak > bk) - (ak < bk)

        return [row for _, row in sorted(enumerate(items), key=cmp_to_key(compare))]


def catalogue(service: ViewerService, query: Query) -> dict:
    """Build the public filtered basket catalogue."""
    order = [h for h in service.order if not service.display[h]['hidden']]
    visible = set(order)
    all_rows = [
        service.run(rid, **query.selection)
        for rid, row in service.runs.items()
        if row['marker']['basket_sha256'] in visible
    ]
    selected = [row for row in all_rows if query.accepts(row)]
    items = []
    for basket_hash in order:
        rows = [r for r in selected if r['basket_hash'] == basket_hash]
        if not rows:
            continue
        source = next(v['basket']['meta'] for v in service.runs.values() if v['marker']['basket_sha256'] == basket_hash)
        items.append(
            {
                'hash': basket_hash,
                'name': source['name'],
                'description': source['description'],
                **service.display[basket_hash],
                'run_count': len(rows),
                'total_run_count': sum(r['basket_hash'] == basket_hash for r in all_rows),
            }
        )
    options = {
        'user': sorted({r['user'] for r in all_rows}),
        'basket': [
            {
                'hash': h,
                'name': next(
                    v['basket']['meta']['name'] for v in service.runs.values() if v['marker']['basket_sha256'] == h
                ),
            }
            for h in order
        ],
    }
    for group in ('model', 'effort', 'setup'):
        options[group] = sorted({t for r in all_rows for t in r['tag_groups'].get(group, [])})
    return {
        **query.page(items),
        'filter_options': options,
        'shown_runs': len(selected),
        'total_runs': len(all_rows),
        'selection': query.selection,
    }


def basket_runs(service: ViewerService, basket_hash: str, query: Query) -> dict:
    """Build the paginated run list for one basket."""
    if basket_hash not in service.order:
        raise ViewerError('not_found', 'Basket not found.', 404)
    rows = [
        service.run(rid, **query.selection)
        for rid, v in service.runs.items()
        if v['marker']['basket_sha256'] == basket_hash
    ]
    return {**query.page(query.sorted([r for r in rows if query.accepts(r)])), 'selection': query.selection}


def find_task(service: ViewerService, rid: str, task_id: str) -> tuple[CatalogueEntry, ReportTask]:
    """Resolve a task within its selected stored report."""
    row = service._require_run(rid)
    task = next((t for t in service.full_report(rid).tasks if t.id == task_id), None)
    if task is None:
        raise ViewerError('not_found', 'Task not found.', 404)
    return row, task


def public_task(task: ReportTask, file_aggregation: str) -> dict:
    """Project final task evidence and the requested file aggregation."""
    status = task_status(task)
    metrics = aggregate([task], [status], 'Mpr', file_aggregation)
    return {
        'task_id': task.id,
        'kind': task.kind,
        'repository': task.repository,
        'number': task.number,
        'url': task.url,
        'title': task.title,
        'status': status,
        'scoring_status': task.scoring.status,
        'score': (
            (task.scoring.Mf_mlp if file_aggregation == 'Mf' else task.scoring.LCf_mlp)
            if task.scoring.status == 'available'
            else None
        ),
        'f1_files': metrics['f1_files'],
        'cost': metrics['cost'],
        'tokens': metrics['tokens'],
        'time': metrics['time'],
    }


def public_files(task: ReportTask) -> list[dict]:
    """Project the task file summaries without source contents."""
    return [
        {
            'file_id': digest(f.path.encode()),
            'path': f.path,
            'score': f.mlp,
            'changed_human': f.changed_human,
            'changed_agent': f.changed_agent,
            'exists': f.exists.to_wire(),
        }
        for f in task.scoring.files
    ]


def file_diff(service: ViewerService, rid: str, task_id: str, file_id: str) -> dict:
    """Build the requested source-role diff from verified stored artifacts."""
    run, task = find_task(service, rid, task_id)
    row = next((f for f in task.scoring.files if digest(f.path.encode()) == file_id), None)
    if row is None:
        raise ViewerError('not_found', 'File not found.', 404)
    cap = service.config.limits.diff_bytes
    sources = {}
    truncated = False
    for role in ('base', 'human', 'ai'):
        if getattr(row, role) is None:
            sources[role] = []
            continue
        with service.store.open(run['prefix'] + 'data/' + getattr(row, role)) as stream:
            data = stream.read(cap + 1)
        truncated |= len(data) > cap
        text = data[:cap].decode('utf-8', errors='replace')
        lines = text.splitlines(keepends=True)
        if len(lines) > service.config.limits.diff_lines:
            lines = lines[: service.config.limits.diff_lines]
            truncated = True
        sources[role] = lines

    def side(role: str) -> dict:
        """Read one source role using its recorded existence and digest."""
        chunks = []
        length = 0
        cut = truncated
        for line in difflib.unified_diff(
            sources['base'], sources[role], fromfile='base/' + row.path, tofile=role + '/' + row.path
        ):
            if not line.endswith('\n'):
                line += '\n\\ No newline at end of file\n'
            data = line.encode()
            room = cap - length
            if len(data) > room:
                chunks.append(data[:room].decode(errors='ignore'))
                cut = True
                break
            chunks.append(line)
            length += len(data)
        if not chunks and row.exists.base != getattr(row.exists, role):
            chunks = ['[File ' + ('added' if getattr(row.exists, role) else 'deleted') + '; empty content]\n']
        content = ''.join(chunks).encode()
        if len(content) > cap:
            content = content[:cap]
            cut = True
        return {'text': content.decode(errors='ignore'), 'truncated': cut}

    return {
        'file_id': file_id,
        'path': row.path,
        'score': row.mlp,
        'exists': row.exists.to_wire(),
        'human_diff': side('human'),
        'agent_diff': side('ai'),
    }
