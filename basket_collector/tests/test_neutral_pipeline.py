"""An independent mocked hosting reader drives native Git into the new basket contract."""

import json
import subprocess
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

import httpx
import jsonschema
import pytest

from basket_collector.config import CollectorConfig, GitHubConfig
from basket_collector.models.artifacts import BasketDocument
from basket_collector.persistence.storage import read_document
from basket_collector.pipeline import Pipeline
from basket_collector.providers.github import GitHubClient
from basket_collector.providers.github_source import GitHubRemoteVCS
from basket_collector.providers.gitlab import GitLabClient
from basket_collector.providers.gitlab_source import GitLabRemoteVCS
from basket_collector.vcs.git import GitRepository


def github_response(before: str, after: str) -> Callable[[httpx.Request], httpx.Response]:
    """Build complete GitHub metadata and issue responses for the native pipeline."""
    repository = {
        'id': 123,
        'full_name': 'owner/repo',
        'html_url': 'https://github.com/owner/repo',
        'clone_url': 'https://github.com/owner/repo.git',
        'default_branch': 'main',
    }
    request = {
        'number': 42,
        'merged_at': '2026-10-01T00:00:00Z',
        'title': 'Fix source',
        'body': '',
        'html_url': 'https://github.com/owner/repo/pull/42',
        'user': None,
        'commits': 1,
        'base': {'ref': 'main', 'sha': before, 'repo': repository},
        'head': {'sha': after, 'repo': None},
        'merge_commit_sha': after,
    }

    def respond(message: httpx.Request) -> httpx.Response:
        """Serve the complete source and explicit issue link contract in memory."""
        if message.url.path == '/repos/owner/repo':
            data = repository
        elif message.url.path.endswith('/pulls/42/commits'):
            data = [{'sha': after}]
        elif message.url.path.endswith('/pulls/42'):
            data = request
        elif message.url.path == '/graphql':
            data = {
                'data': {
                    'repository': {
                        'pullRequest': {
                            'closingIssuesReferences': {
                                'nodes': [
                                    {
                                        'number': 7,
                                        'url': 'https://github.com/owner/repo/issues/7',
                                        'title': 'Fix source behavior',
                                        'body': 'Apply the required source change.',
                                        'repository': {'nameWithOwner': 'owner/repo', 'databaseId': 123},
                                    }
                                ],
                                'pageInfo': {'hasNextPage': False, 'endCursor': None},
                            }
                        }
                    }
                }
            }
        else:
            raise AssertionError(message.url.path)
        return httpx.Response(200, json=data)

    return respond


def gitlab_response(before: str, after: str) -> Callable[[httpx.Request], httpx.Response]:
    """Build complete GitLab metadata and issue responses for the native pipeline."""

    def respond(message: httpx.Request) -> httpx.Response:
        """Serve native MR payloads through the same four collector stages."""
        path = message.url.path
        if path in ('/api/v4/projects/owner/repo', '/api/v4/projects/123'):
            data = {
                'id': 123,
                'path_with_namespace': 'owner/repo',
                'web_url': 'https://gitlab.com/owner/repo',
                'http_url_to_repo': 'https://gitlab.com/owner/repo.git',
                'default_branch': 'main',
            }
        elif path.endswith('/merge_requests/42/commits'):
            data = [{'id': after}]
        elif path.endswith('/merge_requests/42/versions'):
            data = [{'id': 1, 'base_commit_sha': before, 'start_commit_sha': before, 'head_commit_sha': after}]
        elif path.endswith('/merge_requests/42/closes_issues'):
            data = [
                {
                    'project_id': 123,
                    'iid': 7,
                    'web_url': 'https://gitlab.com/owner/repo/-/issues/7',
                    'title': 'Fix source behavior',
                    'description': 'Apply the required source change.',
                }
            ]
        elif path.endswith('/merge_requests/42'):
            data = {
                'id': 9999,
                'iid': 42,
                'target_project_id': 123,
                'state': 'merged',
                'merged_at': '2026-10-01T00:00:00+00:00',
                'title': 'Fix source',
                'description': '',
                'web_url': 'https://gitlab.com/owner/repo/-/merge_requests/42',
                'author': None,
                'target_branch': 'main',
                'diff_refs': {'start_sha': before, 'base_sha': before, 'head_sha': after},
                'merge_commit_sha': after,
                'squash_commit_sha': None,
            }
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)

    return respond


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_mocked_issue_pipeline_exports_neutral_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Exercise all four real stages with native objects and no LLM/network calls."""
    upstream = tmp_path / 'upstream'
    upstream.mkdir()

    def git(*arguments: str) -> str:
        """Create and inspect only the owned upstream fixture."""
        return subprocess.check_output(['git', '-C', str(upstream), *arguments], text=True).strip()

    git('init', '-q', '--initial-branch=main')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (upstream / 'source.py').write_text('before\n')
    git('add', '.')
    git('commit', '-qm', 'before')
    before = git('rev-parse', 'HEAD')
    (upstream / 'source.py').write_text('after\n')
    git('commit', '-qam', 'after')
    after = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/pull/42/head', after)
    git('update-ref', 'refs/merge-requests/42/head', after)
    config = CollectorConfig.model_validate(
        {
            'schema_version': 2,
            'dataset': {'name': 'fixture', 'description': 'Fixture', 'maintainers': ['test']},
            'source': {'provider': provider, 'project': 'owner/repo'},
            'collect': {'selection': 'change_ids', 'change_ids': [42]},
            'generation': {'source': 'issue'},
            'output_dir': str(tmp_path / 'output'),
            'cache_dir': str(tmp_path / 'cache'),
        }
    )
    if provider == 'github':
        native = GitHubClient(
            GitHubConfig(),
            transport=httpx.MockTransport(github_response(before, after)),
            token='synthetic-fixture-only',
        )
        remote = GitHubRemoteVCS(config.source, client=native)
    else:
        native = GitLabClient(
            config.source, transport=httpx.MockTransport(gitlab_response(before, after)), token='synthetic-fixture-only'
        )
        remote = GitLabRemoteVCS(config.source, client=native)

    def fetch(cache: GitRepository, reference: str, depth: int) -> None:
        """Deliver a real Git pack via local transport instead of network."""
        cache._run('fetch', '--no-tags', '--depth=' + str(depth), str(upstream), reference)

    monkeypatch.setattr(GitRepository, '_fetch', fetch)
    try:
        assert Pipeline(config, remote=remote).run('basket') == 0
        basket = read_document(tmp_path / 'output/basket.json', BasketDocument)
        assert len(basket.tasks) == 1
        row = basket.tasks[0]
        assert row.revisions.before == before and row.revisions.after == after
        assert row.workspace == '.' and row.diff_policy == 'git_source_changes_v1'
        assert row.provenance.generation.attempts == 0
        assert row.provenance.retrieval.before_tree == row.revisions.before_tree
        schema = json.loads(files('basket_collector').joinpath('schemas/basket-v3.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(basket.model_dump(mode='json'))
    finally:
        native.close()
