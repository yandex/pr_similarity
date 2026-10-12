"""Host-side OAuth rotation, private profiles and access-only worker snapshots."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from benchmark.auth import auth_snapshot
from benchmark.errors import BenchmarkError
from benchmark.storage import file_lock, read_json


def profile(directory: Path, *, expiry: int = 4000000) -> Path:
    """Create an owner-only fixture profile with OAuth and a second provider."""
    auth = directory / 'data/opencode/auth.json'
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                'openai': {
                    'type': 'oauth',
                    'access': 'access-only',
                    'refresh': 'host-refresh',
                    'expires': expiry,
                    'accountId': 'fixture-account',
                },
                'other': {'type': 'api', 'key': 'other-provider'},
            }
        )
    )
    return directory


def test_worker_snapshot_has_no_refresh_or_other_provider(tmp_path: Path) -> None:
    """Verify worker snapshot has no refresh or other provider."""
    root = profile(tmp_path / 'profile')
    content = auth_snapshot(root, 1800, clock=lambda: 1000)
    data = json.loads(content)
    assert set(data) == {'openai'}
    assert data['openai']['access'] == 'access-only'
    assert data['openai']['refresh'] == ''
    assert 'host-refresh' not in content and 'other-provider' not in content


def test_rotation_is_persisted_before_short_lifetime_failure(tmp_path: Path) -> None:
    """Verify rotation is persisted before short lifetime failure."""
    root = profile(tmp_path / 'profile', expiry=1000000)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 100}
            )
        )
    )
    with pytest.raises(BenchmarkError) as caught:
        auth_snapshot(root, 1800, clock=lambda: 1000, client=client)
    assert caught.value.code == 'auth_required'
    data = json.loads((root / 'data/opencode/auth.json').read_text())
    assert data['openai']['refresh'] == 'new-refresh'
    assert (root / 'data/opencode/auth.json').stat().st_mode & 0o777 == 0o600


def test_valid_refresh_returns_access_only_and_preserves_unrelated_entries(tmp_path: Path) -> None:
    """Verify valid refresh returns access only and preserves unrelated entries."""
    root = profile(tmp_path / 'profile', expiry=1000000)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}
            )
        )
    )
    content = auth_snapshot(root, 1800, clock=lambda: 1000, client=client)
    assert json.loads(content)['openai']['access'] == 'new-access'
    assert 'new-refresh' not in content
    persisted = json.loads((root / 'data/opencode/auth.json').read_text())
    assert persisted['other']['key'] == 'other-provider'


def test_refresh_failure_does_not_echo_response_or_credentials(tmp_path: Path) -> None:
    """Verify refresh failure does not echo response or credentials."""
    root = profile(tmp_path / 'profile', expiry=1000000)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text='SENSITIVE_BODY')))
    with pytest.raises(BenchmarkError) as caught:
        auth_snapshot(root, 1800, clock=lambda: 1000, client=client)
    assert 'SENSITIVE_BODY' not in str(caught.value)
    assert 'host-refresh' not in str(caught.value)


def test_concurrent_profile_refresh_rotates_once(tmp_path: Path) -> None:
    """Verify concurrent profile refresh rotates once."""
    root = profile(tmp_path / 'profile', expiry=1000000)
    requests = []

    def refresh(request: httpx.Request) -> httpx.Response:
        """Simulate a delayed refresh response with rotated access and refresh tokens."""
        requests.append(request)
        time.sleep(0.1)
        return httpx.Response(
            200, json={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}
        )

    client = httpx.Client(transport=httpx.MockTransport(refresh))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth_snapshot(root, 1800, clock=lambda: 1000, client=client), range(2)))
    assert len(requests) == 1
    assert all(json.loads(value)['openai']['access'] == 'new-access' for value in results)
    assert json.loads((root / 'data/opencode/auth.json').read_text())['openai']['refresh'] == 'new-refresh'


def test_cancelled_profile_lock_is_not_an_authentication_failure(tmp_path: Path) -> None:
    """Interrupt a real profile lock wait before reading or rotating any credentials."""
    root = profile(tmp_path / 'profile')
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    before = (root / 'data/opencode/auth.json').read_bytes()
    with file_lock(root / 'auth.lock'):
        timer.start()
        try:
            with pytest.raises(BenchmarkError) as caught:
                auth_snapshot(root, 1800, clock=lambda: 1000, cancel=cancel)
            assert caught.value.code == 'operation_cancelled'
            assert (root / 'data/opencode/auth.json').read_bytes() == before
        finally:
            timer.join()


def test_refresh_rotation_is_saved_before_pending_cancellation(tmp_path: Path) -> None:
    """Preserve a completed token rotation before honoring cancellation of its caller."""
    root = profile(tmp_path / 'profile', expiry=1000000)
    cancel = threading.Event()

    def rotated(_request: httpx.Request) -> httpx.Response:
        """Commit a synthetic server-side rotation while recording the user cancellation."""
        cancel.set()
        return httpx.Response(
            200, json={'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh', 'expires_in': 3600}
        )

    with httpx.Client(transport=httpx.MockTransport(rotated)) as client:
        with pytest.raises(BenchmarkError) as caught:
            auth_snapshot(root, 1800, clock=lambda: 1000, cancel=cancel, client=client)
        assert caught.value.code == 'operation_cancelled'
    assert read_json(root / 'data/opencode/auth.json')['openai']['refresh'] == 'rotated-refresh'
