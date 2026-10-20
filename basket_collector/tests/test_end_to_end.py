"""Run every stage with a real temporary Git DAG and a local GitHub transport."""

import subprocess
from pathlib import Path

import httpx
import pytest
from basket_collector.config import CollectorConfig
from basket_collector.contracts import BasketDocument, Producer
from basket_collector.git import GitRepository
from basket_collector.github import GitHubClient
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.pipeline import Pipeline
from basket_collector.storage import read_document
from fixture_helpers import local_git, native_config


def _git(directory: Path, *args: str) -> str:
    """Run a Git command in the temporary source repository."""
    result = subprocess.run(
        ['git', '-C', str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _github_response(request: httpx.Request, before: str, after: str, calls: list[str]) -> httpx.Response:
    """Answer fixture GitHub requests and record their order."""
    calls.append(f'{request.method} {request.url.path}')
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
    if request.url.path == '/repos/owner/repo/pulls/7':
        return httpx.Response(
            200,
            json={
                'number': 7,
                'merged_at': '2026-01-02T00:00:00Z',
                'html_url': 'https://github.com/owner/repo/pull/7',
                'title': 'Add program',
                'body': 'PR context',
                'user': {'login': 'alice'},
                'commits': 1,
                'merge_commit_sha': after,
                'base': {'sha': before, 'ref': 'develop', 'repo': {'full_name': 'owner/repo', 'id': 123}},
                'head': {'sha': after, 'repo': {'full_name': 'owner/repo', 'id': 123}},
            },
        )
    if request.url.path == '/repos/owner/repo/pulls/7/commits':
        return httpx.Response(200, json=[{'sha': after}])
    if request.url.path == '/graphql':
        return httpx.Response(
            200,
            json={
                'data': {
                    'repository': {
                        'pullRequest': {
                            'closingIssuesReferences': {
                                'nodes': [
                                    {
                                        'number': 3,
                                        'url': 'https://github.com/owner/repo/issues/3',
                                        'title': 'Add program',
                                        'body': 'Write main.py',
                                        'repository': {'nameWithOwner': 'owner/repo', 'databaseId': 123},
                                    }
                                ],
                                'pageInfo': {'hasNextPage': False, 'endCursor': None},
                            }
                        }
                    }
                }
            },
        )
    raise AssertionError(f'unexpected request {request.url}')


@pytest.mark.parametrize('workspace', ['.', 'new/package'])
def test_complete_offline_basket_uses_exact_git_and_saved_issue(
    tmp_path: Path, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build and reuse a complete offline basket from exact Git revisions."""
    source = tmp_path / 'source'
    source.mkdir()
    _git(source, 'init', '-q')
    _git(source, 'config', 'user.name', 'Fixture')
    _git(source, 'config', 'user.email', 'fixture@example.invalid')
    (source / 'README.md').write_text('base\n', encoding='utf-8')
    _git(source, 'add', '.')
    _git(source, 'commit', '-qm', 'base')
    before = _git(source, 'rev-parse', 'HEAD')
    (source / workspace).mkdir(parents=True, exist_ok=True)
    (source / workspace / 'main.py').write_text('print(\'hello\')\n', encoding='utf-8')
    _git(source, 'add', '.')
    _git(source, 'commit', '-qm', 'add program')
    after = _git(source, 'rev-parse', 'HEAD')

    local_git(monkeypatch, source)
    config = CollectorConfig.model_validate(
        {
            'schema_version': 2,
            'source': {'provider': 'github', 'project': 'owner/repo'},
            'dataset': {'name': 'fixture', 'description': 'offline', 'maintainers': ['alice']},
            'output_dir': str(tmp_path / 'output'),
            'cache_dir': str(tmp_path / 'cache'),
            'collect': {'selection': 'change_ids', 'change_ids': [7]},
            'generation': {'source': 'issue'},
        }
    )
    calls: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        """Bind the fixture revisions to the local GitHub response helper."""
        return _github_response(request, before, after, calls)

    producer = Producer(
        version='fixture',
        implementation_sha256='a' * 64,
        dependencies_sha256='b' * 64,
        python_version='3.12.3',
        git_version='git test',
    )
    with GitHubClient(native_config(config.source), transport=httpx.MockTransport(reply), token='fixture') as github:
        cache = GitRepository(Path(config.cache_dir), str(source), 10)
        pipeline = Pipeline(
            config, producer=producer, remote=GitHubRemoteVCS(config.source, client=github), git_repository=cache
        )
        assert pipeline.run('basket') == 0
        output = Path(config.output_dir)
        basket = read_document(output / 'basket.json', BasketDocument)
        assert basket.meta.repository.default_branch == 'develop'
        assert basket.tasks[0].revisions.before == before
        assert basket.tasks[0].revisions.after == after
        assert [item.path for item in basket.tasks[0].changes.files] == [(Path(workspace) / 'main.py').as_posix()]
        assert basket.tasks[0].agent_task == 'Task: Add program\n\nSubtasks:\n- Write main.py'
        assert basket.tasks[0].workspace == workspace
        assert basket.meta.schema_version == 2
        assert not basket.meta.partial
        first_bytes = (output / 'basket.json').read_bytes()
        first_calls = list(calls)
        assert (
            Pipeline(
                config, producer=producer, remote=GitHubRemoteVCS(config.source, client=github), git_repository=cache
            ).run('basket')
            == 0
        )
        assert calls == first_calls
        assert (output / 'basket.json').read_bytes() == first_bytes
