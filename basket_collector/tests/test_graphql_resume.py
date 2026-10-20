"""A GraphQL rate limit leaves a resumable filter checkpoint with saved PRs."""

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from basket_collector.config import CollectorConfig
from basket_collector.contracts import (
    ChangedFile,
    Changes,
    ChangeStats,
    CommitMessage,
    FilterDocument,
    Producer,
    ResolvedRevisions,
    SourceRevisions,
)
from basket_collector.git import GitRepository
from basket_collector.github import GitHubClient
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.pipeline import Pipeline
from basket_collector.sources import RetrievalReceipt, SourceDescriptor
from basket_collector.storage import read_document
from fixture_helpers import native_config, retrieval

SHA = 'a' * 40
BASE = 'b' * 40
BLOB = 'c' * 40
HASH = 'd' * 64
TIME = '2026-01-02T00:00:00Z'


class FakeGitRepository:
    """Supply deterministic Git results to isolate GraphQL checkpoint behavior."""

    def prepare(self) -> None:
        """Accept cache preparation without touching a repository."""
        pass

    def fetch_commit(self, sha: str, fetch_refs: list[str] | None = None) -> None:
        """Treat fixture commit objects as already available."""
        pass

    def resolve_revisions(
        self, source: SourceRevisions, api_commits: list[str], fetch_refs: list[str] | None = None
    ) -> ResolvedRevisions:
        """Return the fixed range after checking the GitHub commit list."""
        assert api_commits == [SHA]
        return ResolvedRevisions(
            policy='change_merge_base_v1',
            before=BASE,
            after=SHA,
            commits=[SHA],
            before_tree='dddddddddddddddddddddddddddddddddddddddd',
            after_tree='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
        )

    def verify_retrieval(self, source: SourceDescriptor, resolved: ResolvedRevisions) -> RetrievalReceipt:
        """Supply explicit proof while keeping this HTTP recovery test offline."""
        return retrieval(source, resolved.before, resolved.after, resolved.before_tree, resolved.after_tree)

    def read_changes(self, resolved: ResolvedRevisions) -> Changes:
        """Return one deterministic text change for filter processing."""
        return Changes(
            files=[
                ChangedFile(
                    path='file.py',
                    status='added',
                    additions=1,
                    deletions=0,
                    before_blob=None,
                    after_blob=BLOB,
                    before_mode=None,
                    after_mode='100644',
                )
            ],
            stats=ChangeStats(files_count=1, additions=1, deletions=0, changed_lines=1, patch_chars=1),
            diff='x',
            commit_messages=[CommitMessage(sha=SHA, message='Change')],
        )


def _config(tmp_path: Path) -> CollectorConfig:
    """Build the local configuration fixture for this test module."""
    return CollectorConfig.model_validate(
        {
            'schema_version': 2,
            'source': {'provider': 'github', 'project': 'owner/repo', 'max_retries': 1, 'requests_per_second': 1000000},
            'dataset': {'name': 'test basket', 'description': 'offline', 'maintainers': ['alice']},
            'output_dir': str(tmp_path / 'output'),
            'cache_dir': str(tmp_path / 'cache'),
            'collect': {'selection': 'change_ids', 'change_ids': [1, 2]},
            'filter': {'workers': 1, 'max_retries': 0},
            'generation': {'source': 'issue'},
        }
    )


def _producer() -> Producer:
    """Build stable producer metadata for checkpoint fixtures."""
    return Producer(
        version='test',
        implementation_sha256=HASH,
        dependencies_sha256=HASH,
        python_version='3.12.3',
        git_version='git test',
    )


def _pull(number: int) -> dict[str, Any]:
    """Build a merged PR response for the requested fixture number."""
    return {
        'number': number,
        'merged_at': TIME,
        'html_url': f'https://github.com/owner/repo/pull/{number}',
        'title': 'Change',
        'body': '',
        'user': None,
        'commits': 1,
        'merge_commit_sha': None,
        'base': {'sha': BASE, 'ref': 'develop', 'repo': {'full_name': 'owner/repo', 'id': 123}},
        'head': {'sha': SHA, 'repo': None},
    }


def _issue(number: int) -> dict[str, Any]:
    """Build one linked issue response for the requested PR."""
    return {
        'data': {
            'repository': {
                'pullRequest': {
                    'closingIssuesReferences': {
                        'nodes': [
                            {
                                'number': 100 + number,
                                'url': f'https://github.com/owner/repo/issues/{100 + number}',
                                'title': 'Issue',
                                'body': 'Body',
                                'repository': {'nameWithOwner': 'owner/repo', 'databaseId': 123},
                            }
                        ],
                        'pageInfo': {'hasNextPage': False, 'endCursor': None},
                    }
                }
            }
        }
    }


@pytest.mark.parametrize(
    'limit_error,headers',
    [
        ({'type': 'RATE_LIMITED', 'message': 'secret'}, {'Retry-After': '0'}),
        ({'message': 'You have exceeded a secondary rate limit.'}, {'Retry-After': '0'}),
    ],
)
def test_filter_resumes_only_pending_pr_after_graphql_rate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_error: dict[str, str],
    headers: dict[str, str],
) -> None:
    """Verify filter resumes only the pending PR after a GraphQL rate limit."""
    monkeypatch.setattr('basket_collector.github.time.sleep', lambda seconds: None)
    config = _config(tmp_path)
    requests: list[tuple[str, int]] = []
    limited = True
    graphql_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return REST and GraphQL responses across a checkpoint resume."""
        nonlocal graphql_attempts
        if request.url.path == '/repos/owner/repo':
            return httpx.Response(
                200,
                json={
                    'full_name': 'owner/repo',
                    'private': False,
                    'html_url': 'https://github.com/owner/repo',
                    'clone_url': 'https://github.com/owner/repo.git',
                    'default_branch': 'develop',
                    'id': 123,
                },
            )
        if request.url.path == '/graphql':
            number = json.loads(request.content)['variables']['number']
            requests.append(('graphql', number))
            if limited and number == 2:
                graphql_attempts += 1
                return httpx.Response(200, headers=headers, json={'errors': [limit_error]})
            return httpx.Response(200, json=_issue(number))
        number = int(request.url.path.split('/')[5])
        requests.append(('rest', number))
        if request.url.path.endswith('/commits'):
            return httpx.Response(200, json=[{'sha': SHA}])
        return httpx.Response(200, json=_pull(number))

    transport = httpx.MockTransport(handler)
    with GitHubClient(native_config(config.source), transport=transport, token='test') as github:
        assert (
            Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect')
            == 0
        )
        assert (
            Pipeline(
                config,
                producer=_producer(),
                remote=GitHubRemoteVCS(config.source, client=github),
                git_repository=cast(GitRepository, FakeGitRepository()),
            ).run('filter')
            == 3
        )
    assert graphql_attempts == 1
    checkpoint = read_document(Path(config.output_dir) / 'filtered.json', FilterDocument)
    assert checkpoint.meta.status == 'completed'
    assert checkpoint.meta.counts.succeeded == 1
    assert checkpoint.meta.counts.pending == 0 and checkpoint.meta.counts.failed == 1
    assert checkpoint.failed[0].error.code == 'github_rate_limited'
    assert checkpoint.meta.error is None

    requests.clear()
    limited = False
    with GitHubClient(native_config(config.source), transport=transport, token='test') as github:
        assert (
            Pipeline(
                config,
                producer=_producer(),
                remote=GitHubRemoteVCS(config.source, client=github),
                git_repository=cast(GitRepository, FakeGitRepository()),
            ).run('filter', retry_failed=True)
            == 0
        )
    assert requests == [('rest', 2), ('rest', 2), ('graphql', 2)]
    filtered = read_document(Path(config.output_dir) / 'filtered.json', FilterDocument)
    assert sorted(row.change_request.number for row in filtered.tasks) == [1, 2]
