"""Offline transport tests for GitHub pagination, errors and issue links."""

import asyncio
import email.utils
import json
import threading
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest

from basket_collector.config import GitHubConfig
from basket_collector.models.sources import issue_id
from basket_collector.providers.github import GitHubClient, GitHubError, GitHubInterrupted


@pytest.fixture(autouse=True)
def isolate_provider_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep virtual provider pacing from freezing the HTTP runtime's asyncio clock."""
    monkeypatch.setattr(
        'basket_collector.providers.github.time',
        SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=time.sleep),
    )


@pytest.mark.parametrize('token', [None, ''])
def test_missing_graphql_token_is_retryable_without_requests(
    token: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow credential repair to resume a missing-token failure without automatic requests."""
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record unexpected HTTP attempts made without GraphQL credentials."""
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token=token) as client:
        with pytest.raises(GitHubError) as error:
            client.get_closing_issues('o/r', 1)
    assert error.value.code == 'github_auth_error'
    assert error.value.retryable is True
    assert calls == []


@pytest.mark.parametrize('header_kind', ['seconds', 'date'])
@pytest.mark.parametrize('budget', [2, 40])
def test_unavailable_retry_after_is_a_minimum(monkeypatch: pytest.MonkeyPatch, header_kind: str, budget: int) -> None:
    """Respect explicit server delays or stop retryably when the delay exceeds the local budget."""
    epoch = 1700000000.0
    elapsed = [0.0]
    sleeps: list[float] = []
    attempts: list[float] = []
    monkeypatch.setattr('basket_collector.providers.github.time.time', lambda: epoch)
    monkeypatch.setattr('basket_collector.providers.github.time.monotonic', lambda: elapsed[0])
    header = '30' if header_kind == 'seconds' else email.utils.formatdate(epoch + 30, usegmt=True)

    def sleep(seconds: float) -> None:
        """Advance a virtual monotonic clock by the actual requested delay."""
        sleeps.append(seconds)
        elapsed[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail once with a server delay, then record the retry arrival time."""
        attempts.append(elapsed[0])
        if len(attempts) == 1:
            return httpx.Response(503, headers={'Retry-After': header})
        return httpx.Response(200, json={'number': 1})

    monkeypatch.setattr('basket_collector.providers.github.time.sleep', sleep)
    with GitHubClient(
        GitHubConfig(max_retries=1, max_retry_delay_seconds=budget, requests_per_second=1000000),
        transport=httpx.MockTransport(handler),
    ) as client:
        if budget < 30:
            with pytest.raises(GitHubError) as error:
                client.get_pull_request('o/r', 1)
            assert error.value.code == 'github_unavailable'
            assert error.value.retryable is True
            assert attempts == [0.0]
            assert sleeps == []
        else:
            assert client.get_pull_request('o/r', 1)['number'] == 1
            assert attempts == [0.0, 30.0]
            assert sleeps == [30.0]


def test_unavailable_without_retry_after_keeps_bounded_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local exponential backoff bounded when the server supplies no delay."""
    sleeps: list[float] = []
    attempts = 0
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', sleeps.append)
    monkeypatch.setattr(GitHubClient, '_wait_for_turn', lambda self: None)

    def handler(request: httpx.Request) -> httpx.Response:
        """Return three transient failures before successful fallback retry."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts <= 3 else httpx.Response(200, json={'number': 1})

    with GitHubClient(
        GitHubConfig(max_retries=3, max_retry_delay_seconds=2), transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_pull_request('o/r', 1)['number'] == 1
    assert attempts == 4
    assert sleeps == [1, 2, 2]


def test_shutdown_during_http_client_setup_prevents_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe shutdown intent after asynchronous client setup and before the actual HTTP request."""
    stop = threading.Event()
    calls: list[str] = []
    real_enter = httpx.AsyncClient.__aenter__

    async def stopped_enter(self: httpx.AsyncClient) -> httpx.AsyncClient:
        """Set the stop event after entering the request client context."""
        client = await real_enter(self)
        stop.set()
        return client

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any HTTP request sent after shutdown intent."""
        calls.append(request.url.path)
        return httpx.Response(200, json={'number': 1})

    monkeypatch.setattr(httpx.AsyncClient, '__aenter__', stopped_enter)
    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), stop_event=stop) as client:
        with pytest.raises(GitHubInterrupted):
            client.get_pull_request('o/r', 1)
    assert calls == []


def test_rest_paginates_all_closed_prs_and_commits() -> None:
    """Verify every REST page of closed PRs and commits is requested."""
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: every REST page of closed PRs and commits is requested."""
        page = int(request.url.params['page'])
        calls.append((request.url.path, page))
        if request.url.path.endswith('/commits'):
            return httpx.Response(200, json=[{'sha': 'a' * 40}] * (100 if page == 1 else 1))
        return httpx.Response(200, json=[{'number': 1}] * (100 if page == 1 else 1))

    with GitHubClient(
        GitHubConfig(requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        pages = list(client.list_closed_prs('Owner/Repo'))
        commits = client.get_pull_request_commits('Owner/Repo', 5)
    assert [page for page, _ in pages] == [1, 2]
    assert len(commits) == 101
    assert calls == [
        ('/repos/Owner/Repo/pulls', 1),
        ('/repos/Owner/Repo/pulls', 2),
        ('/repos/Owner/Repo/pulls/5/commits', 1),
        ('/repos/Owner/Repo/pulls/5/commits', 2),
    ]


def test_retries_429_then_succeeds_and_does_not_retry_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify rate limits are retried while credential failures stop immediately."""
    sleeps: list[float] = []
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', sleeps.append)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return HTTP 429, success, or unauthorized responses for retry checks."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={'Retry-After': '3'})
        return httpx.Response(200, json={'number': 4})

    with GitHubClient(
        GitHubConfig(requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        assert client.get_pull_request('o/r', 4)['number'] == 4
    assert attempts == 2
    assert 3.0 in sleeps

    auth_attempts = 0

    def unauthorized(request: httpx.Request) -> httpx.Response:
        """Return an unauthorized response and count authentication attempts."""
        nonlocal auth_attempts
        auth_attempts += 1
        return httpx.Response(401, json={'message': 'secret response'})

    with GitHubClient(
        GitHubConfig(requests_per_second=1000000), transport=httpx.MockTransport(unauthorized), token='secret'
    ) as client:
        with pytest.raises(GitHubError, match='HTTP 401') as error:
            client.get_pull_request('o/r', 4)
    assert auth_attempts == 1
    assert 'secret' not in str(error.value)


def test_graphql_explicit_links_paginate_and_sort_cross_repository_issues() -> None:
    """Verify linked issues from all GraphQL pages retain cross-repository identities."""
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return two pages of linked issues from different repositories."""
        assert request.url.path == '/graphql'
        payload = json.loads(request.content)
        assert 'closingIssuesReferences' in payload['query']
        assert 'excludeUserLinked: false' in payload['query']
        cursor = payload['variables']['cursor']
        cursors.append(cursor)
        repo = 'Other/Repo' if cursor is None else 'owner/repo'
        number = 9 if cursor is None else 2
        return httpx.Response(
            200,
            json={
                'data': {
                    'repository': {
                        'pullRequest': {
                            'closingIssuesReferences': {
                                'nodes': [
                                    {
                                        'number': number,
                                        'url': f'https://github.com/{repo}/issues/{number}',
                                        'title': 'T',
                                        'body': None,
                                        'repository': {'nameWithOwner': repo, 'databaseId': 1 if cursor is None else 2},
                                    }
                                ],
                                'pageInfo': {
                                    'hasNextPage': cursor is None,
                                    'endCursor': 'next' if cursor is None else None,
                                },
                            },
                        }
                    }
                }
            },
        )

    with GitHubClient(
        GitHubConfig(requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        issues = client.get_closing_issues('owner/repo', 12)
    assert cursors == [None, 'next']
    assert [item.id for item in issues] == sorted(
        [issue_id('github', 'https://github.com', '1', 9), issue_id('github', 'https://github.com', '2', 2)]
    )
    assert all(item.body == '' for item in issues)


def test_graphql_partial_error_is_not_treated_as_no_links() -> None:
    """Verify partial GraphQL errors do not silently become an empty issue list."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return GraphQL data with errors to test partial failure."""
        return httpx.Response(200, json={'data': {'repository': None}, 'errors': [{'message': 'private'}]})

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test') as client:
        with pytest.raises(GitHubError, match='GraphQL returned errors'):
            client.get_closing_issues('o/r', 1)


def test_repository_visibility_is_not_serialized_or_filtered() -> None:
    """Keep accessible repositories on the same path without recording private flags."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return ordinary accessible project metadata with an upstream visibility label."""
        return httpx.Response(
            200,
            json={
                'id': 123,
                'full_name': 'o/r',
                'private': True,
                'html_url': 'https://github.com/o/r',
                'clone_url': 'https://github.com/o/r.git',
                'default_branch': 'develop',
            },
        )

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test') as client:
        repository = client.repository('o/r')
        assert repository.project_id == '123'
        assert 'private' not in repository.model_dump(mode='json')


def test_repository_snapshots_nonstandard_default_branch() -> None:
    """Verify the actual default branch is saved even when named develop."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: the actual default branch is saved even when named develop."""
        return httpx.Response(
            200,
            json={
                'id': 123,
                'full_name': 'o/r',
                'private': False,
                'html_url': 'https://github.com/o/r',
                'clone_url': 'https://github.com/o/r.git',
                'default_branch': 'develop',
            },
        )

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test') as client:
        assert client.repository('o/r').default_branch == 'develop'


def test_repository_requires_default_branch() -> None:
    """Verify repository metadata without a default branch is rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: repository metadata without a default branch is rejected."""
        return httpx.Response(
            200,
            json={
                'id': 123,
                'full_name': 'o/r',
                'private': False,
                'html_url': 'https://github.com/o/r',
                'clone_url': 'https://github.com/o/r.git',
            },
        )

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test') as client:
        with pytest.raises(GitHubError, match='default branch'):
            client.repository('o/r')


def test_stop_event_prevents_retry_and_interrupts_backoff() -> None:
    """Verify shutdown prevents retry and interrupts rate-limit backoff."""
    stop = threading.Event()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: shutdown prevents retry and interrupts rate-limit backoff."""
        nonlocal attempts
        attempts += 1
        stop.set()
        return httpx.Response(429, headers={'Retry-After': '60'})

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test', stop_event=stop) as client:
        with pytest.raises(GitHubInterrupted):
            client.get_pull_request('o/r', 1)
    assert attempts == 1

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test', stop_event=stop) as client:
        with pytest.raises(GitHubInterrupted):
            client.get_pull_request('o/r', 1)
    assert attempts == 1


def test_request_timeout_is_overall_deadline_for_slow_response_body() -> None:
    """Verify slow response bodies cannot outlive the whole request deadline."""
    attempts = 0

    class SlowBody(httpx.AsyncByteStream):
        """Stream response chunks slowly enough to cross the request deadline."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            """Yield delayed JSON chunks to test the overall HTTP deadline."""
            for part in (b'{"number":', b' ', b'1', b'}'):
                await asyncio.sleep(0.35)
                yield part

    def handler(request: httpx.Request) -> httpx.Response:
        """Stream delayed response chunks after the request deadline."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, stream=SlowBody())

    started = time.monotonic()
    with GitHubClient(
        GitHubConfig(request_timeout_seconds=1, max_retries=0), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        with pytest.raises(GitHubError, match='deadline') as error:
            client.get_pull_request('o/r', 1)
    elapsed = time.monotonic() - started
    assert attempts == 1
    assert 0.8 <= elapsed < 1.6
    assert error.value.code == 'github_timeout'
    assert error.value.retryable is True


@pytest.mark.parametrize(
    'status,headers,expected_code,retryable,expected_attempts',
    [
        (403, {'X-RateLimit-Remaining': '0', 'Retry-After': '0'}, 'github_rate_limited', True, 2),
        (403, {}, 'github_access_denied', False, 1),
        (401, {}, 'github_auth_error', False, 1),
        (404, {}, 'github_not_found', False, 1),
        (429, {'Retry-After': '0'}, 'github_rate_limited', True, 2),
        (503, {'Retry-After': '0'}, 'github_unavailable', True, 2),
    ],
)
def test_http_error_has_typed_retryability(
    status: int,
    headers: dict[str, str],
    expected_code: str,
    retryable: bool,
    expected_attempts: int,
) -> None:
    """Verify HTTP failures expose stable codes, retryability, and attempt counts."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the configured HTTP failure and count attempts."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, headers=headers, json={'message': 'do not log'})

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_pull_request('o/r', 1)
    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    assert attempts == expected_attempts
    assert 'do not log' not in str(error.value)


def test_malformed_response_is_nonretryable() -> None:
    """Verify malformed successful responses are classified as permanent errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a malformed PR response."""
        return httpx.Response(200, json=[])

    with GitHubClient(GitHubConfig(), transport=httpx.MockTransport(handler), token='test') as client:
        with pytest.raises(GitHubError) as error:
            client.get_pull_request('o/r', 1)
    assert error.value.retryable is False
    assert error.value.code == 'github_error'


def test_exhausted_network_error_is_retryable() -> None:
    """Verify exhausted network retries remain resumable failures."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: exhausted network retries remain resumable failures."""
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError('network unavailable')

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_pull_request('o/r', 1)
    assert attempts == 2
    assert error.value.code == 'github_network_error'
    assert error.value.retryable is True


def test_remote_protocol_error_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a disconnected server response uses the configured retry budget."""
    sleeps: list[float] = []
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', sleeps.append)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Disconnect on the first attempt and return a PR on the second."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=request)
        return httpx.Response(200, json={'number': 4})

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        assert client.get_pull_request('o/r', 4)['number'] == 4
    assert attempts == 2
    assert 1.0 in sleeps


@pytest.mark.parametrize('max_retries', [0, 2])
def test_exhausted_remote_protocol_error_is_retryable(max_retries: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify exhausted server disconnect retries become bounded resumable failures."""
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', lambda seconds: None)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Disconnect on every attempt and count the actual requests."""
        nonlocal attempts
        attempts += 1
        raise httpx.RemoteProtocolError('private server detail', request=request)

    with GitHubClient(
        GitHubConfig(max_retries=max_retries, requests_per_second=1000000),
        transport=httpx.MockTransport(handler),
        token='test',
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_pull_request('o/r', 4)
    assert attempts == max_retries + 1
    assert error.value.code == 'github_network_error'
    assert error.value.retryable is True
    assert isinstance(error.value.__cause__, httpx.RemoteProtocolError)
    assert 'private server detail' not in str(error.value)


def test_remote_protocol_error_does_not_retry_after_shutdown() -> None:
    """Verify shutdown after a server disconnect prevents any retry attempt."""
    stop = threading.Event()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Request shutdown as the server disconnects during the first attempt."""
        nonlocal attempts
        attempts += 1
        stop.set()
        raise httpx.RemoteProtocolError('server disconnected', request=request)

    with GitHubClient(
        GitHubConfig(max_retries=2, requests_per_second=1000000),
        transport=httpx.MockTransport(handler),
        token='test',
        stop_event=stop,
    ) as client:
        with pytest.raises(GitHubInterrupted):
            client.get_pull_request('o/r', 4)
    assert attempts == 1


def test_graphql_rate_limited_error_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a GraphQL rate-limit error is retried and then succeeds."""
    sleeps: list[float] = []
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', sleeps.append)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: a GraphQL rate-limit error is retried and then succeeds."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                headers={'Retry-After': '3'},
                json={'errors': [{'type': 'RATE_LIMITED', 'message': 'private detail'}]},
            )
        return httpx.Response(
            200,
            json={
                'data': {
                    'repository': {
                        'pullRequest': {
                            'closingIssuesReferences': {
                                'nodes': [],
                                'pageInfo': {'hasNextPage': False, 'endCursor': None},
                            },
                        }
                    }
                }
            },
        )

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        assert client.get_closing_issues('o/r', 1) == []
    assert attempts == 2
    assert 3.0 in sleeps


@pytest.mark.parametrize(
    'error_payload,headers',
    [
        ({'type': 'RATE_LIMITED', 'message': 'secret'}, {}),
        ({'extensions': {'code': 'RATE_LIMITED'}, 'message': 'secret'}, {}),
        ({'message': 'secret'}, {'X-RateLimit-Remaining': '0', 'Retry-After': '0'}),
        ({'message': 'unrelated error'}, {'Retry-After': '0'}),
        ({'message': 'You have exceeded a SeCoNdArY rAtE lImIt.'}, {}),
    ],
)
def test_graphql_rate_limit_exhaustion_is_retryable(
    error_payload: dict[str, object],
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify GraphQL rate-limit variants remain retryable after exhaustion."""
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', lambda seconds: None)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return GraphQL rate-limit errors for every attempt."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, headers=headers, json={'errors': [error_payload]})

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_closing_issues('o/r', 1)
    assert attempts == 2
    assert error.value.code == 'github_rate_limited'
    assert error.value.retryable is True
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('headers', [{}, {'Retry-After': 'not-a-date'}])
def test_graphql_permanent_error_remains_nonretryable(headers: dict[str, str]) -> None:
    """Verify unrelated GraphQL errors do not enter the retry loop."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: unrelated GraphQL errors do not enter the retry loop."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, headers=headers, json={'errors': [{'type': 'NOT_FOUND', 'message': 'secret'}]})

    with GitHubClient(
        GitHubConfig(max_retries=2, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_closing_issues('o/r', 1)
    assert attempts == 1
    assert error.value.code == 'github_error'
    assert error.value.retryable is False
    assert 'secret' not in str(error.value)


def test_graphql_primary_reset_beyond_wait_budget_stays_retryable_without_early_retry() -> None:
    """Verify a distant primary reset stops early retries without losing retryability."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a primary rate-limit reset beyond the allowed wait."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={
                'X-RateLimit-Remaining': '0',
                'X-RateLimit-Reset': str(int(time.time() + 3600)),
                'Retry-After': '0',
            },
            json={'errors': [{'message': 'rate limit'}]},
        )

    with GitHubClient(
        GitHubConfig(max_retries=3, requests_per_second=1000000, max_retry_delay_seconds=2),
        transport=httpx.MockTransport(handler),
        token='test',
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_closing_issues('o/r', 1)
    assert attempts == 1
    assert error.value.code == 'github_rate_limited'
    assert error.value.retryable is True


def test_graphql_secondary_403_without_headers_retries_without_changing_rest_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify a secondary GraphQL 403 retries without weakening REST authentication errors."""
    sleeps: list[float] = []
    monkeypatch.setattr('basket_collector.providers.github.time.sleep', sleeps.append)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a secondary GraphQL rate-limit HTTP 403."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={'errors': [{'message': 'You have exceeded a secondary rate limit.'}]})

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_closing_issues('o/r', 1)
    assert attempts == 2
    assert 60 in sleeps
    assert error.value.code == 'github_rate_limited'
    assert error.value.retryable is True


def test_graphql_secondary_retry_after_beyond_wait_budget_is_not_retried() -> None:
    """Verify an excessive secondary retry delay leaves the request pending."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a secondary limit with an excessive retry delay."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, headers={'Retry-After': '3600'}, json={'errors': [{'message': 'rate limited'}]})

    with GitHubClient(
        GitHubConfig(max_retries=3, requests_per_second=1000000, max_retry_delay_seconds=2),
        transport=httpx.MockTransport(handler),
        token='test',
    ) as client:
        with pytest.raises(GitHubError) as error:
            client.get_closing_issues('o/r', 1)
    assert attempts == 1
    assert error.value.code == 'github_rate_limited'
    assert error.value.retryable is True


@pytest.mark.parametrize('limit_kind', ['graphql', 'rest'])
def test_rate_limit_cooldown_is_shared_with_concurrent_request(
    monkeypatch: pytest.MonkeyPatch,
    limit_kind: str,
) -> None:
    """Verify a rate-limit cooldown delays another concurrent request."""
    cooldown_started = threading.Event()
    second_sent = threading.Event()
    attempts = 0
    failures: list[BaseException] = []
    real_wait = GitHubClient._wait

    def observed_wait(self: GitHubClient, seconds: float) -> None:
        """Observe cooldown waits before delegating to the original wait."""
        if threading.current_thread().name == 'rate-limited' and seconds >= 0.25:
            cooldown_started.set()
        real_wait(self, seconds)

    monkeypatch.setattr(GitHubClient, '_wait', observed_wait)

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: a rate-limit cooldown delays another concurrent request."""
        nonlocal attempts
        if request.url.path == '/repos/o/r/pulls/2':
            second_sent.set()
            return httpx.Response(200, json={'number': 2})
        attempts += 1
        if attempts == 1:
            if limit_kind == 'graphql':
                return httpx.Response(
                    200, headers={'Retry-After': '0.3'}, json={'errors': [{'message': 'secondary rate limit'}]}
                )
            return httpx.Response(429, headers={'Retry-After': '0.3'})
        if limit_kind == 'graphql':
            return httpx.Response(
                200,
                json={
                    'data': {
                        'repository': {
                            'pullRequest': {
                                'closingIssuesReferences': {'nodes': [], 'pageInfo': {'hasNextPage': False}},
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={'number': 1})

    with GitHubClient(
        GitHubConfig(max_retries=1, requests_per_second=1000000), transport=httpx.MockTransport(handler), token='test'
    ) as client:

        def first() -> None:
            """Send the first request in a worker and capture failures."""
            try:
                if limit_kind == 'graphql':
                    client.get_closing_issues('o/r', 1)
                else:
                    client.get_pull_request('o/r', 1)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=first, name='rate-limited')
        worker.start()
        assert cooldown_started.wait(2)
        second = threading.Thread(target=lambda: client.get_pull_request('o/r', 2))
        second.start()
        assert not second_sent.wait(0.1)
        worker.join(2)
        second.join(2)
        assert not worker.is_alive() and not second.is_alive()
    assert failures == []
    assert second_sent.is_set()
    assert attempts == 2


def test_graphql_cooldown_wait_is_interruptible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify shutdown interrupts a worker waiting on GraphQL cooldown."""
    stop = threading.Event()
    wait_started = threading.Event()
    failures: list[BaseException] = []
    attempts = 0
    real_wait = GitHubClient._wait

    def observed_wait(self: GitHubClient, seconds: float) -> None:
        """Observe cooldown waits before delegating to the original wait."""
        if seconds >= 1:
            wait_started.set()
        real_wait(self, seconds)

    monkeypatch.setattr(GitHubClient, '_wait', observed_wait)

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: shutdown interrupts a worker waiting on GraphQL cooldown."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200, headers={'Retry-After': '1.5'}, json={'errors': [{'message': 'secondary rate limit'}]}
        )

    with GitHubClient(
        GitHubConfig(max_retries=2, requests_per_second=1000000),
        transport=httpx.MockTransport(handler),
        token='test',
        stop_event=stop,
    ) as client:

        def run() -> None:
            """Run the rate-limited request and capture its shutdown result."""
            try:
                client.get_closing_issues('o/r', 1)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        assert wait_started.wait(2)
        stop.set()
        worker.join(1)
        assert not worker.is_alive()
    assert attempts == 1
    assert len(failures) == 1 and isinstance(failures[0], GitHubInterrupted)


def test_prebooked_worker_observes_later_shared_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a reserved request slot observes a later shared cooldown."""
    reserved = threading.Event()
    release = threading.Event()
    second_sent = threading.Event()
    failures: list[BaseException] = []
    real_wait = GitHubClient._wait

    def observed_wait(self: GitHubClient, seconds: float) -> None:
        """Observe cooldown waits before delegating to the original wait."""
        if threading.current_thread().name == 'prebooked' and not reserved.is_set():
            reserved.set()
            assert release.wait(2)
        real_wait(self, seconds)

    monkeypatch.setattr(GitHubClient, '_wait', observed_wait)

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve mocked HTTP responses for this scenario: a reserved request slot observes a later shared cooldown."""
        if request.url.path.endswith('/2'):
            second_sent.set()
            return httpx.Response(200, json={'number': 2})
        if request.url.path.endswith('/3'):
            return httpx.Response(429, headers={'Retry-After': '0.3'})
        return httpx.Response(200, json={'number': 1})

    with GitHubClient(
        GitHubConfig(max_retries=0, requests_per_second=20), transport=httpx.MockTransport(handler), token='test'
    ) as client:
        assert client.get_pull_request('o/r', 1)['number'] == 1

        def prebooked() -> None:
            """Send the reserved request in a worker and capture failures."""
            try:
                client.get_pull_request('o/r', 2)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=prebooked, name='prebooked')
        worker.start()
        assert reserved.wait(2)
        with pytest.raises(GitHubError) as error:
            client.get_pull_request('o/r', 3)
        assert error.value.code == 'github_rate_limited'
        release.set()
        assert not second_sent.wait(0.1)
        worker.join(2)
        assert not worker.is_alive()
    assert failures == []
    assert second_sent.is_set()


@pytest.mark.parametrize('status', [429, 503])
def test_server_cooldown_applies_to_next_github_record(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The final attempt still postpones subsequent records on the same API client."""
    clock = [100.0]
    sent: list[float] = []
    monkeypatch.setattr('basket_collector.providers.github.time.monotonic', lambda: clock[0])
    monkeypatch.setattr(
        'basket_collector.providers.github.time.sleep', lambda delay: clock.__setitem__(0, clock[0] + delay)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one server cooldown followed by a successful next record."""
        sent.append(clock[0])
        if len(sent) == 1:
            return httpx.Response(status, headers={'Retry-After': '60'})
        return httpx.Response(200, json={'number': 2})

    with GitHubClient(GitHubConfig(max_retries=0), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubError):
            client.get_pull_request('o/r', 1)
        assert client.get_pull_request('o/r', 2)['number'] == 2
    assert sent == [100, 160]
