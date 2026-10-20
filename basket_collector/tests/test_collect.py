"""Queue freezing and minimal merged-PR snapshot tests."""

from collections.abc import Iterator
from typing import Any, cast

import pytest
from basket_collector.config import CollectConfig, SourceConfig
from basket_collector.contracts import CollectRecord, Issue, Repository
from basket_collector.github import GitHubClient, GitHubError
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.remote import Candidate, RemoteError
from basket_collector.stages.collect import (
    collect_change,
    discover_date_range,
    select_change_ids,
)


class FakeReader:
    """Provide paginated PR snapshots and record GitHub read calls."""

    def __init__(self, pages: list[list[dict[str, Any]]], pr: dict[str, Any] | None = None) -> None:
        """Store page and PR fixtures with an empty request log."""
        self.pages = pages
        self.change_request = pr or {}
        self.requests: list[int] = []

    def list_closed_prs(self, repository: str, *, start_page: int = 1) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        """Yield fixture pages starting from the requested page number."""
        for page in range(start_page, len(self.pages) + 1):
            self.requests.append(page)
            yield page, self.pages[page - 1]

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Return the configured PR and record its number."""
        self.requests.append(number)
        return self.change_request

    def list_merged_candidates(
        self,
        repository: Repository,
        *,
        start_page: int = 1,
        merged_from: str | None = None,
        merged_before: str | None = None,
    ) -> Iterator[tuple[int, list[Candidate]]]:
        """Normalize native discovery fixtures before the shared selection stage."""
        adapter = GitHubRemoteVCS(
            SourceConfig(provider='github', project=repository.path), client=cast(GitHubClient, self)
        )
        yield from adapter.list_merged_candidates(repository, start_page=start_page)

    def get_change_request(self, repository: Repository, number: int) -> CollectRecord:
        """Normalize raw native metadata through the real owning GitHub adapter."""
        adapter = GitHubRemoteVCS(
            SourceConfig(provider='github', project=repository.path), client=cast(GitHubClient, self)
        )
        return adapter.get_change_request(repository, number)

    def resolve_repository(self, locator: str) -> Repository:
        """Return the fixture project without making an API call."""
        return _repository()

    def get_commits(self, record: CollectRecord) -> list[str]:
        """Keep the protocol complete for these discovery-only test clients."""
        return [record.revisions.source.head_sha]

    def get_closing_issues(self, record: CollectRecord) -> list[Issue]:
        """Do not add issue operations to collection-stage tests."""
        return []

    def close(self) -> None:
        """Own no external resources."""


def _date_config(max_changes: int = 2) -> CollectConfig:
    """Build a half-open date-range collection configuration."""
    return CollectConfig(
        selection='date_range',
        merged_from='2026-01-01T00:00:00Z',
        merged_before='2026-02-01T00:00:00Z',
        max_changes=max_changes,
    )


def _repository(default_branch: str = 'develop') -> Repository:
    """Build a repository fixture with a known default branch."""
    return Repository(
        path='Owner/Repo',
        web_url='https://github.com/Owner/Repo',
        default_branch=default_branch,
        provider='github',
        server_url='https://github.com',
        project_id='123',
    )


def test_date_range_half_open_deduplicated_sorted_and_capped() -> None:
    """Deduplicate, sort, cap, and resume a half-open PR date range."""
    rows = [
        [
            {'number': 5, 'merged_at': '2026-01-10T00:00:00Z', 'base': {'ref': 'develop'}},
            {'number': 2, 'merged_at': '2026-01-01T00:00:00Z', 'base': {'ref': 'develop'}},
            {'number': 3, 'merged_at': None},
        ],
        [
            {'number': 5, 'merged_at': '2026-01-10T00:00:00Z', 'base': {'ref': 'develop'}},
            {'number': 4, 'merged_at': '2026-01-03T00:00:00Z', 'base': {'ref': 'develop'}},
            {'number': 6, 'merged_at': '2026-02-01T00:00:00Z', 'base': {'ref': 'develop'}},
        ],
    ]
    reader = FakeReader(rows)
    checkpoints: list[tuple[int, tuple[tuple[str, int], ...]]] = []
    result = discover_date_range(
        reader, _repository(), _date_config(), on_page=lambda page, matches: checkpoints.append((page, matches))
    )
    assert reader.requests == [1, 2]
    assert result.numbers == (2, 4)
    assert result.discovery.model_dump() == {'matched': 3, 'selected': 2, 'capped': True}
    assert checkpoints[-1][0] == 3
    resumed = discover_date_range(
        FakeReader(rows), _repository(), _date_config(), start_page=2, previous_matches=checkpoints[0][1]
    )
    assert resumed == result


def test_date_range_excludes_other_branches_before_sort_and_cap() -> None:
    """Exclude nondefault branches before sorting and applying the cap."""
    rows = [
        [
            {'number': 1, 'merged_at': '2026-01-02T00:00:00Z', 'base': {'ref': 'stable'}},
            {'number': 2, 'merged_at': '2026-01-03T00:00:00Z', 'base': {'ref': 'develop'}},
            {'number': 3, 'merged_at': '2026-01-04T00:00:00Z', 'base': {'ref': 'develop'}},
        ]
    ]
    config = _date_config(max_changes=1)
    filtered = discover_date_range(FakeReader(rows), _repository(), config)
    assert filtered.numbers == (2,)
    assert filtered.discovery.model_dump() == {'matched': 2, 'selected': 1, 'capped': True}
    all_branches = config.model_copy(update={'default_branch_only': False})
    assert discover_date_range(FakeReader(rows), _repository(), all_branches).numbers == (1,)


def test_pr_ids_preserve_yaml_order_and_config_rejects_duplicates_bool_empty() -> None:
    """Preserve explicit PR order and reject invalid PR ID lists."""
    config = CollectConfig(selection='change_ids', change_ids=[8, 2, 5])
    assert select_change_ids(config).numbers == (8, 2, 5)
    for ids in ([1, 1], [True], []):
        with pytest.raises(ValueError):
            CollectConfig(selection='change_ids', change_ids=cast(list[int], ids))


def test_collect_pr_keeps_deleted_author_and_fork_fields() -> None:
    """Retain nullable author and fork fields in a collected PR."""
    repository = _repository()
    pr = {
        'number': 12,
        'merged_at': '2026-01-02T03:04:05Z',
        'html_url': 'https://github.com/Owner/Repo/pull/12',
        'title': 'Change',
        'body': None,
        'user': None,
        'commits': 2,
        'merge_commit_sha': None,
        'base': {'sha': 'a' * 40, 'ref': 'develop', 'repo': {'full_name': 'owner/repo', 'id': 123}},
        'head': {'sha': 'b' * 40, 'repo': None},
    }
    reader = FakeReader([], pr)
    record = collect_change(reader, repository, 12, CollectConfig(selection='change_ids', change_ids=[12]))
    assert record.id == 'change:17ece1fcca71e94cb9d87c2ca551cbd01dfd8cb5bfa28ef437ee3180e3e860e6'
    assert record.change_request.body == '' and record.change_request.author is None
    assert record.revisions.source.head_repository is None
    assert record.revisions.source.merge_commit_sha is None
    assert reader.requests == [12]


def test_explicit_unmerged_pr_reports_record_failure() -> None:
    """Report an unmerged PR as a record failure for the stage retry policy."""
    repository = _repository()
    with pytest.raises(GitHubError, match='not merged') as exc:
        collect_change(
            FakeReader([], {'number': 1, 'merged_at': None}),
            repository,
            1,
            CollectConfig(selection='change_ids', change_ids=[1]),
        )
    assert exc.value.code == 'source_not_merged'


def test_explicit_other_branch_requires_override() -> None:
    """Require an override for a PR targeting another branch."""
    repository = _repository()
    raw = {
        'number': 9,
        'merged_at': '2026-01-03T00:00:00Z',
        'html_url': 'https://github.com/Owner/Repo/pull/9',
        'title': 'Backport',
        'body': '',
        'user': None,
        'commits': 1,
        'merge_commit_sha': None,
        'base': {'sha': 'a' * 40, 'ref': 'stable', 'repo': {'full_name': 'Owner/Repo', 'id': 123}},
        'head': {'sha': 'b' * 40, 'repo': None},
    }
    config = CollectConfig(selection='change_ids', change_ids=[9])
    with pytest.raises(RemoteError, match='another branch') as exc:
        collect_change(FakeReader([], raw), repository, 9, config)
    assert exc.value.code == 'non_default_branch'
    assert (
        collect_change(
            FakeReader([], raw), repository, 9, config.model_copy(update={'default_branch_only': False})
        ).change_request.number
        == 9
    )
