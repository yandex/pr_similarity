"""Keep record retry budgets independent of transport adapter configuration."""

import threading

import httpx
import pytest
from basket_collector.attempts import AttemptJournal, AttemptRunner
from basket_collector.config import GitHubConfig, RetryPolicy
from basket_collector.contracts import StageError
from basket_collector.github import GitHubClient, GitHubError
from basket_collector.transport_scope import record_owned, record_scope


def test_record_retries_are_not_multiplied_by_github_retries() -> None:
    """Two stage attempts produce two requests even when the client allows seven retries."""
    calls: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        """Return a repeatable HTTP failure without external network access."""
        calls.append(request.url.path)
        return httpx.Response(503)

    def classify(error: Exception) -> StageError | None:
        """Keep unrelated exceptions fatal in the stage attempt loop."""
        if isinstance(error, GitHubError):
            return StageError(code=error.code, message=str(error), retryable=True)
        return None

    policy = RetryPolicy(max_retries=1)
    runner = AttemptRunner(
        policy, threading.Event(), lambda value: None, classify, (InterruptedError,), wait=lambda delay: None
    )
    with GitHubClient(
        GitHubConfig(max_retries=7, requests_per_second=100000), transport=httpx.MockTransport(reply)
    ) as client:
        result = runner.run(AttemptJournal.new('a' * 64, policy), lambda: client.repository('owner/repo'))
    assert result.error is not None and len(calls) == 2
    assert not record_owned()


def test_retry_after_reaches_owner_without_being_capped() -> None:
    """A stage owner receives the actual server delay even above the transport cap."""
    with GitHubClient(
        GitHubConfig(max_retries=5),
        transport=httpx.MockTransport(lambda request: httpx.Response(503, headers={'Retry-After': '180'})),
    ) as client:
        with record_scope(), pytest.raises(GitHubError) as caught:
            client.repository('owner/repo')
    assert caught.value.retry_after == 180
