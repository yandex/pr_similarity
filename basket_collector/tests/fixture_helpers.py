"""Construct explicit provider-neutral fixtures owned by the collector tests."""

from pathlib import Path

import pytest
from basket_collector.config import GitHubConfig, SourceConfig
from basket_collector.git import GitRepository
from basket_collector.sources import (
    Repository,
    RetrievalReceipt,
    SourceDescriptor,
    change_id,
    issue_id,
    source_hash,
)


def repository(path: str = 'owner/repo', branch: str = 'develop') -> Repository:
    """Supply complete immutable project identity for hosting-independent stage tests."""
    return Repository(
        provider='github',
        server_url='https://github.com',
        project_id='123',
        path=path,
        web_url='https://github.com/' + path,
        default_branch=branch,
    )


def native_config(source: SourceConfig) -> GitHubConfig:
    """Apply the production native-client parameter projection to HTTP fixtures."""
    return GitHubConfig.model_validate(source.model_dump(exclude={'provider', 'project', 'server_url'}))


def local_git(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
    """Deliver native warm and cold packs locally, refusing an accidental network source."""

    def fetch(cache: GitRepository, reference: str, depth: int) -> None:
        """Use only the fixture objects while retaining native cache/deepening behavior."""
        assert cache.clone_url in (str(upstream), 'https://github.com/owner/repo.git')
        cache._run('fetch', '--no-tags', '--depth=' + str(depth), str(upstream), reference)

    monkeypatch.setattr(GitRepository, '_fetch', fetch)


def task_identity(number: int, project_id: str = '123') -> str:
    """Use immutable project identity rather than a repository display path."""
    return change_id('github', 'https://github.com', project_id, 'pull_request', number)


def issue_identity(number: int, project_id: str = '123') -> str:
    """Keep issue and change namespaces distinct in linked-issue fixtures."""
    return issue_id('github', 'https://github.com', project_id, number)


def descriptor(repository: str, number: int, branch: str = 'develop') -> SourceDescriptor:
    """Describe the declared source used by mocked/native fixture readers."""
    return SourceDescriptor(
        vcs='git',
        fetch_url=f'https://github.com/{repository}.git',
        fetch_refs=[f'refs/pull/{number}/head', 'refs/heads/' + branch],
    )


def retrieval(
    source: SourceDescriptor, before: str, after: str, before_tree: str = 'd' * 40, after_tree: str = 'e' * 40
) -> RetrievalReceipt:
    """Bind synthetic proof to the exact fixture descriptor and frozen endpoints."""
    return RetrievalReceipt(
        checked_at='2026-10-02T00:00:00Z',
        source_sha256=source_hash(source),
        before=before,
        after=after,
        before_tree=before_tree,
        after_tree=after_tree,
    )
