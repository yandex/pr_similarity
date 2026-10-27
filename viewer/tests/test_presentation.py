"""Global filter algebra, deterministic null-last sorting and diff bounds."""

import json
from dataclasses import replace
from pathlib import Path

from helpers import archive_fixture
from starlette.datastructures import QueryParams

from viewer.config import Config, Limits
from viewer.models import BasketV3
from viewer.models.report import ReportRun, ReportTask
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore
from viewer.presentation.queries import (
    Query,
    basket_metadata,
    file_diff,
    public_files,
    wall_time,
)
from viewer.service import ViewerService
from viewer.validation.archive import validate_archive


def row(
    name: str = 'agent', score: float | None = 0.5, date: str = '2026-10-05T10:00:00Z', user: str = 'alice'
) -> dict:
    """Build a pinned catalogue row with controlled identity, score and facets."""
    return {
        'display_name': name,
        'score': score,
        'created_at': date,
        'run_id': name,
        'user': user,
        'tags': ['setup:x'],
        'tag_groups': {'model': ['A', 'B'], 'effort': ['high'], 'setup': ['setup:x']},
        'basket_hash': 'hash',
        'pinned': True,
    }


def test_filter_or_inside_and_between_groups_and_exclusive_dates() -> None:
    """Verify filter or inside and between groups and exclusive dates."""
    q = Query(
        QueryParams(
            (
                'model=absent&model=B&effort=high&user=alice&q=SETUP:X&from=2026-1'
                '0-05T09:00:00Z&to=2026-10-05T10:00:01Z&pinned=1'
            )
        )
    )
    assert q.accepts(row())
    assert not q.accepts(row(user='bob'))
    assert not q.accepts(row(date='2026-10-05T10:00:01Z'))
    assert not Query(QueryParams('effort=low')).accepts(row())


def test_numeric_unknown_last_and_chronological_dates() -> None:
    """Verify numeric unknown last and chronological dates."""
    items = [row('unknown', None), row('low', 0.1), row('high', 0.9)]
    assert [r['run_id'] for r in Query(QueryParams('direction=asc')).sorted(items)] == ['low', 'high', 'unknown']
    assert [r['run_id'] for r in Query(QueryParams()).sorted(items)] == ['high', 'low', 'unknown']
    items = [row('fraction', date='2026-10-05T10:00:00.1Z'), row('whole', date='2026-10-05T10:00:00Z')]
    assert Query(QueryParams('sort=created_at&direction=asc')).sorted(items)[0]['run_id'] == 'whole'


def test_total_token_sort_uses_reported_total_and_keeps_nulls_last() -> None:
    """Verify total token sort uses reported total and keeps nulls last."""
    items = []
    for name, total, date in [
        ('unknown', None, '2026-10-05T10:00:00Z'),
        ('zero', 0, '2026-10-05T10:00:00Z'),
        ('small', 10, '2026-10-05T10:00:00Z'),
        ('late', 30, '2026-10-05T10:00:01Z'),
        ('b', 30, '2026-10-05T10:00:00Z'),
        ('a', 30, '2026-10-05T10:00:00Z'),
    ]:
        item = row(name, date=date)
        item['tokens'] = {'total': {'amount': total}, 'input': {'amount': 9999 if total is None else 1000 - total}}
        items.append(item)
    assert [r['run_id'] for r in Query(QueryParams('sort=total_tokens&direction=asc')).sorted(items)] == [
        'zero',
        'small',
        'a',
        'b',
        'late',
        'unknown',
    ]
    assert [r['run_id'] for r in Query(QueryParams('sort=total_tokens&direction=desc')).sorted(items)] == [
        'a',
        'b',
        'late',
        'small',
        'zero',
        'unknown',
    ]


def test_diff_byte_limit_is_explicit_and_file_id_not_a_path(tmp_path: Path) -> None:
    """Verify diff byte limit is explicit and file id not a path."""
    path = tmp_path / 'run.tar.gz'
    report, _ = archive_fixture(path)
    imported = validate_archive(path, tmp_path / 'data', Limits())
    limits = replace(Limits(), diff_bytes=32)
    config = Config(runtime=tmp_path / 'runtime', storage='mock', limits=limits)
    svc = ViewerService(config, MemoryStore(), Database(config.runtime))
    svc.publish(imported, 'alice', 'key')
    task = report['tasks'][0]
    file_id = public_files(ReportTask.model_validate(task))[0]['file_id']
    diff = file_diff(svc, report['run']['id'], task['id'], file_id)
    assert diff['human_diff']['truncated'] and diff['agent_diff']['truncated']
    assert len(diff['human_diff']['text'].encode()) <= 32
    assert file_id != task['scoring']['files'][0]['path']
    svc.db.close()


def test_basket_generation_projection_and_wall_time_unknown() -> None:
    """Verify basket generation projection and wall time unknown."""
    fixtures = Path(__file__).parent / 'fixtures'
    basket = BasketV3.model_validate(json.loads((fixtures / 'basket.json').read_text()))
    generation = next(entry for entry in basket.meta.lineage if entry.stage == 'generate')
    generation.settings = {'source': 'issue'}
    assert basket_metadata(basket)['task_source'] == {'source': 'issue', 'context': None}
    basket.meta.lineage = []
    assert basket_metadata(basket)['task_source'] == {'source': None, 'context': None}
    run = ReportRun.model_validate(json.loads((fixtures / 'completed.json').read_text())['run'])
    run.created_at = '2026-09-30T15:00:00Z'
    run.finished_at = None
    assert wall_time(run) is None
    run.created_at = '2026-09-30T15:02:00Z'
    run.finished_at = '2026-09-30T15:00:00Z'
    assert wall_time(run) is None
