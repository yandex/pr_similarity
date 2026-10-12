"""Bounded, read-only GitHub REST and GraphQL access for basket collection."""

import email.utils
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from basket_collector.config import GitHubConfig
from basket_collector.contracts import Issue
from basket_collector.http_runtime import HTTPRuntime
from basket_collector.remote import RemoteError, RemoteInterrupted
from basket_collector.sources import Repository, issue_id


class GitHubError(RemoteError):
    """A GitHub request or response cannot be used as a complete snapshot."""

    def __init__(self, message: str, *, code: str = 'github_error', retryable: bool = False) -> None:
        """Record a stable failure code and whether retrying could succeed."""
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class GitHubInterrupted(GitHubError, RemoteInterrupted):
    """The coordinator requested draining before another HTTP attempt."""


def _object(value: object, label: str) -> dict[str, Any]:
    """Require an object in a GitHub JSON response."""
    if not isinstance(value, dict):
        raise GitHubError(f'GitHub returned invalid {label}')
    return value


def _string(value: object, label: str) -> str:
    """Require a nonempty string in a GitHub JSON response."""
    if not isinstance(value, str) or not value:
        raise GitHubError(f'GitHub returned invalid {label}')
    return value


def _positive_int(value: object, label: str) -> int:
    """Require a positive integer in a GitHub JSON response."""
    if type(value) is not int or value <= 0:
        raise GitHubError(f'GitHub returned invalid {label}')
    return value


def github_url(value: object, label: str) -> str:
    """Keep source URLs and clone paths on the public credential-free host."""
    raw = _string(value, label)
    parsed = urlsplit(raw)
    if (
        parsed.scheme != 'https'
        or parsed.netloc.casefold() != 'github.com'
        or parsed.username
        or parsed.password
        or not parsed.path.startswith('/')
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubError(f'GitHub returned invalid {label}')
    return raw


def _retry_after(headers: httpx.Headers) -> float | None:
    """Read the longest valid delay from GitHub retry and rate-limit headers."""
    delays: list[float] = []
    value = headers.get('retry-after')
    if value is not None:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                delays.append(seconds)
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                delays.append(max(0.0, parsed.timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
    if headers.get('x-ratelimit-remaining') == '0':
        reset = headers.get('x-ratelimit-reset')
        if reset is not None:
            try:
                epoch = float(reset)
                if math.isfinite(epoch):
                    delays.append(max(0.0, epoch - time.time()))
            except ValueError:
                pass
    return max(delays) if delays else None


def _graphql_rate_limited(data: object, headers: httpx.Headers) -> bool:
    """Recognize GraphQL rate-limit errors even when HTTP succeeds."""
    if not isinstance(data, dict):
        return False
    errors = data.get('errors')
    if not isinstance(errors, list) or not errors:
        return False
    # GitHub's primary GraphQL limit is HTTP 200 with errors and this header.
    if headers.get('x-ratelimit-remaining') == '0':
        return True
    if 'retry-after' in headers and _retry_after(headers) is not None:
        return True
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get('extensions')
        values = [error.get('type'), error.get('code')]
        if isinstance(extensions, dict):
            values.extend((extensions.get('type'), extensions.get('code')))
        if any(isinstance(value, str) and value.upper() == 'RATE_LIMITED' for value in values):
            return True
        message = error.get('message')
        if isinstance(message, str) and 'secondary rate limit' in message.casefold():
            return True
    return False


class GitHubClient:
    """One rate limiter and retry budget shared by calls made through this client."""

    def __init__(
        self,
        config: GitHubConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Set up bounded requests, shared rate limiting, and optional test transport."""
        self.config = config
        self._token = token if token is not None else os.environ.get(config.token_env)
        self._stop_event = stop_event
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_version = 0
        self._rate_lock = threading.Lock()
        self._transport = transport
        self._closed = False
        self.wait_callback: Callable[[str], None] | None = None
        self._runtime = HTTPRuntime(transport=transport, shutdown_timeout=config.request_timeout_seconds)
        self._headers = {
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': config.api_version,
            'User-Agent': 'pr-similarity-os-basket-collector',
            **({'Authorization': f'Bearer {self._token}'} if self._token else {}),
        }

    def __enter__(self) -> 'GitHubClient':
        """Return this client for scoped use."""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close the client when its scope ends."""
        self.close()

    def close(self) -> None:
        """Prevent any further requests through this client."""
        self._closed = True
        runtime = getattr(self, '_runtime', None)
        if runtime is not None:
            runtime.close()

    def _notify(self, reason: str) -> None:
        """Offer safe wait state without allowing display errors to affect requests."""
        try:
            if self.wait_callback is not None:
                self.wait_callback(reason)
        except Exception:
            pass

    def _retry_wait(self, seconds: float) -> None:
        """Describe backoff while keeping the existing delay and stop policy."""
        self._notify('retry backoff')
        try:
            self._wait(seconds)
        finally:
            self._notify('HTTP request')

    def _check_running(self) -> None:
        """Abort before another request if the client closed or shutdown began."""
        if self._closed:
            raise GitHubError('GitHub client is closed')
        if self._stop_event is not None and self._stop_event.is_set():
            raise GitHubInterrupted('GitHub operation interrupted')

    def _wait(self, seconds: float) -> None:
        """Wait for a retry or rate-limit delay while honoring shutdown."""
        self._check_running()
        if seconds <= 0:
            return
        if self._stop_event is None:
            time.sleep(seconds)
        elif self._stop_event.wait(seconds):
            raise GitHubInterrupted('GitHub operation interrupted')

    def _wait_for_turn(self) -> None:
        """Reserve a request slot after any shared rate-limit cooldown."""
        self._notify('rate limit')
        interval = 1 / self.config.requests_per_second
        with self._rate_lock:
            scheduled = max(time.monotonic(), self._next_request_at, self._cooldown_until)
            self._next_request_at = scheduled + interval
            version = self._cooldown_version
        while True:
            self._wait(scheduled - time.monotonic())
            with self._rate_lock:
                if version == self._cooldown_version or self._cooldown_until <= scheduled:
                    break
                # A rate limit arrived after this worker reserved its slot.
                version = self._cooldown_version
                scheduled = max(self._cooldown_until, self._next_request_at)
                self._next_request_at = scheduled + interval
        self._check_running()

    def _set_cooldown(self, seconds: float) -> None:
        """Extend the shared cooldown after a rate-limit response."""
        with self._rate_lock:
            deadline = time.monotonic() + seconds
            if deadline > self._cooldown_until:
                self._cooldown_until = deadline
                self._cooldown_version += 1
                self._next_request_at = max(self._next_request_at, deadline)

    @staticmethod
    def _rate_limit_response(
        response: httpx.Response,
        method: str,
        path: str,
    ) -> tuple[bool, bool, object | None]:
        """Classify a response before normal HTTP and GraphQL validation.

        Returns:
            Overall rate-limit flag, GraphQL-specific flag, and parsed GraphQL
            response when its status permits an error payload.

        """
        graphql_data: object | None = None
        if method == 'POST' and path == '/graphql' and response.status_code in (200, 403):
            try:
                graphql_data = response.json()
            except ValueError:
                pass
        graphql_limited = _graphql_rate_limited(graphql_data, response.headers)
        rate_limited = (
            graphql_limited
            or response.status_code == 429
            or (
                response.status_code == 403
                and (response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers)
            )
        )
        return rate_limited, graphql_limited, graphql_data

    @staticmethod
    def _response_json(response: httpx.Response, graphql_data: object | None) -> object:
        """Return the checked GraphQL payload or decode one successful REST response."""
        if graphql_data is not None:
            return graphql_data
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError('GitHub returned invalid JSON') from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> object:
        """Read through the shared pool without changing provider error policy."""
        try:
            return self._request_json(method, path, params=params, json=json)
        finally:
            self._notify('')

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> object:
        """Request JSON with bounded retries, rate limiting, and response checks.

        Args:
            method: HTTP method.
            path: GitHub API path.
            params: Query parameters.
            json: Request body.

        Raises:
            GitHubError: If the response is unusable or retry limits are reached.
            GitHubInterrupted: If shutdown starts before another attempt.

        """
        for attempt in range(self.config.max_retries + 1):
            self._check_running()
            self._wait_for_turn()
            try:
                self._notify('HTTP request')
                response = self._runtime.request(
                    method,
                    str(httpx.URL('https://api.github.com').join(path)),
                    params=params,
                    json=json,
                    headers=self._headers,
                    timeout_seconds=self.config.request_timeout_seconds,
                    check_stop=self._check_running,
                )
            except TimeoutError as exc:
                self._check_running()
                if attempt == self.config.max_retries:
                    raise GitHubError(
                        'GitHub request exceeded overall deadline after bounded retries',
                        code='github_timeout',
                        retryable=True,
                    ) from exc
                self._retry_wait(min(2**attempt, self.config.max_retry_delay_seconds))
                continue
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                self._check_running()
                if attempt == self.config.max_retries:
                    raise GitHubError(
                        'GitHub request failed after bounded retries', code='github_network_error', retryable=True
                    ) from exc
                self._retry_wait(min(2**attempt, self.config.max_retry_delay_seconds))
                continue
            rate_limited, graphql_limited, graphql_data = self._rate_limit_response(response, method, path)
            if rate_limited:
                self._check_running()
                delay = _retry_after(response.headers)
                if delay is None:
                    # GitHub asks for at least a minute on secondary GraphQL limits without headers.
                    delay = 60 * (2**attempt) if graphql_limited else 2**attempt
                self._set_cooldown(delay)
                if attempt == self.config.max_retries or delay > self.config.max_retry_delay_seconds:
                    raise GitHubError(
                        f'GitHub rate limit after bounded retries (HTTP {response.status_code})',
                        code='github_rate_limited',
                        retryable=True,
                    )
                self._retry_wait(delay)
                continue
            if 500 <= response.status_code < 600:
                self._check_running()
                delay = _retry_after(response.headers)
                if attempt == self.config.max_retries or (
                    delay is not None and delay > self.config.max_retry_delay_seconds
                ):
                    raise GitHubError(
                        f'GitHub HTTP {response.status_code} after bounded retries',
                        code='github_unavailable',
                        retryable=True,
                    )
                self._retry_wait(delay if delay is not None else min(2**attempt, self.config.max_retry_delay_seconds))
                continue
            if response.status_code >= 400:
                code = (
                    'github_auth_error'
                    if response.status_code in (401, 403)
                    else 'github_not_found' if response.status_code == 404 else 'github_http_error'
                )
                remedy = (
                    f'check {self.config.token_env}, its expiry and project/API read permissions'
                    if response.status_code in (401, 403)
                    else (
                        'check the project locator and token permissions; a resource may be missing or hidden'
                        if response.status_code == 404
                        else 'check API capabilities and request configuration'
                    )
                )
                raise GitHubError(f'GitHub HTTP {response.status_code}; {remedy}.', code=code)
            return self._response_json(response, graphql_data)
        raise AssertionError('unreachable retry state')

    @staticmethod
    def _repo_path(repository: str) -> str:
        """Validate and URL-encode an owner/repository pair."""
        parts = repository.split('/')
        if len(parts) != 2 or not all(parts):
            raise ValueError('repository must be owner/repo')
        return quote(parts[0], safe='') + '/' + quote(parts[1], safe='')

    def repository(self, full_name: str) -> Repository:
        """Read and validate the public repository identity and default branch.

        Args:
            full_name: Requested owner/repository pair.

        Returns:
            Public repository metadata with a credential-free clone URL.

        Raises:
            GitHubError: If GitHub returns a private or mismatched repository.

        """
        data = _object(self._request('GET', f'/repos/{self._repo_path(full_name)}'), 'repository')
        name = _string(data.get('full_name'), 'repository full_name')
        if name.casefold() != full_name.casefold():
            raise GitHubError('repository identity differs from requested owner/repo')
        return Repository(
            provider='github',
            server_url='https://github.com',
            project_id=str(_positive_int(data.get('id'), 'project ID')),
            path=name,
            web_url=github_url(data.get('html_url'), 'repository URL'),
            default_branch=_string(data.get('default_branch'), 'repository default branch'),
        )

    def list_closed_prs(self, repository: str, *, start_page: int = 1) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        """Yield every REST page, including duplicates that callers must deduplicate."""
        if start_page < 1:
            raise ValueError('start_page must be positive')
        path = f'/repos/{self._repo_path(repository)}/pulls'
        page = start_page
        while True:
            self._check_page_budget(page)
            data = self._request('GET', path, params={'state': 'closed', 'per_page': 100, 'page': page})
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise GitHubError('GitHub returned invalid pull request page')
            yield page, data
            if len(data) < 100:
                break
            page += 1

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Fetch one PR metadata object by repository and number."""
        _positive_int(number, 'PR number')
        return _object(self._request('GET', f'/repos/{self._repo_path(repository)}/pulls/{number}'), 'pull request')

    def get_pull_request_commits(self, repository: str, number: int) -> list[dict[str, Any]]:
        """Read every commit page; revision validation belongs to the Git stage."""
        _positive_int(number, 'PR number')
        path = f'/repos/{self._repo_path(repository)}/pulls/{number}/commits'
        commits: list[dict[str, Any]] = []
        page = 1
        while True:
            self._check_page_budget(page)
            data = self._request('GET', path, params={'per_page': 100, 'page': page})
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise GitHubError('GitHub returned invalid commit page')
            commits.extend(data)
            if len(data) < 100:
                return commits
            page += 1

    def _check_page_budget(self, page: int) -> None:
        """Refuse incomplete pagination without pretending the observed prefix is complete."""
        if page > self.config.max_pages:
            raise GitHubError(
                'GitHub pagination exceeded source.max_pages; narrow the selection or increase the limit '
                'and rebuild with OVERWRITE=1.',
                code='source_pagination_budget',
                retryable=True,
            )

    def get_closing_issues(self, repository: str, number: int) -> list[Issue]:
        """Snapshot explicit closing references, including manually linked issues."""
        _positive_int(number, 'PR number')
        if not self._token:
            raise GitHubError(
                f'GitHub GraphQL requires token in {self.config.token_env}', code='github_auth_error', retryable=True
            )
        owner, name = repository.split('/', 1)
        query = '''
        query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              closingIssuesReferences(first: 100, after: $cursor, excludeUserLinked: false) {
                nodes { number url title body repository { nameWithOwner databaseId } }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        '''
        cursor: str | None = None
        seen_cursors: set[str] = set()
        issues: dict[str, Issue] = {}
        while True:
            self._check_page_budget(len(seen_cursors) + 1)
            data = _object(
                self._request(
                    'POST',
                    '/graphql',
                    json={
                        'query': query,
                        'variables': {
                            'owner': owner,
                            'name': name,
                            'number': number,
                            'cursor': cursor,
                        },
                    },
                ),
                'GraphQL response',
            )
            if data.get('errors'):
                raise GitHubError('GitHub GraphQL returned errors while reading closing issues')
            root = _object(data.get('data'), 'GraphQL data')
            repo = _object(root.get('repository'), 'GraphQL repository')
            pr = _object(repo.get('pullRequest'), 'GraphQL pull request')
            refs = _object(pr.get('closingIssuesReferences'), 'closing issues connection')
            nodes = refs.get('nodes')
            if not isinstance(nodes, list):
                raise GitHubError('GitHub returned invalid closing issues nodes')
            for raw in nodes:
                issue = _object(raw, 'closing issue')
                issue_repo = _object(issue.get('repository'), 'issue repository')
                full_name = _string(issue_repo.get('nameWithOwner'), 'issue repository name')
                issue_number = _positive_int(issue.get('number'), 'issue number')
                body = issue.get('body')
                if body is not None and not isinstance(body, str):
                    raise GitHubError('GitHub returned invalid issue body')
                item = Issue(
                    id=issue_id(
                        'github',
                        'https://github.com',
                        str(_positive_int(issue_repo.get('databaseId'), 'issue project ID')),
                        issue_number,
                    ),
                    repository=full_name,
                    number=issue_number,
                    url=github_url(issue.get('url'), 'issue URL'),
                    title=_string(issue.get('title'), 'issue title'),
                    body=body or '',
                )
                issues[item.id.casefold()] = item
            page_info = _object(refs.get('pageInfo'), 'closing issues pageInfo')
            if page_info.get('hasNextPage') is False:
                return [issues[key] for key in sorted(issues)]
            next_cursor = _string(page_info.get('endCursor'), 'closing issues endCursor')
            if next_cursor in seen_cursors:
                raise GitHubError('GitHub repeated a closing issues cursor')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
