"""Full source views preserve context, file existence and display budgets."""

from pathlib import Path

import pytest
from test_absent_role_diff import empty_role_archive
from viewer.config import Config
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore
from viewer.presentation import queries as queries
from viewer.service import ViewerService
from viewer.util import digest


def test_full_context_numbers_and_literal_code() -> None:
    """Retain context numbering and literal HTML in added source rows."""
    before = [f'line {i}\n' for i in range(30)]
    after = before.copy()
    after[15] = '<script>not executed</script>\n'
    result = queries.full_file_lines(before, after, 10000, 100)
    assert not result['truncated'] and len(result['lines']) == 31
    assert result['lines'][0] == dict(kind='context', text='line 0', base_line=1, result_line=1)
    assert result['lines'][-1]['text'] == 'line 29'
    assert result['lines'][15] == dict(kind='removed', text='line 15', base_line=16, result_line=None)
    assert result['lines'][16] == dict(
        kind='added',
        text='<script>not executed</script>',
        base_line=None,
        result_line=16,
    )


def test_unchanged_empty_and_no_final_newline() -> None:
    """Handle unchanged files, empty files and unterminated final lines."""
    assert queries.full_file_lines([], [], 100, 10) == {'lines': [], 'truncated': False}
    result = queries.full_file_lines(['a\n', 'b'], ['a\n', 'b'], 100, 10)
    assert [x['text'] for x in result['lines']] == ['a', 'b']
    assert all(x['kind'] == 'context' for x in result['lines'])


@pytest.mark.parametrize('before,after,kind', [([], ['x\n'], 'added'), (['x\n'], [], 'removed')])
def test_whole_add_delete(before: list[str], after: list[str], kind: str) -> None:
    """Identify rows from whole-file additions and removals."""
    assert queries.full_file_lines(before, after, 100, 10)['lines'][0]['kind'] == kind


def test_display_limits() -> None:
    """Apply byte and line limits without displaying partial rows."""
    assert queries.full_file_lines([], ['é\n'], 2, 10) == {'lines': [], 'truncated': True}
    result = queries.full_file_lines(['a\n', 'b\n'], ['a\n', 'b\n'], 100, 1)
    assert result['truncated'] and len(result['lines']) == 1


@pytest.mark.parametrize(
    'exists,state',
    [
        ((False, True, True), 'added'),
        ((True, False, False), 'deleted'),
        ((False, False, True), 'absent'),
    ],
)
def test_real_archive_empty_roles(tmp_path: Path, exists: tuple[bool, bool, bool], state: str) -> None:
    """Keep empty source-role existence visible after archive publication."""
    imported = empty_role_archive(tmp_path / 'empty.tar.gz', exists)
    config = Config(runtime=tmp_path / 'runtime', storage='mock')
    svc = ViewerService(config, MemoryStore(), Database(config.runtime))
    svc.publish(imported, 'alice', 'empty')
    task = imported.report.tasks[0]
    file = task.scoring.files[0]
    result = queries.file_diff(svc, imported.report.run.id, task.id, digest(file.path.encode()), full=True)
    assert (
        result['human_file']['state'] == state
        and result['human_file']['lines'] == []
        and not result['human_file']['truncated']
    )
    svc.db.close()
