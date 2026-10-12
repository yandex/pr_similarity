"""Offline GitHub credential and backoff recovery through the actual filter pipeline."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from basket_collector.config import CollectorConfig
from basket_collector.contracts import FilterDocument, Producer
from basket_collector.git import GitRepository
from basket_collector.github import GitHubClient, GitHubError
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.pipeline import Pipeline
from basket_collector.storage import read_checkpoint, read_document
from fixture_helpers import local_git, native_config


def _git(path: Path, *args: str) -> str:
    """Run Git only in the temporary fixture repository."""
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


class LocalGitHub:
    """Serve two public PRs backed by actual commits in a temporary Git repository."""

    def __init__(self, path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Create local source revisions and deterministic pipeline provenance."""
        self.source = path / 'source'
        self.source.mkdir()
        _git(self.source, 'init', '-q', '--initial-branch=main')
        _git(self.source, 'config', 'user.name', 'Test')
        _git(self.source, 'config', 'user.email', 'test@example.invalid')
        (self.source / 'file.py').write_text('value = 1\n', encoding='utf-8')
        _git(self.source, 'add', '.')
        _git(self.source, 'commit', '-qm', 'base')
        self.base = _git(self.source, 'rev-parse', 'HEAD')
        (self.source / 'file.py').write_text('value = 2\n', encoding='utf-8')
        _git(self.source, 'commit', '-qam', 'change')
        self.head = _git(self.source, 'rev-parse', 'HEAD')
        for number in (1, 2):
            _git(self.source, 'update-ref', f'refs/pull/{number}/head', self.head)
        local_git(monkeypatch, self.source)
        self.config = CollectorConfig.model_validate(
            {
                'source': {
                    'provider': 'github',
                    'project': 'owner/repo',
                    'requests_per_second': 1000000,
                    'max_retries': 1,
                    'max_retry_delay_seconds': 2,
                },
                'dataset': {'name': 'test', 'description': 'offline', 'maintainers': ['alice']},
                'output_dir': str(path / 'output'),
                'cache_dir': str(path / 'cache'),
                'collect': {'selection': 'change_ids', 'change_ids': [1, 2]},
                'filter': {'workers': 1, 'selection': {'shuffle': False}},
                'generation': {'source': 'issue'},
            }
        )
        self.producer = Producer(
            version='test',
            implementation_sha256='a' * 64,
            dependencies_sha256='b' * 64,
            python_version='3.12.3',
            git_version='git test',
        )
        self.calls: list[str] = []
        self.issue_numbers: list[int] = []
        self.failure: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Serve REST anonymously and require a fake credential for successful GraphQL responses."""
        route = request.url.path
        self.calls.append(route)
        if route == '/repos/owner/repo':
            return httpx.Response(
                200,
                json={
                    'full_name': 'owner/repo',
                    'private': False,
                    'html_url': 'https://github.com/owner/repo',
                    'clone_url': 'https://github.com/owner/repo.git',
                    'default_branch': 'main',
                    'id': 123,
                },
            )
        if route == '/graphql':
            number = json.loads(request.content)['variables']['number']
            self.issue_numbers.append(number)
            if number == 2 and self.failure is not None:
                return httpx.Response(self.failure, headers={'Retry-After': '30'} if self.failure == 503 else {})
            assert request.headers['Authorization'] == 'Bearer fixture-key'
            return self._issues(number)
        if route.endswith('/commits'):
            return httpx.Response(200, json=[{'sha': self.head}])
        number = int(route.rsplit('/', 1)[1])
        return httpx.Response(
            200,
            json={
                'number': number,
                'merged_at': '2026-01-02T00:00:00Z',
                'html_url': f'https://github.com/owner/repo/pull/{number}',
                'title': 'Change',
                'body': '',
                'user': None,
                'commits': 1,
                'merge_commit_sha': None,
                'base': {'sha': self.base, 'ref': 'main', 'repo': {'full_name': 'owner/repo', 'id': 123}},
                'head': {'sha': self.head, 'repo': None},
            },
        )

    @staticmethod
    def _issues(number: int) -> httpx.Response:
        """Return a complete issue link for a PR."""
        return httpx.Response(
            200,
            json={
                'data': {
                    'repository': {
                        'pullRequest': {
                            'closingIssuesReferences': {
                                'nodes': [
                                    {
                                        'number': number + 10,
                                        'url': f'https://github.com/owner/repo/issues/{number + 10}',
                                        'title': 'Change value',
                                        'body': 'Set the value to two.',
                                        'repository': {'nameWithOwner': 'owner/repo', 'databaseId': 123},
                                    }
                                ],
                                'pageInfo': {'hasNextPage': False},
                            }
                        }
                    }
                }
            },
        )

    def run(self, stage: str) -> int:
        """Create fresh real clients and run a stage against the same durable output."""
        with GitHubClient(native_config(self.config.source), transport=httpx.MockTransport(self.handler)) as github:
            repository = GitRepository(Path(self.config.cache_dir), str(self.source), 10)
            return Pipeline(
                self.config,
                producer=self.producer,
                remote=GitHubRemoteVCS(self.config.source, client=github),
                git_repository=repository,
            ).run(stage)

    def checkpoint(self) -> FilterDocument:
        """Read the durable filter document for recovery assertions."""
        checkpoint, _ = read_checkpoint(Path(self.config.output_dir) / '.work' / 'filter.json', FilterDocument)
        return checkpoint


@pytest.mark.parametrize('token', [None, ''])
def test_anonymous_collect_resumes_filter_after_adding_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    """Repair missing GraphQL credentials while keeping anonymous collection unchanged."""
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    if token is not None:
        monkeypatch.setenv('GITHUB_TOKEN', token)
    fixture = LocalGitHub(tmp_path, monkeypatch)
    assert fixture.run('collect') == 0
    collected_path = Path(fixture.config.output_dir) / 'collected.json'
    collected = collected_path.read_bytes()
    with pytest.raises(GitHubError) as error:
        fixture.run('filter')
    checkpoint = fixture.checkpoint()
    assert checkpoint.meta.status == 'failed'
    assert checkpoint.meta.counts.pending == 2
    assert checkpoint.meta.error is not None
    assert checkpoint.meta.error.code == 'github_auth_error'
    assert checkpoint.meta.error.retryable is True
    assert fixture.issue_numbers == []
    assert error.value.code == 'github_auth_error'
    monkeypatch.setenv('GITHUB_TOKEN', 'fixture-key')
    assert fixture.run('filter') == 0
    filtered = read_document(Path(fixture.config.output_dir) / 'filtered.json', FilterDocument)
    assert [item.change_request.number for item in filtered.tasks] == [1, 2]
    assert fixture.issue_numbers == [1, 2]
    assert fixture.calls.count('/repos/owner/repo') == 1
    assert collected_path.read_bytes() == collected


@pytest.mark.parametrize('failure', [401, 503])
def test_filter_recovery_preserves_completed_prs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: int) -> None:
    """Keep successful filter records while repairing credentials or a server-requested long delay."""
    monkeypatch.setenv('GITHUB_TOKEN', 'fixture-key')
    sleeps: list[float] = []
    monkeypatch.setattr('basket_collector.github.time.sleep', sleeps.append)
    fixture = LocalGitHub(tmp_path, monkeypatch)
    fixture.failure = failure
    assert fixture.run('collect') == 0
    with pytest.raises(GitHubError):
        fixture.run('filter')
    checkpoint = fixture.checkpoint()
    assert checkpoint.meta.counts.succeeded == 1
    assert checkpoint.meta.counts.pending == 1
    assert checkpoint.meta.error is not None and checkpoint.meta.error.retryable is True
    assert checkpoint.meta.error.code == ('github_auth_error' if failure == 401 else 'github_unavailable')
    assert fixture.issue_numbers == [1, 2]
    assert all(delay < 2 for delay in sleeps)
    done = checkpoint.tasks[0]
    if failure == 401:
        monkeypatch.delenv('GITHUB_TOKEN')
        with pytest.raises(GitHubError):
            fixture.run('filter')
        missing = fixture.checkpoint()
        assert missing.meta.error is not None and missing.meta.error.code == 'github_auth_error'
        assert missing.meta.error.retryable is True
        assert missing.tasks == [done]
        assert missing.meta.counts.pending == 1
        assert fixture.issue_numbers == [1, 2]
        monkeypatch.setenv('GITHUB_TOKEN', 'fixture-key')
    fixture.failure = None
    assert fixture.run('filter') == 0
    filtered = read_document(Path(fixture.config.output_dir) / 'filtered.json', FilterDocument)
    assert fixture.issue_numbers == [1, 2, 2]
    assert filtered.tasks[0] == done
    assert [item.change_request.number for item in filtered.tasks] == [1, 2]
