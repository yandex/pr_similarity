"""Tests for local and population-level PR filters."""

import hashlib

import pytest
from fixture_helpers import descriptor, retrieval, task_identity

from basket_collector.config import (
    FilesConfig,
    FilterConfig,
    PercentileConfig,
    ScopeConfig,
    SelectionConfig,
    SizeConfig,
)
from basket_collector.filters import apply_population, check_local
from basket_collector.models.artifacts import (
    ChangedFile,
    ChangeRequest,
    Changes,
    ChangeStats,
    CommitMessage,
    FilterRecord,
    FilterRevisions,
    ResolvedRevisions,
    SourceRevisions,
)

SHA = 'a' * 40
OTHER = 'b' * 40


def changes(paths: list[str], *, lines: int = 1, chars: int = 1) -> Changes:
    """Build a valid change fixture for the requested paths and size."""
    files = [
        ChangedFile(
            path=path,
            status='added',
            additions=lines if index == 0 else 0,
            deletions=0,
            before_blob=None,
            after_blob=SHA,
            before_mode=None,
            after_mode='100644',
        )
        for index, path in enumerate(paths)
    ]
    return Changes(
        files=files,
        stats=ChangeStats(files_count=len(files), additions=lines, deletions=0, changed_lines=lines, patch_chars=chars),
        diff='x' * chars,
        commit_messages=[CommitMessage(sha=SHA, message='change')],
    )


def record(number: int, metric: int) -> FilterRecord:
    """Build a filtered PR fixture with a chosen size metric."""
    source = SourceRevisions(
        base_sha=OTHER,
        head_sha=SHA,
        merge_commit_sha=None,
        head_repository=None,
        commits_count=1,
        expected_base_sha=None,
        squash_commit_sha=None,
        diff_version_id=None,
        target_branch='develop',
    )
    resolved = ResolvedRevisions(
        policy='change_merge_base_v1',
        before=OTHER,
        after=SHA,
        commits=[SHA],
        before_tree='dddddddddddddddddddddddddddddddddddddddd',
        after_tree='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
    )
    return FilterRecord(
        id=task_identity(number),
        change_request=ChangeRequest(
            repository='owner/repo',
            number=number,
            url=f'https://example.invalid/{number}',
            title='title',
            body='private detail',
            author=None,
            merged_at='2026-01-01T00:00:00Z',
            kind='pull_request',
        ),
        revisions=FilterRevisions(source=source, resolved=resolved),
        changes=changes([f'src/{number}.py'], lines=metric),
        issues=[],
        source=descriptor('owner/repo', number),
        retrieval=retrieval(descriptor('owner/repo', number), resolved.before, resolved.after),
    )


def rejection_code(value: Changes, config: FilterConfig) -> str:
    """Return the local rejection code and require a rejected value."""
    reason = check_local(value, config)
    assert reason is not None
    return reason.code


@pytest.mark.parametrize(
    'field,value,code',
    [
        ('min_chars', 2, 'min_chars'),
        ('max_chars', 0, 'max_chars'),
        ('min_lines', 2, 'min_lines'),
        ('max_lines', 0, 'max_lines'),
        ('min_files', 2, 'min_files'),
        ('max_files', 0, 'max_files'),
    ],
)
def test_each_size_bound(field: str, value: int, code: str) -> None:
    """Return the matching rejection reason for every size bound."""
    config = FilterConfig(
        size=SizeConfig.model_validate(
            {
                'min_chars': None,
                'max_chars': None,
                'min_lines': None,
                'max_lines': None,
                'min_files': None,
                'max_files': None,
                field: value,
            }
        )
    )
    assert rejection_code(changes(['src/main.py']), config) == code


def test_scope_path_precedence_and_source_fraction() -> None:
    """Check scope and path-rule precedence plus source proportion bounds."""
    value = changes(['src/a.py', 'src/readme.md'])
    config = FilterConfig(scope=ScopeConfig(enabled=True, prefix='src', subfolders_affected=1))
    assert rejection_code(value, config) == 'scope_root_file'
    config = FilterConfig(
        files=FilesConfig.model_validate(
            {
                'enabled': True,
                'forbidden_path_patterns': [r'a\.py$'],
                'allowed_path_patterns': [r'a\.py$'],
                'required_path_patterns': [r'a\.py$'],
                'min_source_proportion': 0.5,
            }
        )
    )
    assert rejection_code(value, config) == 'forbidden_path'
    config = FilterConfig(
        files=FilesConfig.model_validate(
            {
                'enabled': True,
                'allowed_path_patterns': [r'src/'],
                'required_path_patterns': [r'missing'],
                'min_source_proportion': 0.5,
            }
        )
    )
    assert rejection_code(value, config) == 'required_path_missing'
    config = FilterConfig(files=FilesConfig(enabled=True, min_source_proportion=0.5, max_source_proportion=0.5))
    assert check_local(value, config) is None
    config = FilterConfig(files=FilesConfig(enabled=True, min_source_proportion=0.51))
    assert rejection_code(value, config) == 'min_source_proportion'


def test_percentile_then_selection_is_deterministic() -> None:
    """Apply percentile filtering before deterministic seeded selection."""
    original = [record(number, metric) for number, metric in [(1, 2), (2, 1), (3, 3), (4, 3), (5, 4)]]
    config = FilterConfig(
        percentile=PercentileConfig(enabled=True, lower_fraction=0.2, upper_fraction=0.2),
        selection=SelectionConfig(shuffle=True, seed=7, limit=2),
    )
    selected, rejected = apply_population(original, config)
    middle = original[0:1] + original[2:4]
    expected = sorted(middle, key=lambda item: (hashlib.sha256(f'7\n{item.id}'.encode()).hexdigest(), item.id))[:2]
    assert [item.id for item in selected] == [item.id for item in expected]
    assert {item.reasons[0].code for item in rejected} == {'percentile_lower', 'percentile_upper', 'selection_limit'}
    assert len(selected) + len(rejected) == len(original)
    assert 'changes' not in rejected[0].model_dump()


def test_percentile_tie_breaks_by_id() -> None:
    """Break equal percentile metric values by PR identity."""
    original = [record(2, 1), record(1, 1), record(3, 2)]
    config = FilterConfig(
        percentile=PercentileConfig(enabled=True, lower_fraction=0.34, upper_fraction=0.0),
        selection=SelectionConfig(shuffle=False, limit=None),
    )
    selected, rejected = apply_population(original, config)
    assert [item.id for item in rejected] == ['change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea']
    assert [item.id for item in selected] == [
        'change:b76ae00445934fd9558d883e581fac684789154c92d1e1e58a8e6c068b26138f',
        'change:1cf4ae9a9645565e59e930eb85358da6fd5b9682dbdbb4b346fcd1ba31ab1200',
    ]
