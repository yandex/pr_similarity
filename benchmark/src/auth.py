"""Private native login and host-only refresh of ChatGPT OAuth credentials."""

import json
import math
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeGuard

import httpx
from benchmark.cancellation import check_cancelled
from benchmark.errors import BenchmarkError
from benchmark.git import git_environment
from benchmark.storage import file_lock, parse_json, write_json

CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
TOKEN_URL = 'https://auth.openai.com/oauth/token'


def _finite_number(value: object) -> TypeGuard[int | float]:
    """Recognize finite numeric timestamps while rejecting booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _required(message: str) -> BenchmarkError:
    """Build a safe auth failure with the private login recovery command."""
    return BenchmarkError(
        'auth_required', message + ' Run make auth CONFIG=... to renew the private profile.', exit_code=1
    )


def _read_profile(path: Path) -> dict:
    """Read native OAuth credentials without exposing malformed contents."""
    try:
        if path.is_symlink():
            raise OSError('auth symlink')
        content = path.read_bytes()
        data = parse_json(content)
    except (OSError, BenchmarkError):
        raise _required('Cannot read the ChatGPT OAuth profile.') from None
    return data


def _access_only(data: dict, minimum: int, now: float) -> str:
    """Select only the access credentials with sufficient lifetime."""
    entry = data.get('openai')
    if not isinstance(entry, dict) or entry.get('type') != 'oauth':
        raise _required('ChatGPT OAuth login is missing; an API key is a different auth mode.')
    access, expires = entry.get('access'), entry.get('expires')
    if not isinstance(access, str) or not access:
        raise _required('OAuth access token is missing.')
    if not _finite_number(expires) or expires <= (now + minimum) * 1000:
        raise _required('OAuth token lifetime cannot cover setup, agent timeout and shutdown margin.')
    value = {'type': 'oauth', 'access': access, 'refresh': '', 'expires': expires}
    if entry.get('accountId') is not None:
        if not isinstance(entry['accountId'], str) or not entry['accountId']:
            raise _required('OAuth account identity is invalid.')
        value['accountId'] = entry['accountId']
    return json.dumps({'openai': value})


def auth_snapshot(
    profile: Path,
    minimum_valid_seconds: int,
    *,
    refresh_timeout: int = 30,
    expiry_margin: int = 60,
    clock: Callable[[], float] = time.time,
    client: httpx.Client | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Serialize refresh, reread on lock and persist rotation before validating TTL."""
    if minimum_valid_seconds < 0 or refresh_timeout <= 0 or expiry_margin < 0:
        raise BenchmarkError('invalid_auth_limits', 'OAuth lifetime and timeout limits must be nonnegative.')
    profile = profile.expanduser().resolve()
    auth_file = profile / 'data/opencode/auth.json'
    if not auth_file.is_file():
        raise _required('The private OAuth profile has not been initialized.')
    required = minimum_valid_seconds + expiry_margin
    try:
        with file_lock(profile / 'auth.lock', timeout=2 * refresh_timeout, cancel=cancel):
            data = _read_profile(auth_file)
            entry = data.get('openai')
            if not isinstance(entry, dict) or entry.get('type') != 'oauth':
                return _access_only(data, required, clock())
            expires = entry.get('expires')
            if entry.get('access') and _finite_number(expires) and expires > (clock() + required) * 1000:
                return _access_only(data, required, clock())
            refresh = entry.get('refresh')
            if not isinstance(refresh, str) or not refresh:
                raise _required('OAuth refresh credentials are missing.')
            started = clock()
            owned = client is None
            http = client or httpx.Client(timeout=refresh_timeout, follow_redirects=False)
            try:
                response = http.post(
                    TOKEN_URL,
                    data={'grant_type': 'refresh_token', 'refresh_token': refresh, 'client_id': CLIENT_ID},
                    timeout=refresh_timeout,
                )
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError):
                raise _required('OAuth refresh failed.') from None
            finally:
                if owned:
                    http.close()
            if not isinstance(result, dict):
                raise _required('OAuth refresh returned invalid data.')
            access, rotated = result.get('access_token'), result.get('refresh_token', refresh)
            lifetime = result.get('expires_in')
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(rotated, str)
                or not rotated
                or not _finite_number(lifetime)
                or lifetime <= 0
            ):
                raise _required('OAuth refresh returned invalid credentials or lifetime.')
            latest = _read_profile(auth_file)
            if latest.get('openai') != entry:
                raise _required('OAuth profile changed outside the benchmark; stop concurrent login and retry.')
            latest['openai'] = {**entry, 'access': access, 'refresh': rotated, 'expires': (started + lifetime) * 1000}
            write_json(auth_file, latest)
            # Persist rotations before honoring cancellation or rejecting the new token lifetime.
            check_cancelled(cancel)
            return _access_only(latest, required, clock())
    except OSError:
        raise _required('Cannot lock or save the private OAuth profile.') from None


def native_login(binary: Path, profile: Path, *, flow: str = 'headless') -> int:
    """Interactive auth output is direct terminal output, never a benchmark log."""
    if flow not in ('headless', 'browser'):
        raise BenchmarkError('invalid_auth_flow', 'AUTH_FLOW must be headless or browser.')
    profile = profile.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    for name in ('home', 'data', 'config', 'cache', 'state'):
        (profile / name).mkdir(exist_ok=True, mode=0o700)
    environment = git_environment()
    environment.update(
        HOME=str(profile / 'home'),
        XDG_DATA_HOME=str(profile / 'data'),
        XDG_CONFIG_HOME=str(profile / 'config'),
        XDG_CACHE_HOME=str(profile / 'cache'),
        XDG_STATE_HOME=str(profile / 'state'),
        OPENCODE_CONFIG_CONTENT='{}',
        OPENCODE_DISABLE_AUTOUPDATE='true',
    )
    with file_lock(profile / 'auth.lock'), TemporaryDirectory(prefix='benchmark-auth-') as cwd:
        result = subprocess.run(
            [str(binary), 'auth', 'login', '--provider', 'openai', '--method', f'ChatGPT Pro/Plus ({flow})'],
            env=environment,
            cwd=cwd,
            umask=0o077,
            check=False,
        )
    return result.returncode
