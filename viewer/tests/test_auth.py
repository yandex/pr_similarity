"""Browser login and local role provisioning without changing the publish wire protocol."""

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import service_for
from starlette.requests import Request
from starlette.testclient import TestClient

from viewer import cli
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence import database
from viewer.persistence.database import AccountActor, Database, password_hash
from viewer.persistence.storage import MemoryStore
from viewer.web.api import create_app
from viewer.web.auth import BrowserAuth

ORIGIN = 'https://viewer.example.test'


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Yield an HTTPS client with regular and administrator fixture accounts."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    cfg = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock', public_origin=ORIGIN)
    with TestClient(create_app(cfg, MemoryStore()), base_url=ORIGIN) as c:
        service_for(c).db.create_user('Alice', 'secret')
        service_for(c).db.create_user('operator', 'secret', role='admin')
        yield c


def login(client: TestClient, user: str = 'Alice') -> dict[str, str]:
    """Authenticate a fixture account and return its origin and CSRF headers."""
    challenge = client.get('/api/v1/auth/session')
    assert challenge.status_code == 200
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': challenge.json()['csrf_token']}
    response = client.post('/api/v1/auth/session', json={'login': user, 'password': 'secret'}, headers=headers)
    assert response.status_code == 200
    return {'Origin': ORIGIN, 'X-CSRF-Token': response.json()['csrf_token']}


def test_v1_migration_keeps_accounts_and_rejects_newer_database(tmp_path: Path) -> None:
    """Verify v1 migration keeps accounts and rejects newer database."""
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    encoded = password_hash('secret')
    with sqlite3.connect(runtime / 'viewer.sqlite') as db:
        db.execute('CREATE TABLE users(login TEXT PRIMARY KEY,password TEXT NOT NULL,enabled INTEGER NOT NULL)')
        db.execute('INSERT INTO users VALUES (?,?,1)', ('Alice', encoded))
        db.execute('PRAGMA user_version=1')
    db = Database(runtime)
    assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 2
    assert db.authenticate('Alice', 'secret') == {'login': 'Alice', 'role': 'user'}
    assert db.connection.execute('SELECT password FROM users').fetchone()[0] == encoded
    assert list(runtime.glob('viewer-v1-backup-*.sqlite'))
    db.close()
    with sqlite3.connect(runtime / 'viewer.sqlite') as future:
        future.execute('PRAGMA user_version=3')
    with pytest.raises(ViewerError, match='newer'):
        Database(runtime)
    with sqlite3.connect(runtime / 'viewer.sqlite') as future:
        assert future.execute('PRAGMA user_version').fetchone()[0] == 3


def test_public_pages_and_login_logout(client: TestClient) -> None:
    """Verify public pages and login logout."""
    assert client.get('/').status_code == 200
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False
    headers = login(client)
    assert client.get('/api/v1/auth/session').json()['actor'] == {'login': 'Alice', 'role': 'user'}
    assert client.get('/api/v1/admin/users').status_code == 403
    assert client.delete('/api/v1/auth/session', headers=headers).status_code == 200
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False


def test_admin_session_requires_csrf_and_cannot_create_roles(client: TestClient) -> None:
    """Verify admin session requires csrf and cannot create roles."""
    headers = login(client, 'operator')
    assert client.get('/api/v1/admin/users?details=1').json()['items'][1]['role'] == 'admin'
    endpoint = '/api/v1/admin/users'
    assert client.post(endpoint, json={'login': 'Bob', 'password': 'secret'}).status_code == 403
    assert (
        client.post(
            endpoint, json={'login': 'Bob', 'password': 'secret'}, headers={**headers, 'Origin': 'https://evil.test'}
        ).status_code
        == 403
    )
    assert (
        client.post(endpoint, json={'login': 'Bob', 'password': 'secret', 'role': 'admin'}, headers=headers).status_code
        == 422
    )
    assert client.post(endpoint, json={'login': 'Bob', 'password': 'secret'}, headers=headers).status_code == 201
    assert client.get('/api/v1/admin/users', headers={'Authorization': 'Bearer invalid'}).status_code == 403


def test_password_reset_and_role_change_revoke_sessions(client: TestClient) -> None:
    """Verify password reset and role change revoke sessions."""
    headers = login(client)
    assert (
        client.put(
            '/api/v1/auth/password', json={'current_password': 'secret', 'new_password': 'changed'}, headers=headers
        ).status_code
        == 200
    )
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False
    admin_headers = login(client, 'operator')
    with pytest.raises(ViewerError):
        service_for(client).db.set_role('operator', 'user')
    service_for(client).db.create_user('second', 'secret', role='admin')
    service_for(client).db.set_role('operator', 'user')
    assert client.get('/api/v1/admin/users', headers=admin_headers).status_code == 401


@pytest.mark.parametrize('transport', ['cookie', 'bearer'])
@pytest.mark.parametrize('target', ['operator', 'second'])
def test_admin_profiles_protected_from_user_management(client: TestClient, transport: str, target: str) -> None:
    """Verify admin profiles protected from user management."""
    db = service_for(client).db
    db.create_user('second', 'secret', role='admin')
    login(client, target)
    db.issue(target, 'secret', 'publish')
    headers = login(client, 'operator') if transport == 'cookie' else {'Authorization': 'Bearer ' + 'A' * 32}
    before = dict(db.connection.execute('SELECT * FROM users WHERE login=?', (target,)).fetchone())
    sessions = db.connection.execute('SELECT COUNT(*) FROM auth_sessions WHERE login=?', (target,)).fetchone()[0]
    grants = db.connection.execute('SELECT COUNT(*) FROM grants WHERE login=?', (target,)).fetchone()[0]
    base = '/api/v1/admin/users/' + target
    for enabled in (False, True):
        response = client.patch(base, json={'enabled': enabled}, headers=headers)
        assert response.status_code == 403 and response.json()['error']['code'] == 'protected_admin'
    response = client.put(base + '/password', json={'password': 'replacement'}, headers=headers)
    assert response.status_code == 403 and response.json()['error']['code'] == 'protected_admin'
    assert dict(db.connection.execute('SELECT * FROM users WHERE login=?', (target,)).fetchone()) == before
    assert (
        db.connection.execute('SELECT COUNT(*) FROM auth_sessions WHERE login=?', (target,)).fetchone()[0] == sessions
    )
    assert db.connection.execute('SELECT COUNT(*) FROM grants WHERE login=?', (target,)).fetchone()[0] == grants
    # User management still controls ordinary users.
    assert (
        client.put('/api/v1/admin/users/Alice/password', json={'password': 'replacement'}, headers=headers).status_code
        == 200
    )
    assert client.patch('/api/v1/admin/users/Alice', json={'enabled': False}, headers=headers).status_code == 200


def test_user_promoted_during_password_hashing_cannot_be_reset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify user promoted during password hashing cannot be reset."""
    headers = login(client, 'operator')
    db = service_for(client).db
    original = database.password_hash

    def promote(password: str, salt: bytes | None = None) -> str:
        """Promote Alice while hashing her proposed replacement password."""
        hashed = original(password, salt)
        db.set_role('Alice', 'admin')
        return hashed

    monkeypatch.setattr(database, 'password_hash', promote)
    response = client.put('/api/v1/admin/users/Alice/password', json={'password': 'replacement'}, headers=headers)
    assert response.status_code == 403 and response.json()['error']['code'] == 'protected_admin'
    monkeypatch.setattr(database, 'password_hash', original)
    assert db.authenticate('Alice', 'secret')['role'] == 'admin'


def test_plain_login_is_case_sensitive_and_cross_site_login_fails(client: TestClient) -> None:
    """Verify plain login is case sensitive and cross site login fails."""
    token = client.get('/api/v1/auth/session').json()['csrf_token']
    assert (
        client.post(
            '/api/v1/auth/session',
            json={'login': 'Alice', 'password': 'secret'},
            headers={'Origin': 'https://evil.test', 'X-CSRF-Token': token},
        ).status_code
        == 403
    )
    assert (
        client.post(
            '/api/v1/auth/session',
            json={'login': 'alice', 'password': 'secret'},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': token},
        ).status_code
        == 401
    )


def test_expiry_disable_cookie_attributes_and_rate_limit(client: TestClient) -> None:
    """Verify expiry disable cookie attributes and rate limit."""
    headers = login(client, 'operator')
    cookies = client.cookies
    session = next(c for c in cookies.jar if c.name == 'prsim_session')
    assert session.secure and session.get_nonstandard_attr('HttpOnly') is not False
    db = service_for(client).db
    db.connection.execute('UPDATE auth_sessions SET expires=0')
    db.connection.commit()
    assert client.get('/api/v1/admin/users').status_code == 401
    login(client)
    db.update_user('Alice', enabled=False)
    assert client.get('/api/v1/auth/session').json()['authenticated'] is False
    for _ in range(10):
        assert (
            client.post(
                '/api/v1/auth/session', headers=headers, json={'login': 'missing', 'password': 'wrong'}
            ).status_code
            == 401
        )
    assert (
        client.post('/api/v1/auth/session', headers=headers, json={'login': 'missing', 'password': 'wrong'}).status_code
        == 429
    )
    assert (
        client.put(
            '/api/v1/admin/users/operator/role', headers={'Authorization': 'Bearer ' + 'A' * 32}, json={'role': 'admin'}
        ).status_code
        == 404
    )


def test_origin_requires_https_and_valid_port(tmp_path: Path) -> None:
    """Verify origin requires https and valid port."""
    for origin in ('http://viewer.example.test', 'https://viewer.example.test:bad', '', 123):
        path = tmp_path / 'config.json'
        path.write_text(json.dumps({'storage': 'mock', 'public_origin': origin}))
        with pytest.raises(ViewerError):
            Config.load(path)
    assert Config(storage='mock', public_origin='http://127.0.0.1:8765/').public_origin == 'http://127.0.0.1:8765'


def test_grant_and_browser_auth_share_failure_budget(client: TestClient) -> None:
    """Verify grant and browser auth share failure budget."""
    cfg = service_for(client).config
    challenge = client.get('/api/v1/auth/session').json()['csrf_token']
    for _ in range(cfg.login_attempts):
        assert (
            client.post('/api/v1/auth/publish-token', json={'login': 'Alice', 'password': 'wrong'}).status_code == 401
        )
    assert client.post('/api/v1/auth/publish-token', json={'login': 'Alice', 'password': 'secret'}).status_code == 429
    assert (
        client.post(
            '/api/v1/auth/session',
            json={'login': 'Alice', 'password': 'secret'},
            headers={'Origin': ORIGIN, 'X-CSRF-Token': challenge},
        ).status_code
        == 429
    )


def test_explicit_bearer_never_mutates_cookie_password_or_session(client: TestClient) -> None:
    """Verify explicit bearer never mutates cookie password or session."""
    headers = login(client)
    headers['Authorization'] = 'Bearer invalid'
    assert (
        client.put(
            '/api/v1/auth/password', json={'current_password': 'secret', 'new_password': 'changed'}, headers=headers
        ).status_code
        == 401
    )
    assert client.delete('/api/v1/auth/session', headers=headers).status_code == 401
    assert client.get('/api/v1/auth/session').json()['authenticated'] is True
    assert service_for(client).db.authenticate('Alice', 'secret')['role'] == 'user'


def test_local_provisioning_ignores_remote_url_and_never_calls_http(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify local provisioning ignores remote url and never calls http."""
    cfg = service_for(client).config
    monkeypatch.setenv('PRSIM_VIEWER_LOGIN', 'local-operator')
    monkeypatch.setenv('PRSIM_VIEWER_URL', 'https://remote.example.test')
    monkeypatch.setattr(cli, '_request', lambda *a, **k: pytest.fail('local provisioning attempted HTTP'))
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'secret')
    cli.administer('admin-profile-create', cfg)
    actor = service_for(client).db.actor('local-operator')
    assert actor is not None and actor['role'] == 'admin'
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'recovered')
    cli.administer('admin-profile-password-set', cfg)
    assert service_for(client).db.authenticate('local-operator', 'recovered')['role'] == 'admin'
    monkeypatch.setenv('PRSIM_VIEWER_LOGIN', 'Alice')
    monkeypatch.setenv('PRSIM_VIEWER_ROLE', 'admin')
    cli.administer('admin-role-set', cfg)
    actor = service_for(client).db.actor('Alice')
    assert actor is not None and actor['role'] == 'admin'


def test_password_check_admission_rejects_concurrent_work_without_queue(client: TestClient) -> None:
    """Verify password check admission rejects concurrent work without queue."""
    db = service_for(client).db
    original = db.authenticate
    running = threading.Event()
    release = threading.Event()
    responses = []

    def slow(login: object, password: object) -> AccountActor:
        """Hold authentication open until the concurrent request has been tested."""
        running.set()
        assert release.wait(3)
        return original(login, password)

    challenge = client.get('/api/v1/auth/session').json()['csrf_token']
    db.authenticate = slow
    thread = threading.Thread(
        target=lambda: responses.append(
            client.post('/api/v1/auth/publish-token', json={'login': 'Alice', 'password': 'secret'}).status_code
        )
    )
    thread.start()
    try:
        assert running.wait(3)
        assert (
            client.post(
                '/api/v1/auth/session',
                json={'login': 'Alice', 'password': 'secret'},
                headers={'Origin': ORIGIN, 'X-CSRF-Token': challenge},
            ).status_code
            == 429
        )
    finally:
        release.set()
        thread.join(3)
        db.authenticate = original
    assert not thread.is_alive() and responses == [200]


def test_invalid_login_payload_is_not_retained_in_limiter(client: TestClient) -> None:
    """Verify invalid login payload is not retained in limiter."""
    service = service_for(client)
    auth = BrowserAuth(service.db, service.config)
    request = Request({'type': 'http', 'client': ('127.0.0.1', 12345)})
    with pytest.raises(ViewerError):
        auth.authenticate(request, 'x' * 100_000, 'wrong')
    assert all(len(key[1]) <= 64 for key in auth.failures)


def test_full_limiter_keeps_current_keys_and_bounds_rejected_peers(client: TestClient) -> None:
    """Verify full limiter keeps current keys and bounds rejected peers."""
    service = service_for(client)
    auth = BrowserAuth(service.db, service.config)
    auth.failures[('login', '')] = []
    for i in range(2047):
        auth.failures[('peer', 'previous-' + str(i))] = []
    request = Request({'type': 'http', 'client': ('127.0.0.1', 12345)})
    with pytest.raises(ViewerError) as denied:
        auth.authenticate(request, 'x' * 100_000, 'wrong')
    assert denied.value.status == 401 and len(auth.failures) <= 2048
    auth.failures[('login', 'Alice')] = [service.db.clock()] * service.config.login_attempts
    for i in range(10):
        request = Request({'type': 'http', 'client': ('10.0.0.' + str(i), 12345)})
        with pytest.raises(ViewerError) as limited:
            auth.authenticate(request, 'Alice', 'wrong')
        assert limited.value.status == 429 and len(auth.failures) <= 2048


@pytest.mark.parametrize('operation', ['session', 'grant'])
def test_local_role_change_revokes_credentials_issued_during_verification(client: TestClient, operation: str) -> None:
    """Verify local role change revokes credentials issued during verification."""
    db = service_for(client).db
    other = Database(service_for(client).config.runtime)
    other.create_user('second', 'secret', role='admin')
    checked = threading.Event()
    attempting = threading.Event()
    changed = threading.Event()
    failures = []
    original = db.authenticate

    def authenticate(login: object, password: object) -> AccountActor:
        """Pause verified authentication while another connection attempts a role change."""
        actor = original(login, password)
        checked.set()
        assert attempting.wait(3)
        # Before the fix, the local writer could complete during this gap and
        # the subsequently inserted token escaped revocation.
        changed.wait(0.2)
        return actor

    db.authenticate = authenticate

    def demote() -> None:
        """Demote the administrator after credential verification starts."""
        try:
            assert checked.wait(3)
            attempting.set()
            other.set_role('operator', 'user')
            changed.set()
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=demote)
    thread.start()
    try:
        if operation == 'session':
            login(client, 'operator')
        else:
            assert (
                client.post('/api/v1/auth/publish-token', json={'login': 'operator', 'password': 'secret'}).status_code
                == 200
            )
        thread.join(3)
        assert not thread.is_alive() and not failures and changed.is_set()
        actor = db.actor('operator')
        assert actor is not None and actor['role'] == 'user'
        assert db.connection.execute('SELECT COUNT(*) FROM auth_sessions').fetchone()[0] == 0
        assert db.connection.execute('SELECT COUNT(*) FROM grants').fetchone()[0] == 0
    finally:
        db.authenticate = original
        other.close()
