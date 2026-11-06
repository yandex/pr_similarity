"""Raw source change counts preserve separate sides and incomplete measurements."""

import io
from types import SimpleNamespace
from typing import cast

from viewer.models.report import ReportTask
from viewer.presentation import queries as queries
from viewer.service import CatalogueEntry, ViewerService


def compute(files: list[SimpleNamespace], raw: dict[str, bytes], cap: int = 1000, status: str = 'available') -> dict:
    """Run the projection with controlled source bytes and display limits."""
    service = SimpleNamespace(
        config=SimpleNamespace(limits=SimpleNamespace(diff_bytes=cap, diff_lines=100)),
        store=SimpleNamespace(open=lambda key: io.BytesIO(raw[key.removeprefix('p/data/')])),
    )
    task = SimpleNamespace(scoring=SimpleNamespace(status=status, files=files))
    return queries.task_changes(
        cast(ViewerService, service), cast(CatalogueEntry, {'prefix': 'p/'}), cast(ReportTask, task)
    )


def file(base: str | None, human: str | None, ai: str | None, ch: bool = True, ca: bool = True) -> SimpleNamespace:
    """Provide the source references and change flags consumed by the counter."""
    return SimpleNamespace(base=base, human=human, ai=ai, changed_human=ch, changed_agent=ca)


def test_separate_sides_and_noop() -> None:
    """Count human changes separately from agent no-op output."""
    result = compute(
        [file('b', 'h', 'a'), file(None, 'empty', None, True, False)],
        {'b': b'a\nb\n', 'h': b'a\nc\nd\n', 'a': b'a\nb\n', 'empty': b''},
    )
    assert result['human'] == dict(status='complete', added=2, removed=1, files=2, measured_files=2)
    # File existence changes count even if an empty file contributes no lines.
    assert result['agent']['added'] == result['agent']['removed'] == 0
    result = compute([], {})
    assert all(v['status'] == 'complete' and v['files'] == 0 for v in result.values())


def test_limited_file_excluded_from_subtotal() -> None:
    """Exclude oversized files from the measured subtotal."""
    result = compute(
        [
            file(None, 'short', None, True, False),
            file(None, 'large', None, True, False),
        ],
        {'short': b'a\n', 'large': b'123456789'},
        cap=4,
    )
    assert result['human'] == dict(status='partial', added=1, removed=0, files=2, measured_files=1)
    assert result['agent']['status'] == 'complete'


def test_unavailable_is_not_zero() -> None:
    """Keep unmeasured changes unavailable while preserving known file counts."""
    result = compute([file(None, 'large', None, True, False)], {'large': b'123456789'}, cap=4)
    assert (
        result['human']['status'] == 'unavailable'
        and result['human']['added'] is None
        and result['human']['files'] == 1
    )
    result = compute([], {}, status='unavailable')
    assert result['human']['files'] is None


def test_counts_match_full_file_rows() -> None:
    """Match raw additions and removals to full-file display rows."""
    raw = {'b': b'old\nsame\n', 'h': b'new\nsame\nextra\n', 'a': b'same\n'}
    result = compute([file('b', 'h', 'a')], raw)
    for role, key in [('human', 'h'), ('agent', 'a')]:
        rows = queries.full_file_lines(
            raw['b'].decode().splitlines(True),
            raw[key].decode().splitlines(True),
            1000,
            100,
        )['lines']
        assert result[role]['added'] == sum(x['kind'] == 'added' for x in rows)
        assert result[role]['removed'] == sum(x['kind'] == 'removed' for x in rows)
