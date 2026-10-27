"""Tests for filtering a collected PR into a verified record."""

from pathlib import Path
from threading import Event
from typing import cast

import pytest
from fixture_helpers import descriptor, repository, retrieval

from basket_collector.config import FilterConfig
from basket_collector.models.artifacts import (
    ChangedFile,
    ChangeRequest,
    Changes,
    ChangeStats,
    CollectRecord,
    CommitMessage,
    FilterRecord,
    Issue,
    RejectedRecord,
    Repository,
    ResolvedRevisions,
    Revisions,
    SourceRevisions,
)
from basket_collector.models.sources import RetrievalReceipt, SourceDescriptor
from basket_collector.providers.remote import RemoteVCS
from basket_collector.stages.filter import (
    FilterInterrupted,
    SourceChangedError,
    _verify_source,
    process_one,
)
from basket_collector.vcs.git import GitRepository

BASE = 'a' * 40
HEAD = 'b' * 40


def collected() -> CollectRecord:
    """Build a collected PR fixture with saved source revisions."""
    return CollectRecord(
        id='change:f51d71c499237215fd94cbb75863ba420c97976b29660d04d236d789b61b9ec8',
        change_request=ChangeRequest(
            repository='owner/repo',
            number=7,
            url='https://example.invalid/7',
            title='Title',
            body='body',
            author='author',
            merged_at='2026-01-01T00:00:00Z',
            kind='pull_request',
        ),
        revisions=Revisions(
            source=SourceRevisions(
                base_sha=BASE,
                head_sha=HEAD,
                merge_commit_sha=None,
                head_repository=None,
                commits_count=1,
                expected_base_sha=None,
                squash_commit_sha=None,
                diff_version_id=None,
                target_branch='develop',
            )
        ),
        source=descriptor('owner/repo', 7),
    )


class GitHub:
    """Simulate GitHub PR, commit, and closing-issue reads."""

    def __init__(self) -> None:
        """Initialize the call log and current PR head."""
        self.calls: list[str] = []
        self.live_head = HEAD

    def get_change_request(self, repository: Repository, number: int) -> CollectRecord:
        """Return live source revisions and record the PR read."""
        self.calls.append('change_request')
        record = collected()
        return record.model_copy(
            update={
                'revisions': record.revisions.model_copy(
                    update={'source': record.revisions.source.model_copy(update={'head_sha': self.live_head})}
                )
            }
        )

    def get_commits(self, record: CollectRecord) -> list[str]:
        """Return saved PR commits and record the commit read."""
        self.calls.append('commits')
        return [HEAD]

    def get_closing_issues(self, record: CollectRecord) -> list[Issue]:
        """Return a linked issue and record the issue read."""
        self.calls.append('issues')
        return [
            Issue(
                id='issue:66f551d3693c45049b15f8d979be4e41d339f2627172b9f5458df1831abb67c4',
                repository='owner/repo',
                number=1,
                url='https://example.invalid/issues/1',
                title='Issue',
                body='',
            )
        ]


class Git:
    """Simulate Git fetch, revision resolution, and diff reads."""

    def __init__(self) -> None:
        """Initialize the Git call log."""
        self.calls: list[str] = []

    def fetch_commit(self, sha: str, fetch_refs: list[str] | None = None) -> None:
        """Check the expected fallback ref and record the fetch."""
        self.calls.append('fetch_head' if sha == HEAD else 'fetch_base')
        assert sha in (BASE, HEAD)
        assert fetch_refs == collected().source.fetch_refs

    def resolve_revisions(
        self, source: SourceRevisions, commits: list[str], fetch_refs: list[str] | None = None
    ) -> ResolvedRevisions:
        """Return resolved revisions for the expected commit sequence."""
        self.calls.append('resolve')
        assert commits == [HEAD]
        return ResolvedRevisions(
            policy='change_merge_base_v1',
            before=BASE,
            after=HEAD,
            commits=[HEAD],
            before_tree='dddddddddddddddddddddddddddddddddddddddd',
            after_tree='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
        )

    def prepare(self) -> None:
        """Own no native cache for this filter-order fixture."""

    def verify_retrieval(self, source: SourceDescriptor, resolved: ResolvedRevisions) -> RetrievalReceipt:
        """Record proof only after content and issues passed filtering."""
        self.calls.append('proof')
        return retrieval(source, resolved.before, resolved.after, resolved.before_tree, resolved.after_tree)

    def read_changes(self, resolved: ResolvedRevisions) -> Changes:
        """Return a one-file change and record the diff read."""
        self.calls.append('diff')
        return Changes(
            files=[
                ChangedFile(
                    path='src/main.py',
                    status='added',
                    additions=1,
                    deletions=0,
                    before_blob=None,
                    after_blob=HEAD,
                    before_mode=None,
                    after_mode='100644',
                )
            ],
            stats=ChangeStats(files_count=1, additions=1, deletions=0, changed_lines=1, patch_chars=1),
            diff='x',
            commit_messages=[CommitMessage(sha=HEAD, message='change')],
        )


def test_process_one_enriches_and_reads_issues_last() -> None:
    """Resolve revisions and changes before querying linked issues."""
    github = GitHub()
    git = Git()
    result = process_one(
        collected(), cast(GitRepository, git), cast(RemoteVCS, github), FilterConfig(), hosting_repository=repository()
    )
    assert isinstance(result, FilterRecord)
    assert result.revisions.resolved.after == HEAD
    assert [item.id for item in result.issues] == [
        'issue:66f551d3693c45049b15f8d979be4e41d339f2627172b9f5458df1831abb67c4'
    ]
    assert github.calls == ['change_request', 'commits', 'issues']
    assert git.calls == ['fetch_head', 'fetch_base', 'resolve', 'diff', 'proof']


def test_source_change_is_stage_error_before_git() -> None:
    """Stop before Git work when the live source head changed."""
    github = GitHub()
    github.live_head = 'c' * 40
    git = Git()
    with pytest.raises(SourceChangedError, match='source_changed'):
        process_one(
            collected(),
            cast(GitRepository, git),
            cast(RemoteVCS, github),
            FilterConfig(),
            hosting_repository=repository(),
        )
    assert github.calls == ['change_request']
    assert git.calls == []


def test_native_diff_version_is_part_of_frozen_source_identity() -> None:
    """Reject an absent or changed native diff version without changing the frozen source."""
    frozen = collected()
    enriched = frozen.model_copy(
        update={
            'revisions': frozen.revisions.model_copy(
                update={'source': frozen.revisions.source.model_copy(update={'diff_version_id': '1'})}
            )
        }
    )
    with pytest.raises(SourceChangedError):
        _verify_source(frozen, enriched)
    changed = enriched.model_copy(
        update={
            'revisions': enriched.revisions.model_copy(
                update={'source': enriched.revisions.source.model_copy(update={'diff_version_id': '2'})}
            )
        }
    )
    with pytest.raises(SourceChangedError):
        _verify_source(enriched, changed)


def test_stop_between_diff_and_issue_leaves_pr_pending() -> None:
    """Interrupt after diff reading and before issue lookup."""
    stop = Event()
    github = GitHub()
    git = Git()
    original = git.read_changes

    def stopping_diff(resolved: ResolvedRevisions) -> Changes:
        """Set the stop signal after returning the fixture diff."""
        result = original(resolved)
        stop.set()
        return result

    git.read_changes = stopping_diff
    with pytest.raises(FilterInterrupted):
        process_one(
            collected(),
            cast(GitRepository, git),
            cast(RemoteVCS, github),
            FilterConfig(),
            stop,
            hosting_repository=repository(),
        )
    assert github.calls == ['change_request', 'commits']


def test_stop_between_head_and_base_fetch_leaves_pr_pending() -> None:
    """Interrupt after head fetch and before base fetch."""
    stop = Event()
    github = GitHub()
    git = Git()
    original = git.fetch_commit

    def stopping_fetch(sha: str, fetch_refs: list[str] | None = None) -> None:
        """Set the stop signal after the first fixture fetch."""
        original(sha, fetch_refs)
        stop.set()

    git.fetch_commit = stopping_fetch
    with pytest.raises(FilterInterrupted):
        process_one(
            collected(),
            cast(GitRepository, git),
            cast(RemoteVCS, github),
            FilterConfig(),
            stop,
            hosting_repository=repository(),
        )
    assert github.calls == ['change_request', 'commits']
    assert git.calls == ['fetch_head']


def test_missing_issue_rejection_is_compact() -> None:
    """Reject a PR without issues without persisting its change details."""
    github = GitHub()
    github.get_closing_issues = lambda record: []
    result = process_one(
        collected(),
        cast(GitRepository, Git()),
        cast(RemoteVCS, github),
        FilterConfig(),
        hosting_repository=repository(),
    )
    assert isinstance(result, RejectedRecord)
    assert result.reasons[0].code == 'missing_issue'
    assert 'changes' not in result.model_dump()


@pytest.mark.parametrize(
    'boundary,expected_git,expected_api',
    [
        ('fetch_commit', [], ['change_request', 'commits']),
        ('resolve_revisions', ['fetch_head', 'fetch_base'], ['change_request', 'commits']),
        ('verify_retrieval', ['fetch_head', 'fetch_base', 'resolve', 'diff'], ['change_request', 'commits', 'issues']),
    ],
)
def test_unshallow_failure_rejects_only_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_git: list[str],
    expected_api: list[str],
) -> None:
    """Stop one record at fetch, deepen or cold proof without accepting incomplete evidence."""
    github = GitHub()
    git = Git()
    classifier = GitRepository(tmp_path / 'cache', 'https://example.invalid/repo.git', 10)

    def failure(*args: object, **kwargs: object) -> None:
        """Feed the captured native stderr through the actual Git classifier."""
        classifier._raise_command_error(
            ('fetch', '--depth=2', classifier.clone_url, BASE),
            f'fatal: object not found: unshallow {BASE}\n'.encode(),
            True,
        )

    monkeypatch.setattr(git, boundary, failure)
    stop = Event()
    result = process_one(
        collected(),
        cast(GitRepository, git),
        cast(RemoteVCS, github),
        FilterConfig(),
        stop,
        hosting_repository=repository(),
    )
    assert isinstance(result, RejectedRecord)
    assert result.id == collected().id
    assert result.reasons[0].code == 'source_history_unavailable'
    assert 'changes' not in result.model_dump() and 'retrieval' not in result.model_dump()
    assert github.calls == expected_api
    assert git.calls == expected_git
    assert stop.is_set() is False
