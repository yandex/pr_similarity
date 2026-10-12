"""Glob boundaries and explicit workspace/repository scoping."""

import pytest
from benchmark.excludes import compile_exclude, resolve_exclude


@pytest.mark.parametrize(
    'rule,workspace,matching,not_matching',
    [
        ('.opencode/', 'src', 'src/.opencode/skills/a', 'src/.opencodex/a'),
        ('**/cache/', '.', 'cache/file', 'cachex/file'),
        ('**/cache/', '.', 'a/b/cache/file', 'a/b/cachex/file'),
        ('<repo_root>/tools/*.json', 'src', 'tools/config.json', 'src/tools/config.json'),
        ('<workspace>/tmp?.py', 'src', 'src/tmp1.py', 'src/tmp11.py'),
        ('plain.txt', '.', 'plain.txt', 'a/plain.txt'),
    ],
)
def test_scoped_glob(rule: str, workspace: str, matching: str, not_matching: str) -> None:
    """Verify scoped glob."""
    matcher = compile_exclude(resolve_exclude(rule, workspace))
    assert matcher.match(matching)
    assert not matcher.match(not_matching)
