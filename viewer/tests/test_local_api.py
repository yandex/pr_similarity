"""Local publication HTTP admission and immutable read response lifetimes."""

import asyncio
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import TracebackType
from typing import NoReturn

import httpx
import pytest
from helpers import FileCallback, archive_fixture
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.testclient import TestClient
from starlette.types import Message, Scope
from test_local_service import require_record
from viewer.bundles import BundleDescriptor, BundleIdentity
from viewer.config import Config, Limits
from viewer.errors import StorageError, ViewerError
from viewer.local_repository import LocalRecord, LocalRepository
from viewer.local_service import LocalViewerService, ReadLease
from viewer.persistence.database import Database
from viewer.persistence.storage import MemoryStore
from viewer.util import digest
from viewer.validation.archive import ImportedRun, validate_archive
from viewer.web import api
from viewer.web.api import PublicationResponse, create_app
from viewer.web.local_reads import ClosingStreamResponse, local_read


def get_local_service(client: TestClient) -> LocalViewerService:
    """Expose the actual local service after checking the ASGI fixture's concrete types."""
    assert isinstance(client.app, Starlette)
    service = client.app.state.service
    assert isinstance(service, LocalViewerService)
    return service


@pytest.mark.parametrize('disconnect', [False, True])
def test_local_read_lease_covers_stream_and_thread_context(disconnect: bool) -> None:
    """The lease reaches threadpool readers and closes after delivery or disconnect."""
    events = []

    class Lease:
        """Record the success flag supplied when a response releases its file lease."""

        def close(self, success: bool = True) -> None:
            """Record whether the response completed before releasing its lease."""
            events.append(('close', success))

    lease = Lease()
    selected: ContextVar[Lease | None] = ContextVar('selected', default=None)

    class Service:
        """Implement the read boundary used by the synthetic ASGI request."""

        async def acquire_read_async(self, run_id: str) -> Lease:
            """Return the test lease selected for this HTTP request."""
            events.append(('acquire', run_id))
            return lease

        @contextmanager
        def bind_read(self, lease: Lease) -> Iterator[None]:
            """Bind the selected lease for threadpool readers and streamed response bodies."""
            token = selected.set(lease)
            try:
                yield
            finally:
                selected.reset(token)

    async def handler(request: Request) -> Response:
        """Build the response only after the required read authorization and lease binding."""
        assert await run_in_threadpool(selected.get) is lease

        async def body() -> AsyncIterator[bytes]:
            """Yield fixture bytes while exercising the request or response lifetime."""
            assert selected.get() is lease
            assert not any(item[0] == 'close' for item in events)
            yield b'archive'

        return StreamingResponse(body())

    async def exercise() -> None:
        """Drive the ASGI response and verify its transport and cleanup events."""
        scope: Scope = {
            'type': 'http',
            'method': 'GET',
            'path': '/run',
            'headers': [],
            'path_params': {'run_id': 'run'},
            'asgi': {'spec_version': '2.4'},
        }
        response = await local_read(Service(), handler)(Request(scope))
        assert selected.get() is None
        assert events == [('acquire', 'run')]

        async def receive() -> Message:
            """Report client disconnect to the synthetic ASGI response."""
            return {'type': 'http.disconnect'}

        async def send(message: Message) -> None:
            """Record delivery and inject the selected transport failure."""
            if message['type'] == 'http.response.body' and message.get('body'):
                assert not any(item[0] == 'close' for item in events)
                if disconnect:
                    raise OSError('client gone')
                events.append(('body', message['body']))

        if disconnect:
            with pytest.raises(Exception):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)
        assert selected.get() is None

    asyncio.run(exercise())
    assert events[-1] == ('close', not disconnect)
    assert sum(event[0] == 'close' for event in events) == 1


def test_local_read_authorization_precedes_cold_load() -> None:
    """Rejected downloads cannot start cache restoration."""

    class Service:
        """Implement the read boundary used by the synthetic ASGI request."""

        async def acquire_read_async(self, run_id: str) -> ReadLease:
            """Return the test lease selected for this HTTP request."""
            pytest.fail('Unauthorized request started a cold load')

        def bind_read(self, lease: ReadLease) -> AbstractContextManager[None]:
            """Reject any attempt to bind a version before authorization succeeds."""
            pytest.fail('Unauthorized request attempted to bind a lease')

    async def denied(request: Request) -> None:
        """Reject authorization before a read can acquire its version."""
        raise PermissionError('Denied')

    async def handler(request: Request) -> Response:
        """Build the response only after the required read authorization and lease binding."""
        pytest.fail('Unauthorized request reached its handler')

    async def exercise() -> None:
        """Drive the ASGI response and verify its transport and cleanup events."""
        request = Request(
            {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'path_params': {'run_id': 'run'}}
        )
        with pytest.raises(PermissionError):
            await local_read(Service(), handler, denied)(request)

    asyncio.run(exercise())


@pytest.mark.parametrize('fail_at_start', [False, True])
def test_archive_body_closes_on_delivery_failure(fail_at_start: bool) -> None:
    """A broken transport closes the body even before the first chunk is requested."""
    closed = []

    async def exercise() -> None:
        """Drive the ASGI response and verify its transport and cleanup events."""

        async def chunks() -> AsyncIterator[bytes]:
            """Yield one archive chunk through the response lifecycle."""
            yield b'archive'

        response = ClosingStreamResponse(chunks(), lambda: closed.append(True))

        async def receive() -> Message:
            """Report client disconnect to the synthetic ASGI response."""
            return {'type': 'http.disconnect'}

        async def send(message: Message) -> None:
            """Record delivery and inject the selected transport failure."""
            if (message['type'] == 'http.response.start') == fail_at_start:
                raise OSError('transport closed')

        with pytest.raises(Exception):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)

    asyncio.run(exercise())
    assert closed == [True]


def test_archive_validation_reports_extracting_then_validating(tmp_path: Path) -> None:
    """Upload progress follows extraction and evidence validation boundaries."""
    path = tmp_path / 'archive.tar.gz'
    archive_fixture(path)
    phases = []
    validate_archive(path, tmp_path / 'data', Limits(), on_phase=phases.append)
    assert phases == ['extracting', 'validating']


@pytest.fixture
def local_client(tmp_path: Path) -> Iterator[TestClient]:
    """Run the real local service with isolated storage and low test disk budgets."""
    token = tmp_path / 'admin-token'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(
        runtime=tmp_path / 'runtime',
        admin_token_file=token,
        storage='mock',
        publication_storage='local',
        disk_reserve_bytes=1,
    )
    app = create_app(config, MemoryStore())
    app.state.service.db.create_user('alice', 'secret')
    app.state.service.db.create_user('operator', 'secret', role='admin')
    with TestClient(app) as client:
        yield client


def local_upload(
    client: TestClient,
    tmp_path: Path,
    *,
    run_id: str | None = None,
    grant: str | None = None,
    overwrite: bool = False,
    expected: str | None = None,
    configure: FileCallback | None = None,
    prepared: Callable[[dict, dict[str, str]], None] | None = None,
) -> tuple[httpx.Response, dict, dict[str, str], bytes]:
    """Publish fixture bytes with the negotiated local protocol and exact hashes."""
    path = tmp_path / (str(uuid.uuid4()) + '.tar.gz')
    report, manifest = archive_fixture(path, run_id=run_id or str(uuid.uuid4()), configure=configure)
    if grant is None:
        grant = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()['token']
    assert isinstance(grant, str)
    data = path.read_bytes()
    key = str(uuid.uuid4())
    headers = {
        'Authorization': 'Bearer ' + grant,
        'Content-Type': 'application/gzip',
        'X-Publication-Protocol': 'local-durable-v1',
        'Idempotency-Key': key,
        'X-Archive-SHA256': digest(data),
        'X-Report-SHA256': manifest['report_sha256'],
    }
    params = {'overwrite': '1' if overwrite else '0'}
    if expected is not None:
        params['expected_publication_sha256'] = expected
    if prepared is not None:
        prepared(report, headers)
    response = client.put('/api/v1/runs/' + report['run']['id'], content=data, headers=headers, params=params)
    return response, report, headers, data


def test_local_publication_receipt_status_replay_and_expired_grant(local_client: TestClient, tmp_path: Path) -> None:
    """A durable operation remains readable with its admitted grant after grant cleanup."""
    client = local_client
    capability = client.get('/api/v1/publication-capabilities')
    assert capability.json() == {'protocols': ['local-durable-v1'], 'operation_status': True}
    response, report, headers, data = local_upload(client, tmp_path)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt['protocol'] == 'local-durable-v1'
    assert receipt['durability'] == 'local_durable'
    assert receipt['phase'] == 'available'
    assert receipt['author_login'] == receipt['actor_login'] == 'alice'
    db = get_local_service(client).db
    with db.lock, db.connection:
        db.connection.execute('DELETE FROM grants')
    identity = {
        'run_id': report['run']['id'],
        'archive_sha256': headers['X-Archive-SHA256'],
        'report_sha256': headers['X-Report-SHA256'],
    }
    status = client.get(
        '/api/v1/publication-operations/' + headers['Idempotency-Key'],
        headers={'Authorization': headers['Authorization']},
        params=identity,
    )
    assert status.status_code == 200, status.text
    assert status.json()['phase'] == 'available'
    assert status.json()['receipt'] == receipt
    assert 'grant_hash' not in status.text and 'archive_path' not in status.text
    replay = client.put('/api/v1/runs/' + report['run']['id'], content=b'', headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == receipt
    wrong = client.get(
        '/api/v1/publication-operations/' + headers['Idempotency-Key'],
        headers={'Authorization': headers['Authorization']},
        params={**identity, 'archive_sha256': '0' * 64},
    )
    assert wrong.status_code == 409


def test_local_admin_overwrite_preserves_owner_and_rejects_nonadmin(local_client: TestClient, tmp_path: Path) -> None:
    """Administrative replacement uses a dedicated grant and preserves the original owner."""
    client = local_client
    denied = client.post('/api/v1/auth/admin-overwrite-token', json={'login': 'alice', 'password': 'secret'})
    assert denied.status_code == 403
    initial, report, headers, data = local_upload(client, tmp_path)
    assert initial.status_code == 201, initial.text
    grant = client.post('/api/v1/auth/admin-overwrite-token', json={'login': 'operator', 'password': 'secret'})
    assert grant.status_code == 200, grant.text

    def changed(report: dict, source: dict, files: dict[str, bytes]) -> None:
        """Change the uploaded agent name while retaining the original frozen basket."""
        report['agent']['name'] = 'Administrative replacement'

    replaced, _, _, _ = local_upload(
        client,
        tmp_path,
        run_id=report['run']['id'],
        grant=grant.json()['token'],
        overwrite=True,
        expected=headers['X-Archive-SHA256'],
        configure=changed,
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()['author_login'] == 'alice'
    assert replaced.json()['actor_login'] == 'operator'
    assert client.get('/api/v1/runs/' + report['run']['id']).json()['display_name'] == report['agent']['name']


def test_local_catalogue_does_not_acquire_read_and_archive_checks_auth_first(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only authorized detail reads acquire a version; catalogue reads stay local."""
    client = local_client
    uploaded, report, _, data = local_upload(client, tmp_path)
    assert uploaded.status_code == 201, uploaded.text
    service = get_local_service(client)
    acquired = []
    original = service.acquire_read_async

    async def acquire(run_id: str) -> ReadLease:
        """Record each admitted detail read before delegating to the local service."""
        acquired.append(run_id)
        return await original(run_id)

    monkeypatch.setattr(service, 'acquire_read_async', acquire)
    catalogue = client.get('/api/v1/baskets').json()
    basket = catalogue['items'][0]['hash']
    assert client.get('/api/v1/baskets/' + basket + '/runs').status_code == 200
    assert acquired == []
    base = '/api/v1/runs/' + report['run']['id']
    assert client.get(base + '/archive').status_code == 401
    assert acquired == []
    assert client.get(base).status_code == 200
    archive = client.get(base + '/archive', headers={'Authorization': 'Bearer ' + 'A' * 32})
    assert archive.status_code == 200
    assert archive.content == data
    assert acquired == [report['run']['id']] * 2


@pytest.mark.parametrize('send_failure', [False, True])
def test_backup_starts_after_final_response_send_attempt(send_failure: bool) -> None:
    """Response loss cannot strand committed backup intent in the running process."""
    events = []

    async def exercise() -> None:
        """Drive the ASGI response and verify its transport and cleanup events."""
        response = PublicationResponse({'phase': 'available'}, lambda: events.append('backup'))
        assert events == []

        async def receive() -> Message:
            """Report client disconnect to the synthetic ASGI response."""
            return {'type': 'http.disconnect'}

        async def send(message: Message) -> None:
            """Record delivery and inject the selected transport failure."""
            assert 'backup' not in events
            events.append(message['type'])
            if send_failure and message['type'] == 'http.response.body':
                raise OSError('response lost')

        if send_failure:
            with pytest.raises(OSError):
                await response({'type': 'http'}, receive, send)
        else:
            await response({'type': 'http'}, receive, send)

    asyncio.run(exercise())
    assert events == ['http.response.start', 'http.response.body', 'backup']


def test_local_publish_requires_protocol_before_reading_body(local_client: TestClient) -> None:
    """A legacy request cannot upload bytes to a local-only writer accidentally."""
    client = local_client
    grant = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()['token']
    body_read = []

    def body() -> Iterator[bytes]:
        """Yield fixture bytes while exercising the request or response lifetime."""
        body_read.append(True)
        yield b'unwanted upload'

    response = client.put(
        '/api/v1/runs/' + str(uuid.uuid4()),
        content=body(),
        headers={
            'Authorization': 'Bearer ' + grant,
            'Content-Type': 'application/gzip',
            'Idempotency-Key': str(uuid.uuid4()),
            'X-Archive-SHA256': 'a' * 64,
            'X-Report-SHA256': 'b' * 64,
        },
    )
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'publication_protocol_required'
    assert body_read == []


def test_archive_phase_callback_failure_removes_extraction(tmp_path: Path) -> None:
    """A failed durable progress update cannot leave an unowned extracted directory."""
    path = tmp_path / 'archive.tar.gz'
    archive_fixture(path)

    def phase(value: str) -> None:
        """Fail at validation to exercise cleanup of extracted artifacts."""
        if value == 'validating':
            raise RuntimeError('progress persistence failed')

    with pytest.raises(RuntimeError):
        validate_archive(path, tmp_path / 'data', Limits(), on_phase=phase)
    assert not (tmp_path / 'data').exists()


def test_legacy_factory_rejects_local_catalogue_before_storage_reads(tmp_path: Path) -> None:
    """An explicit legacy config cannot silently replace the durable local catalogue."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock')
    db = Database(config.runtime)
    LocalRepository(db)
    assert db.connection.execute('PRAGMA user_version').fetchone()[0] == 3
    db.close()

    class NoReads(MemoryStore):
        """Reject remote discovery when legacy startup should refuse a local catalogue."""

        def list(self, prefix: str = '') -> NoReturn:
            """Reject storage enumeration before the local-mode guard runs."""
            pytest.fail('Legacy startup attempted remote storage discovery')

    with pytest.raises(ViewerError) as error:
        create_app(config, NoReads())
    assert error.value.code == 'local_storage_mode_required'
    assert error.value.status == 503


@pytest.mark.parametrize('existing', [False, True])
def test_local_parallel_publication_and_status_stay_responsive(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    """A blocked validator leaves another upload and operation status responsive."""
    client = local_client
    run_id = None
    if existing:
        initial, report, _, _ = local_upload(client, tmp_path)
        assert initial.status_code == 201
        run_id = report['run']['id']
    entered, release = threading.Event(), threading.Event()
    original = api.validate_archive
    prepared = {}

    def remember(report: dict, headers: dict[str, str]) -> None:
        """Capture publication identity before the HTTP upload begins."""
        prepared.update(run_id=report['run']['id'], headers=headers)

    def validate(
        path: Path,
        destination: Path,
        limits: Limits,
        *,
        deadline: float | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> ImportedRun:
        """Pause validation until unrelated HTTP operations have been checked."""
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return original(path, destination, limits, deadline=deadline, on_phase=on_phase)

    monkeypatch.setattr(api, 'validate_archive', validate)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(local_upload, client, tmp_path, run_id=run_id, overwrite=existing, prepared=remember)
        try:
            assert entered.wait(5)
            headers = prepared['headers']
            status = pool.submit(
                client.get,
                '/api/v1/publication-operations/' + headers['Idempotency-Key'],
                headers={'Authorization': headers['Authorization']},
                params={
                    'run_id': prepared['run_id'],
                    'archive_sha256': headers['X-Archive-SHA256'],
                    'report_sha256': headers['X-Report-SHA256'],
                },
            ).result(timeout=5)
            assert status.status_code == 200, status.text
            assert status.json()['phase'] == 'extracting'
            admin = {'Authorization': 'Bearer ' + 'A' * 32}
            snapshot = pool.submit(client.get, '/api/v1/admin/stats', headers=admin).result(timeout=5)
            assert snapshot.status_code == 200
            uploads = [row for row in snapshot.json()['activity']['items'] if row['kind'] == 'upload']
            assert len(uploads) == 1
            assert uploads[0]['run_id'] == prepared['run_id']
            assert uploads[0]['status'] == 'extracting'
            assert uploads[0]['received_bytes'] == uploads[0]['total_bytes'] > 0
            assert 'grant_hash' not in str(uploads)
            blocked_read = pool.submit(client.get, '/api/v1/runs/' + prepared['run_id']).result(timeout=5)
            assert blocked_read.status_code == 503
            assert blocked_read.json()['error']['code'] == 'run_updating'
            second, _, _, _ = pool.submit(local_upload, client, tmp_path).result(timeout=10)
            assert second.status_code == 201, second.text
            assert not first.done()
        finally:
            release.set()
        completed, _, _, _ = first.result(timeout=10)
        assert completed.status_code == (200 if existing else 201), completed.text
        snapshot = client.get('/api/v1/admin/stats', headers=admin).json()
        assert not [row for row in snapshot['activity']['items'] if row['kind'] == 'upload']


def test_pinned_report_cache_miss_does_not_block_catalogue(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned detail read parses its report without occupying the catalogue lock."""
    client = local_client
    uploaded, report, _, _ = local_upload(client, tmp_path)
    assert uploaded.status_code == 201, uploaded.text
    service = get_local_service(client)
    assert not service._reports
    entered, release = threading.Event(), threading.Event()
    original = service.store.get

    def blocked(key: str, limit: int = 64 << 20) -> bytes:
        """Delay the selected storage operation until the test releases it."""
        if key.endswith('/data/report.json'):
            entered.set()
            assert release.wait(15)
        return original(key, limit)

    monkeypatch.setattr(service.store, 'get', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        detail = pool.submit(client.get, '/api/v1/runs/' + report['run']['id'] + '/tasks')
        try:
            assert entered.wait(5)
            catalogue = pool.submit(client.get, '/api/v1/baskets').result(timeout=5)
            assert catalogue.status_code == 200, catalogue.text
            assert not detail.done()
        finally:
            release.set()
        assert detail.result(timeout=5).status_code == 200


def test_late_cold_read_rejects_replaced_generation(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold waiter cannot return its old generation after an overwrite commits."""
    client = local_client
    service = get_local_service(client)
    initial, report, _, _ = local_upload(client, tmp_path)
    assert initial.status_code == 201
    service = get_local_service(client)
    run_id = report['run']['id']
    deadline = time.monotonic() + 5
    while require_record(service, run_id).backup_state != 'backed_up' and time.monotonic() < deadline:
        time.sleep(0.01)
    record = require_record(service, run_id)
    assert record.backup_state == 'backed_up' and record.backup_valid and record.backup_metadata_revision > 0
    assert record.local_path is not None
    assert service.files.evict(Path(record.local_path))
    assert service.repository.update_local_state(
        run_id, generation=record.generation, local_path=None, cache_state='cold', cache_bytes=0
    )
    restored, release = threading.Event(), threading.Event()
    original = service._restore

    def restore(value: LocalRecord) -> None:
        """Delay restore completion after the selected generation has been materialized."""
        original(value)
        restored.set()
        assert release.wait(15)

    monkeypatch.setattr(service, '_restore', restore)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cold = pool.submit(client.get, '/api/v1/runs/' + run_id)
        try:
            assert restored.wait(5)
            overwritten, _, headers, _ = pool.submit(
                local_upload, client, tmp_path, run_id=run_id, overwrite=True
            ).result(timeout=10)
            assert overwritten.status_code == 200, overwritten.text
        finally:
            release.set()
        stale = cold.result(timeout=5)
    assert stale.status_code == 409, stale.text
    assert stale.json()['error']['code'] == 'run_changed'
    current = client.get('/api/v1/runs/' + run_id)
    assert current.status_code == 200
    assert current.json()['publication_sha256'] == headers['X-Archive-SHA256']


@pytest.mark.parametrize('revocation', ['expired', 'role'])
def test_admin_overwrite_expired_or_revoked_grant_is_denied(
    local_client: TestClient, tmp_path: Path, revocation: str
) -> None:
    """Admission requires both the short grant lifetime and current admin role."""
    client = local_client
    initial, report, headers, _ = local_upload(client, tmp_path)
    assert initial.status_code == 201
    token = client.post('/api/v1/auth/admin-overwrite-token', json={'login': 'operator', 'password': 'secret'}).json()[
        'token'
    ]
    db = get_local_service(client).db
    if revocation == 'expired':
        with db.lock, db.connection:
            db.connection.execute(
                'UPDATE grants SET expires=? WHERE token_hash=?', (db.clock() - 1, digest(token.encode()))
            )
    else:
        db.create_user('keeper', 'secret', role='admin')
        db.set_role('operator', 'user')
    rejected, _, _, _ = local_upload(
        client, tmp_path, run_id=report['run']['id'], grant=token, overwrite=True, expected=headers['X-Archive-SHA256']
    )
    assert rejected.status_code == 401, rejected.text
    assert client.get('/api/v1/runs/' + report['run']['id']).json()['publication_sha256'] == headers['X-Archive-SHA256']


def test_admin_grant_cannot_admit_two_runs_concurrently(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first admission consumes the grant before a second UUID can reuse it."""
    client = local_client
    initial_a, report_a, headers_a, _ = local_upload(client, tmp_path)
    initial_b, report_b, headers_b, _ = local_upload(client, tmp_path)
    assert initial_a.status_code == initial_b.status_code == 201
    token = client.post('/api/v1/auth/admin-overwrite-token', json={'login': 'operator', 'password': 'secret'}).json()[
        'token'
    ]
    entered, release = threading.Event(), threading.Event()
    original = api.validate_archive

    def validate(
        path: Path,
        destination: Path,
        limits: Limits,
        *,
        deadline: float | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> ImportedRun:
        """Pause validation until unrelated HTTP operations have been checked."""
        entered.set()
        assert release.wait(15)
        return original(path, destination, limits, deadline=deadline, on_phase=on_phase)

    monkeypatch.setattr(api, 'validate_archive', validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            local_upload,
            client,
            tmp_path,
            run_id=report_a['run']['id'],
            grant=token,
            overwrite=True,
            expected=headers_a['X-Archive-SHA256'],
        )
        try:
            assert entered.wait(5)
            second, _, _, _ = pool.submit(
                local_upload,
                client,
                tmp_path,
                run_id=report_b['run']['id'],
                grant=token,
                overwrite=True,
                expected=headers_b['X-Archive-SHA256'],
            ).result(timeout=5)
            assert second.status_code == 401, second.text
        finally:
            release.set()
        completed, _, _, _ = first.result(timeout=10)
        assert completed.status_code == 200, completed.text


def test_refresh_resume_requires_admin_and_preserves_requested_plan(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume forwards the selected saved request without starting a fresh refresh."""
    client = local_client
    service = get_local_service(client)
    calls = []

    def resume(request_id: str) -> dict:
        """Return progress for the exact saved refresh plan selected by the request."""
        calls.append(('resume', request_id))
        return {'request_id': request_id, 'status': 'pending', 'phase': 'checking', 'checked': 4, 'total': 7}

    def fresh() -> dict:
        """Return a distinct discovery plan for a newly requested refresh."""
        calls.append(('fresh', None))
        return {'request_id': 'new-plan', 'status': 'pending', 'phase': 'discovery', 'checked': 0, 'total': 0}

    monkeypatch.setattr(service, 'resume_refresh', resume)
    monkeypatch.setattr(service, 'request_refresh', fresh)
    path = '/api/v1/admin/refresh/saved-plan/resume'
    denied = client.post(path, json={})
    assert denied.status_code == 401
    token = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()['token']
    denied = client.post(path, json={}, headers={'Authorization': 'Bearer ' + token})
    assert denied.status_code == 403
    assert calls == []
    admin = {'Authorization': 'Bearer ' + 'A' * 32}
    resumed = client.post(path, json={}, headers=admin)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['refresh_job']['request_id'] == 'saved-plan'
    assert resumed.json()['refresh_job']['checked'] == 4
    assert resumed.headers['cache-control'] == 'no-store'
    assert calls == [('resume', 'saved-plan')]
    invalid = client.post(path, json={'request_id': 'different'}, headers=admin)
    assert invalid.status_code == 422
    assert calls == [('resume', 'saved-plan')]
    started = client.post('/api/v1/admin/refresh', json={}, headers=admin)
    assert started.status_code == 200
    assert started.json()['refresh_job']['request_id'] == 'new-plan'
    assert calls == [('resume', 'saved-plan'), ('fresh', None)]


def test_checkpoint_request_requires_admin_and_queues_job(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoint creation is an administrator action with an empty request body."""
    client = local_client
    service = get_local_service(client)
    calls = []

    def request_checkpoint() -> dict:
        """Record checkpoint admission and return the queued job identity."""
        calls.append('queued')
        return {'request_id': 'checkpoint-test', 'status': 'pending', 'error_code': None, 'manifest_key': None}

    monkeypatch.setattr(service, 'request_checkpoint', request_checkpoint, raising=False)
    path = '/api/v1/admin/checkpoint'
    assert client.post(path, json={}).status_code == 401
    token = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()['token']
    assert client.post(path, json={}, headers={'Authorization': 'Bearer ' + token}).status_code == 403
    assert calls == []
    admin = {'Authorization': 'Bearer ' + 'A' * 32}
    invalid = client.post(path, json={'path': '/arbitrary'}, headers=admin)
    assert invalid.status_code == 422
    assert calls == []
    response = client.post(path, json={}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()['checkpoint_job']['request_id'] == 'checkpoint-test'
    assert response.json()['checkpoint_job']['status'] == 'pending'
    assert response.headers['cache-control'] == 'no-store'
    assert calls == ['queued']


@pytest.mark.parametrize('reject_archive', [False, True])
def test_upload_reservation_outlives_staging_until_cleanup(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject_archive: bool
) -> None:
    """Admission reserves extraction capacity and releases it after temp cleanup on both paths."""
    service = get_local_service(local_client)
    original_admit, original_release = service.admit_publication, service.release_upload
    calls = []

    def admit(
        actor: str,
        key: str,
        rid: str,
        archive_sha256: str,
        report_sha256: str,
        *,
        overwrite: bool = False,
        admin: bool = False,
        expected_publication_sha256: str | None = None,
        grant_token: str | None = None,
        reserve_upload: bool = False,
    ) -> dict:
        """Verify that HTTP upload admission reserves temporary extraction capacity."""
        assert reserve_upload is True
        result = original_admit(
            actor,
            key,
            rid,
            archive_sha256,
            report_sha256,
            overwrite=overwrite,
            admin=admin,
            expected_publication_sha256=expected_publication_sha256,
            grant_token=grant_token,
            reserve_upload=reserve_upload,
        )
        assert service.files.reserved > 0
        calls.append('admitted')
        return result

    def release(actor: str, key: str) -> None:
        """Check staging cleanup before releasing the upload reservation."""
        assert list(service.staging.glob('upload-*')) == []
        assert service.files.reserved > 0
        calls.append('released')
        original_release(actor, key)

    def invalid(*args: object, **kwargs: object) -> NoReturn:
        """Reject validation while its extraction capacity is still reserved."""
        assert service.files.reserved > 0
        raise ViewerError('invalid_archive', 'Invalid archive.')

    monkeypatch.setattr(service, 'admit_publication', admit)
    monkeypatch.setattr(service, 'release_upload', release)
    if reject_archive:
        monkeypatch.setattr(api, 'validate_archive', invalid)
    response, _, _, _ = local_upload(local_client, tmp_path)
    assert response.status_code == (422 if reject_archive else 201), response.text
    assert calls == ['admitted', 'released']
    assert service.files.reserved == 0


def test_checkpoint_slow_storage_does_not_block_http_ack_or_stats(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint worker may wait on storage while the HTTP caller receives its job ID."""
    client = local_client
    service = get_local_service(client)
    entered, release = threading.Event(), threading.Event()
    original = service.remote.put_file

    def blocked(key: str, path: str | Path, *, deadline: float | None = None) -> None:
        """Delay the selected storage operation until the test releases it."""
        entered.set()
        assert release.wait(15)
        return original(key, path, deadline=deadline)

    monkeypatch.setattr(service.remote, 'put_file', blocked)
    admin = {'Authorization': 'Bearer ' + 'A' * 32}
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.post, '/api/v1/admin/checkpoint', json={}, headers=admin)
        try:
            assert entered.wait(5)
            response = pending.result(timeout=5)
            assert response.status_code == 200, response.text
            job_id = response.json()['checkpoint_job']['request_id']
            assert job_id
            stats = pool.submit(client.get, '/api/v1/admin/stats', headers=admin).result(timeout=5)
            assert stats.status_code == 200
            assert stats.json()['checkpoint_job']['request_id'] == job_id
            assert stats.json()['checkpoint_job']['status'] == 'running'
            repeated = pool.submit(client.post, '/api/v1/admin/checkpoint', json={}, headers=admin).result(timeout=5)
            assert repeated.json()['checkpoint_job']['request_id'] == job_id
        finally:
            release.set()
    deadline = time.monotonic() + 5
    while service.checkpoint_job['status'] in ('pending', 'running') and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.checkpoint_job['status'] == 'succeeded'
    assert isinstance(service.remote, MemoryStore)
    assert service.checkpoint_job['manifest_key'] in service.remote.objects


def test_upload_capacity_is_checked_before_receiving_bytes(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worst-case extraction capacity is rejected before consuming a grant or request body."""
    client = local_client
    service = get_local_service(client)
    token = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()['token']
    usage = shutil.disk_usage(service.staging)
    peak = 3 * service.config.limits.archive_bytes + 2 * service.config.limits.unpacked_bytes
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: usage._replace(free=peak - 1))
    received = []

    def body() -> Iterator[bytes]:
        """Yield fixture bytes while exercising the request or response lifetime."""
        received.append(True)
        yield b'not accepted'

    response = client.put(
        '/api/v1/runs/' + str(uuid.uuid4()),
        content=body(),
        headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/gzip',
            'X-Publication-Protocol': 'local-durable-v1',
            'Idempotency-Key': str(uuid.uuid4()),
            'X-Archive-SHA256': 'a' * 64,
            'X-Report-SHA256': 'b' * 64,
        },
    )
    assert response.status_code == 503, response.text
    assert response.json()['error']['code'] == 'local_capacity'
    assert received == []
    assert service.db.authorize(token, 'publish') == 'alice'
    assert service.files.reserved == 0
    assert service.busy_runs() == set()


def test_failed_staging_cleanup_releases_prediction_and_warns_safely(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Residual files do not leak the reservation; later admission still checks free disk."""
    service = get_local_service(local_client)
    original = shutil.rmtree
    residual = []

    def keep_upload(
        path: str | Path,
        ignore_errors: bool = False,
        onerror: (
            Callable[[Callable[..., object], str, tuple[type[BaseException], BaseException, TracebackType]], object]
            | None
        ) = None,
        *,
        onexc: Callable[[Callable[..., object], str, BaseException], object] | None = None,
        dir_fd: int | None = None,
    ) -> None:
        """Retain upload staging to test cleanup diagnostics and reservation release."""
        if Path(path).name.startswith('upload-'):
            residual.append(Path(path))
        elif onexc is not None:
            original(path, ignore_errors, onexc=onexc, dir_fd=dir_fd)
        else:
            original(path, ignore_errors, onerror, dir_fd=dir_fd)

    monkeypatch.setattr(shutil, 'rmtree', keep_upload)
    try:
        response, _, _, _ = local_upload(local_client, tmp_path)
        assert response.status_code == 201, response.text
        assert residual and residual[0].exists()
        assert service.files.reserved == 0
        warnings = [record.getMessage() for record in caplog.records if record.name == 'viewer.upload']
        assert warnings == ['upload_cleanup_incomplete operation_id=' + response.json()['operation_id']]
        usage = shutil.disk_usage(service.staging)
        monkeypatch.setattr(shutil, 'disk_usage', lambda path: usage._replace(free=0))
        with pytest.raises(ViewerError) as error:
            with service.files.reserve(1):
                pytest.fail('Insufficient free disk admitted another reservation')
        assert error.value.code == 'local_capacity'
    finally:
        for path in residual:
            original(path, ignore_errors=True)


def test_cold_read_checks_restore_capacity_before_remote_download(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold HTTP request cannot start unpacking without room for staging and installed copies."""
    client = local_client
    service = get_local_service(client)
    response, report, _, _ = local_upload(client, tmp_path)
    assert response.status_code == 201, response.text
    run_id = report['run']['id']
    deadline = time.monotonic() + 5
    while require_record(service, run_id).backup_state != 'backed_up' and time.monotonic() < deadline:
        time.sleep(0.01)
    record = require_record(service, run_id)
    assert record.backup_state == 'backed_up'
    assert record.local_path is not None
    assert service.files.evict(Path(record.local_path))
    assert service.repository.update_local_state(
        run_id, generation=record.generation, local_path=None, cache_state='cold', cache_bytes=0
    )
    peak = 2 * (record.entry['marker']['archive_size'] + service.config.limits.unpacked_bytes)
    usage = shutil.disk_usage(service.staging)
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: usage._replace(free=peak - 1))

    def download(identity: BundleIdentity) -> BundleDescriptor:
        """Reject remote access before cold-restore disk admission."""
        pytest.fail('Cold read accessed remote storage without disk admission')

    monkeypatch.setattr(service.bundles, 'read_bundle', download)
    failed = client.get('/api/v1/runs/' + run_id)
    assert failed.status_code == 503, failed.text
    assert failed.json()['error']['code'] == 'local_capacity'
    assert require_record(service, run_id).local_path is None
    assert service.files.reserved == 0
    assert list(service.files.temporary.iterdir()) == []


def test_backup_retry_requires_admin_and_selected_run(
    local_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit backup retry authorizes administrators and binds the selected UUID."""
    client = local_client
    service = get_local_service(client)
    calls = []

    def retry(run_id: str) -> dict:
        """Record the selected run and return its newly scheduled backup count."""
        calls.append(run_id)
        return {'run_id': run_id, 'scheduled': 1}

    monkeypatch.setattr(service, 'retry_backup', retry, raising=False)
    path = '/api/v1/admin/runs/selected-run/backup/retry'
    assert client.post(path, json={}).status_code == 401
    owner = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()['token']
    assert client.post(path, json={}, headers={'Authorization': 'Bearer ' + owner}).status_code == 403
    assert calls == []
    admin = {'Authorization': 'Bearer ' + 'A' * 32}
    assert client.post(path, json={'run_id': 'other'}, headers=admin).status_code == 422
    assert calls == []
    response = client.post(path, json={}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json() == {'run_id': 'selected-run', 'scheduled': 1}
    assert response.headers['cache-control'] == 'no-store'
    assert calls == ['selected-run']


def test_admin_retry_recovers_blocked_backup_after_storage_access_is_fixed(
    local_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanent storage failure stays blocked until an explicit administrator retry."""
    client = local_client
    service = get_local_service(client)
    forbidden = True
    writes = []
    original = service.remote.put

    def put(key: str, data: bytes) -> None:
        """Simulate denied storage writes until access is explicitly restored."""
        writes.append(key)
        if forbidden:
            raise StorageError(retryable=False)
        return original(key, data)

    monkeypatch.setattr(service.remote, 'put', put)
    uploaded, report, headers, _ = local_upload(client, tmp_path)
    assert uploaded.status_code == 201, uploaded.text
    run_id = report['run']['id']
    deadline = time.monotonic() + 5
    while require_record(service, run_id).backup_state != 'blocked' and time.monotonic() < deadline:
        time.sleep(0.01)
    assert require_record(service, run_id).backup_state == 'blocked'
    assert writes
    forbidden = False
    count = len(writes)
    time.sleep(1.1)
    assert require_record(service, run_id).backup_state == 'blocked'
    assert len(writes) == count
    path = '/api/v1/admin/runs/' + run_id + '/backup/retry'
    admin = {'Authorization': 'Bearer ' + 'A' * 32}
    retried = client.post(path, json={}, headers=admin)
    assert retried.status_code == 200, retried.text
    assert retried.json() == {'run_id': run_id, 'scheduled': 1}
    deadline = time.monotonic() + 5
    while require_record(service, run_id).backup_state != 'backed_up' and time.monotonic() < deadline:
        time.sleep(0.01)
    record = require_record(service, run_id)
    assert record.backup_state == 'backed_up' and record.backup_valid
    assert record.backup_manifest_key
    assert client.get('/api/v1/runs/' + run_id).json()['publication_sha256'] == headers['X-Archive-SHA256']
    count = len(writes)
    again = client.post(path, json={}, headers=admin)
    assert again.status_code == 200
    assert again.json() == {'run_id': run_id, 'scheduled': 0}
    assert len(writes) == count
