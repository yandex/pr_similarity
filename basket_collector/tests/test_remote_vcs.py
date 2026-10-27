"""Native API payloads normalize into one source contract before stage processing."""

import json

import httpx

from basket_collector.config import CollectConfig, SourceConfig
from basket_collector.models.sources import change_id
from basket_collector.providers.github import GitHubClient
from basket_collector.providers.github_source import GitHubRemoteVCS
from basket_collector.stages.collect import collect_change, discover_date_range


def reader() -> GitHubRemoteVCS:
    """Serve both discovery and one merged private-labeled API request in memory."""
    repository = {
        'id': 123,
        'full_name': 'owner/repo',
        'html_url': 'https://github.com/owner/repo',
        'clone_url': 'https://github.com/owner/repo.git',
        'default_branch': 'main',
        'private': True,
    }
    request = {
        'number': 42,
        'merged_at': '2026-10-01T00:00:00Z',
        'title': 'Fix behavior',
        'body': 'Task context',
        'html_url': 'https://github.com/owner/repo/pull/42',
        'user': {'login': 'author'},
        'commits': 1,
        'base': {'ref': 'main', 'sha': 'a' * 40, 'repo': repository},
        'head': {'sha': 'b' * 40, 'repo': None},
        'merge_commit_sha': 'c' * 40,
    }

    def respond(message: httpx.Request) -> httpx.Response:
        """Return complete normalized-source inputs without touching credentials or network."""
        path = message.url.path
        if path == '/repos/owner/repo':
            data = repository
        elif path.endswith('/pulls'):
            data = [request, {**request, 'number': 43, 'merged_at': None}]
        elif path.endswith('/pulls/42/commits'):
            data = [{'sha': 'b' * 40}]
        elif path.endswith('/pulls/42'):
            data = request
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)

    config = SourceConfig(provider='github', project='owner/repo')
    adapter = GitHubRemoteVCS(config)
    adapter.client.close()
    adapter.client = GitHubClient(adapter.client.config, transport=httpx.MockTransport(respond))
    return adapter


def test_github_remote_does_not_filter_repository_visibility() -> None:
    """Normalize accessible metadata while omitting the provider's private flag."""
    remote = reader()
    try:
        repository = remote.resolve_repository('owner/repo')
        record = collect_change(remote, repository, 42, CollectConfig(selection='change_ids', change_ids=[42]))
        assert record.id == change_id('github', 'https://github.com', '123', 'pull_request', 42)
        assert record.source.fetch_url == 'https://github.com/owner/repo.git'
        assert record.revisions.source.head_sha == 'b' * 40
        assert record.revisions.source.merge_commit_sha == 'c' * 40
        assert remote.get_commits(record) == ['b' * 40]
        assert 'private' not in json.dumps(repository.model_dump(mode='json'))
        assert record.change_request.kind == 'pull_request'
    finally:
        remote.close()


def test_shared_discovery_uses_half_open_interval_and_merged_candidates() -> None:
    """Keep closed-but-unmerged and upper-boundary changes out of the frozen queue."""
    remote = reader()
    try:
        repository = remote.resolve_repository('owner/repo')
        result = discover_date_range(
            remote,
            repository,
            CollectConfig(
                selection='date_range', merged_from='2026-09-30T00:00:00Z', merged_before='2026-10-02T00:00:00Z'
            ),
        )
        assert result.numbers == (42,)
        upper = discover_date_range(
            remote,
            repository,
            CollectConfig(
                selection='date_range', merged_from='2026-09-30T00:00:00Z', merged_before='2026-10-01T00:00:00Z'
            ),
        )
        assert upper.numbers == ()
    finally:
        remote.close()
