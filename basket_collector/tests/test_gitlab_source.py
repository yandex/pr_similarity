"""GitLab metadata and access failures are tested without live API or model calls."""

import asyncio
import json
import threading
from collections.abc import Callable

import httpx
import pytest
from basket_collector.config import CollectConfig, SourceConfig
from basket_collector.gitlab import GitLabClient
from basket_collector.gitlab_source import GitLabRemoteVCS
from basket_collector.remote import RemoteError, RemoteInterrupted
from basket_collector.sources import Repository, change_id
from basket_collector.stages.collect import collect_change, discover_date_range


def configured(handler: Callable[[httpx.Request], httpx.Response]) -> GitLabRemoteVCS:
    """Bind a self-hosted deployment prefix and subgroup project to a mock transport."""
    config = SourceConfig(
        provider='gitlab',
        server_url='https://gitlab.example.org/Prefix',
        project='Group/Sub/project',
        requests_per_second=100000,
        max_retries=0,
    )
    client = GitLabClient(config, transport=httpx.MockTransport(handler), token='synthetic-fixture-only')
    return GitLabRemoteVCS(config, client=client)


def project() -> dict:
    """Return canonical metadata while labeling the accessible project private upstream."""
    return {
        'id': 123,
        'path_with_namespace': 'Group/Sub/project',
        'web_url': 'https://gitlab.example.org/Prefix/Group/Sub/project',
        'http_url_to_repo': 'https://gitlab.example.org/Prefix/Group/Sub/project.git',
        'default_branch': 'master',
        'visibility': 'private',
    }


def merged() -> dict:
    """Keep source, squash and merge objects distinct in the API fixture."""
    return {
        'id': 9999,
        'iid': 42,
        'target_project_id': 123,
        'state': 'merged',
        'merged_at': '2026-10-01T00:00:00+00:00',
        'target_branch': 'master',
        'web_url': 'https://gitlab.example.org/Prefix/Group/Sub/project/-/merge_requests/42',
        'title': 'Fix behavior',
        'description': 'Change context',
        'author': None,
        'diff_refs': {'base_sha': 'a' * 40, 'start_sha': 'b' * 40, 'head_sha': 'c' * 40},
        'merge_commit_sha': 'd' * 40,
        'squash_commit_sha': 'e' * 40,
    }


def test_gitlab_empty_source_commits_is_a_candidate_rejection() -> None:
    """A merged MR with no source commits carries a durable rejection identity."""

    def respond(request: httpx.Request) -> httpx.Response:
        """Return valid MR/version evidence and an empty authenticated commit list."""
        path = request.url.path
        if path.endswith('/commits'):
            data = []
        elif path.endswith('/versions'):
            data = [{'id': 1, 'base_commit_sha': 'a' * 40, 'start_commit_sha': 'b' * 40, 'head_commit_sha': 'c' * 40}]
        elif path.endswith('/merge_requests/42'):
            data = merged()
        else:
            data = project()
        return httpx.Response(200, json=data, headers={'x-next-page': ''})

    remote = configured(respond)
    try:
        repository = remote.resolve_repository('Group/Sub/project')
        with pytest.raises(RemoteError) as caught:
            collect_change(remote, repository, 42, CollectConfig(selection='change_ids', change_ids=[42]))
        assert caught.value.code == 'source_commits_unavailable'
        rejection = getattr(caught.value, 'record', None)
        assert rejection is not None
        assert rejection.id == change_id('gitlab', repository.server_url, '123', 'merge_request', 42)
        assert rejection.change_request.number == 42 and rejection.change_request.title == 'Fix behavior'
        assert rejection.reasons[0].code == 'source_commits_unavailable'
        assert rejection.reasons[0].observed == 0
    finally:
        remote.client.close()


def test_gitlab_remote_contract_and_iid_identity() -> None:
    """Normalize prefix/subgroups/private labels without replacing frozen head."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Serve complete project, merge request, commit and issue payloads."""
        calls.append(request.url.raw_path)
        assert request.headers['private-token'] == 'synthetic-fixture-only'
        path = request.url.path
        if path.startswith('/Prefix/api/v4/projects/') and '/merge_requests' not in path:
            data = project()
        elif path.endswith('/merge_requests/42/commits'):
            data = [{'id': 'c' * 40}]
        elif path.endswith('/merge_requests/42/versions'):
            data = [{'id': 1, 'base_commit_sha': 'a' * 40, 'start_commit_sha': 'b' * 40, 'head_commit_sha': 'c' * 40}]
        elif path.endswith('/merge_requests/42/closes_issues'):
            data = [
                {
                    'project_id': 123,
                    'iid': 7,
                    'title': 'Fix issue',
                    'description': 'Do work',
                    'web_url': 'https://gitlab.example.org/Prefix/Group/Sub/project/-/issues/7',
                }
            ]
        elif path.endswith('/merge_requests/42'):
            data = merged()
        elif path.endswith('/merge_requests'):
            data = [merged()]
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data, headers={'x-next-page': ''})

    remote = configured(respond)
    repository = remote.resolve_repository('Group/Sub/project')
    request = collect_change(remote, repository, 42, CollectConfig(selection='change_ids', change_ids=[42]))
    assert request.id == change_id('gitlab', 'https://gitlab.example.org/Prefix', '123', 'merge_request', 42)
    assert request.change_request.number == 42
    assert request.revisions.source.head_sha == 'c' * 40
    assert request.revisions.source.squash_commit_sha == 'e' * 40
    assert request.revisions.source.expected_base_sha == 'a' * 40
    assert request.source.fetch_refs == ['refs/merge-requests/42/head', 'refs/heads/master']
    assert 'visibility' not in json.dumps(repository.model_dump(mode='json'))
    assert remote.get_commits(request) == ['c' * 40]
    assert remote.get_closing_issues(request)[0].id.startswith('issue:')
    result = discover_date_range(
        remote,
        repository,
        CollectConfig(selection='date_range', merged_from='2026-09-30T00:00:00Z', merged_before='2026-10-02T00:00:00Z'),
    )
    assert result.numbers == (42,)
    assert any(b'Group%2FSub%2Fproject' in value for value in calls)


@pytest.mark.parametrize(
    'status,code',
    [
        (401, 'source_auth_error'),
        (403, 'source_access_denied'),
        (404, 'source_unavailable_or_forbidden'),
        (429, 'source_rate_limited'),
    ],
)
def test_gitlab_access_failure_is_not_empty_discovery(status: int, code: str) -> None:
    """Preserve the actionable cause rather than reporting no matching merged requests."""
    remote = configured(lambda _request: httpx.Response(status, json={'message': 'SENSITIVE_BODY'}))
    with pytest.raises(RemoteError) as caught:
        remote.resolve_repository('Group/Sub/project')
    assert caught.value.code == code
    assert 'SENSITIVE_BODY' not in str(caught.value)
    assert 'synthetic-fixture-only' not in str(caught.value)


def test_gitlab_pagination_rejects_foreign_next_url() -> None:
    """Refuse a pagination redirect before a token can be sent to another origin."""
    remote = configured(
        lambda _request: httpx.Response(
            200,
            json=[merged()],
            headers={'Link': '<https://foreign.example/api/v4/projects/123/merge_requests?page=2>; rel="next"'},
        )
    )
    with pytest.raises(RemoteError, match='pagination URL'):
        list(remote.client.pages('projects/123/merge_requests'))


def test_gitlab_pagination_does_not_stop_at_short_advertised_page() -> None:
    """Follow the server cursor even when a page contains fewer than per_page entries."""
    pages = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Advertise a second page after a deliberately short first response."""
        page = int(request.url.params['page'])
        pages.append(page)
        return httpx.Response(200, json=[{'id': str(page)}], headers={'x-next-page': '2' if page == 1 else ''})

    remote = configured(respond)
    result = list(remote.client.pages('projects/123/merge_requests/42/commits'))
    assert pages == [1, 2] and [row[1][0]['id'] for row in result] == ['1', '2']


def test_gitlab_date_filters_can_fall_back_without_changing_local_interval() -> None:
    """Older self-hosted APIs can reject optional filters while discovery stays exact."""
    calls: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Reject date arguments once, then return inside/outside boundary candidates."""
        optimized = 'merged_after' in request.url.params
        calls.append(optimized)
        if optimized:
            return httpx.Response(400)
        return httpx.Response(
            200,
            json=[merged(), {**merged(), 'iid': 43, 'merged_at': '2026-10-02T00:00:00Z'}],
            headers={'x-next-page': ''},
        )

    remote = configured(respond)
    repository = Repository(
        provider='gitlab',
        server_url=remote.config.server_url,
        project_id='123',
        path='Group/Sub/project',
        web_url=project()['web_url'],
        default_branch='master',
    )
    result = discover_date_range(
        remote,
        repository,
        CollectConfig(selection='date_range', merged_from='2026-10-01T00:00:00Z', merged_before='2026-10-02T00:00:00Z'),
    )
    assert result.numbers == (42,) and calls == [True, False]


def test_gitlab_pagination_budget_does_not_publish_a_complete_prefix() -> None:
    """A server with an endless next-page cursor must exhaust a finite read budget."""
    calls: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Advertise another page even when the current page is small."""
        page = int(request.url.params['page'])
        calls.append(page)
        return httpx.Response(200, json=[{'id': page}], headers={'x-next-page': str(page + 1)})

    remote = configured(respond)
    remote.client.config = remote.config.model_copy(update={'max_pages': 2})
    with pytest.raises(RemoteError) as caught:
        list(remote.client.pages('projects/123/merge_requests'))
    assert caught.value.code == 'source_pagination_budget' and calls == [1, 2]


def test_gitlab_shutdown_during_client_setup_prevents_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe shutdown after entering the HTTP scope before sending credentials."""
    calls: list[str] = []
    stop = threading.Event()
    config = SourceConfig(provider='gitlab', project='group/project', max_retries=0)
    enter = httpx.AsyncClient.__aenter__

    async def stopped_enter(client: httpx.AsyncClient) -> httpx.AsyncClient:
        """Raise stop intent after asynchronous connection setup."""
        result = await enter(client)
        stop.set()
        return result

    def respond(request: httpx.Request) -> httpx.Response:
        """Record any unexpected request after cancellation."""
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncClient, '__aenter__', stopped_enter)
    client = GitLabClient(
        config, token='synthetic-fixture-only', transport=httpx.MockTransport(respond), stop_event=stop
    )
    with pytest.raises(RemoteInterrupted):
        client.request('projects/123')
    assert calls == []


def test_gitlab_cancels_and_reaps_inflight_http() -> None:
    """A cancellation closes the pending request rather than waiting for socket timeout."""
    stop = threading.Event()
    finalized: list[bool] = []

    async def blocked(request: httpx.Request) -> httpx.Response:
        """Keep a request pending until the coordinator records stop intent."""
        stop.set()
        try:
            await asyncio.sleep(30)
            raise AssertionError('request should be cancelled')
        finally:
            finalized.append(True)

    client = GitLabClient(
        SourceConfig(provider='gitlab', project='group/project', max_retries=0),
        token='synthetic-fixture-only',
        transport=httpx.MockTransport(blocked),
        stop_event=stop,
    )
    with pytest.raises(RemoteInterrupted):
        client.request('projects/123')
    assert finalized == [True]


@pytest.mark.parametrize('token', ['', 'synthetic-rejected-token'])
def test_gitlab_requires_api_token_and_never_skips_diff_version(token: str) -> None:
    """Missing credentials fail before HTTP; a rejected version request never produces a record."""
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Expose the public metadata but reject native diff version access."""
        calls.append(request.url.path)
        if request.url.path.endswith('/versions'):
            return httpx.Response(401)
        data = merged() if request.url.path.endswith('/merge_requests/42') else project()
        return httpx.Response(200, json=data)

    config = SourceConfig(
        provider='gitlab',
        server_url='https://gitlab.example.org/Prefix',
        project='Group/Sub/project',
        requests_per_second=100000,
        max_retries=0,
    )
    client = GitLabClient(config, token=token, transport=httpx.MockTransport(respond))
    remote = GitLabRemoteVCS(config, client=client)
    with pytest.raises(RemoteError) as failure:
        repository = remote.resolve_repository(config.project)
        remote.get_change_request(repository, 42)
    assert failure.value.code == 'source_auth_error' and failure.value.retryable
    if not token:
        assert calls == [] and config.token_env in str(failure.value)
    else:
        assert calls[-1].endswith('/versions')


@pytest.mark.parametrize('empty_issue_project', [False, True])
def test_gitlab_followup_uses_frozen_project_identity(empty_issue_project: bool) -> None:
    """Keep MR reads on the frozen ID and accept issues from projects without Git commits."""
    path_lookups = 0
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Reassign a source path after collection while retaining its numeric project."""
        nonlocal path_lookups
        path = request.url.path
        calls.append(path)
        if path.endswith('/projects/Group/Sub/project'):
            path_lookups += 1
            data = project()
            if path_lookups > 1:
                data['id'] = 456
        elif path.endswith('/projects/456'):
            data = {'id': 456, 'path_with_namespace': 'Group/Issues', 'default_branch': None}
        elif path.endswith('/commits'):
            assert '/projects/123/' in path
            data = [{'id': 'c' * 40}]
        elif path.endswith('/versions'):
            data = [{'id': 1, 'base_commit_sha': 'a' * 40, 'start_commit_sha': 'b' * 40, 'head_commit_sha': 'c' * 40}]
        elif path.endswith('/closes_issues'):
            assert '/projects/123/' in path
            data = [
                {
                    'project_id': 456 if empty_issue_project else 123,
                    'iid': 7,
                    'title': 'Frozen project issue',
                    'description': 'Correct task',
                    'web_url': 'https://gitlab.example.org/Prefix/Group/Issues/-/issues/7',
                }
            ]
        else:
            data = merged()
        return httpx.Response(200, json=data, headers={'x-next-page': ''})

    remote = configured(respond)
    repository = remote.resolve_repository('Group/Sub/project')
    record = remote.get_change_request(repository, 42)
    assert remote.get_commits(record) == ['c' * 40]
    issue = remote.get_closing_issues(record)[0]
    assert issue.body == 'Correct task'
    assert issue.repository == ('Group/Issues' if empty_issue_project else repository.path)
    assert path_lookups == 1
    remote.projects.clear()
    with pytest.raises(RemoteError, match='identity changed') as failure:
        remote.get_commits(record)
    assert failure.value.code == 'source_changed'
    assert path_lookups == 2
