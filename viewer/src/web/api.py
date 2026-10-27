"""HTTP ownership/auth boundaries and bounded transfer admission."""

import asyncio
import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.database import Actor, Database
from viewer.persistence.storage import MockFileStore, S3Store, Store, StoredBody
from viewer.presentation.config_view import public_config
from viewer.presentation.queries import (
    Query,
    TaskQuery,
    basket_runs,
    catalogue,
    file_diff,
    find_task,
    public_files,
    public_task,
)
from viewer.service import ViewerService
from viewer.util import parse_json
from viewer.validation.archive import validate_archive
from viewer.validation.contracts import require
from viewer.web.auth import SESSION_COOKIE, BrowserAuth
from viewer.web.middleware import SecurityHeaders

HEX = re.compile(r'[0-9a-f]{64}\Z')


def admin_token(path: Path) -> str:
    """Read a private owner-only administrative token without following symlinks."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            mode = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(mode.st_mode) and stat.S_IMODE(mode.st_mode) == 0o400 and mode.st_uid == os.getuid(),
                'unsafe_admin_token',
            )
            value = stream.read(4097).decode('ascii').strip()
        require(32 <= len(value) <= 4096 and not any(c.isspace() for c in value), 'unsafe_admin_token')
        return value
    except (OSError, UnicodeError):
        raise ViewerError(
            'unsafe_admin_token', 'Admin token needs an owner-only regular file with mode 0400.'
        ) from None


def bearer(request: Request) -> str:
    """Extract the bearer credential from the authorization header."""
    value = request.headers.get('authorization', '')
    return value[7:] if value.startswith('Bearer ') else ''


def create_app(config: Config, store: Store | None = None, clock: Callable[[], float] | None = None) -> Starlette:
    """Create one application with its own database, admission locks and handlers."""
    return _Application(config, store, clock).build()


class _Application:
    """Own request handlers and the private resources shared by one application."""

    def __init__(self, config: Config, store: Store | None = None, clock: Callable[[], float] | None = None) -> None:
        """Initialize the resources and limits owned by this instance."""
        self.config, self.store, self.clock = config, store, clock
        self.token = admin_token(self.config.admin_token_file)
        self.db = Database(
            self.config.runtime, ttl=self.config.grant_ttl_seconds, **({'clock': self.clock} if self.clock else {})
        )
        self.store = self.store or (
            MockFileStore(self.config.runtime / 'mock-storage')
            if self.config.storage == 'mock'
            else S3Store(self.config)
        )
        try:
            self.service = ViewerService(self.config, self.store, self.db)
        except Exception:
            self.db.close()
            raise
        self.writes = asyncio.Lock()
        self.browser = BrowserAuth(self.db, self.config)
        self.password_checks = asyncio.Semaphore(self.config.password_checks)

    async def authenticate_perform[T](self, function: Callable[[], T]) -> T:
        # Bound admission rather than accumulating an unbounded hash-work queue.
        """Bound concurrent password checks before running the protected callback."""
        if self.password_checks.locked():
            raise ViewerError('login_busy', 'Password verification is busy; retry.', 429)
        async with self.password_checks:
            return await self.perform(function)

    async def perform[T](self, function: Callable[[], T]) -> T:
        """Run a callback under the service lock after recovering durable operations."""

        def invoke() -> T:
            """Recover pending operations and invoke the caller under the service lock."""
            with self.service.lock:
                if self.db.operations():
                    self.service.recover()
                    self.service.refresh()
                return function()

        return await run_in_threadpool(invoke)

    async def body(
        self, request: Request, allowed: set[str], required: set[str] | None = None, optional: bool = False
    ) -> dict:
        """Read a bounded JSON body and validate its required and optional keys."""
        raw = bytearray()
        async with asyncio.timeout(self.config.limits.transfer_seconds):
            async for data in request.stream():
                if len(raw) + len(data) > self.config.limits.api_body_bytes:
                    raise ViewerError('body_limit', 'JSON request exceeds limit.', 413)
                raw.extend(data)
        if not raw and optional:
            return {}
        value = parse_json(bytes(raw))
        if not isinstance(value, dict):
            raise ViewerError('invalid_body', 'Uploaded documents or artifacts are inconsistent.')
        require(
            not set(value) - set(allowed) and set(allowed if required is None else required) <= set(value),
            'invalid_body',
        )
        return value

    def require_admin(self, request: Request) -> None:
        """Require an administrative bearer token or an authorized browser session."""
        if 'authorization' in request.headers:
            if hmac.compare_digest(bearer(request), self.token):
                return
            raise ViewerError('admin_denied', 'Administrative authorization required.', 403)
        actor = self.browser.session(request)
        if actor is None:
            raise ViewerError('auth_required', 'Authentication required.', 401)
        if actor['role'] != 'admin':
            raise ViewerError('admin_denied', 'Administrative authorization required.', 403)
        if request.method not in ('GET', 'HEAD'):
            self.browser.check_csrf(request)

    def archive_actor(self, request: Request, rid: str) -> tuple[Actor, bool]:
        """Resolve the authorized actor for an archive download."""
        if 'authorization' in request.headers:
            if hmac.compare_digest(bearer(request), self.token):
                return {'login': None, 'role': 'admin'}, False
            login = self.db.authorize(bearer(request), 'download', rid)
            actor = self.db.actor(login)
            if actor is None:
                raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
            return actor, True
        actor = self.browser.session(request)
        if actor is None:
            raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
        if actor['role'] != 'admin':
            raise ViewerError('foreign_run', 'Only the owner grant or administrator can download.', 403)
        return actor, False

    async def session(self, request: Request) -> Response:
        """Read or update the browser session according to the HTTP method."""
        if 'authorization' in request.headers:
            raise ViewerError('invalid_grant', 'This route requires browser credentials.', 401)
        if request.method == 'POST':
            value = await self.body(request, {'login', 'password'})
            session_token, result = await self.authenticate_perform(
                lambda: self.browser.login(request, value['login'], value['password'])
            )
        elif request.method == 'DELETE':
            result = await self.perform(lambda: self.browser.logout(request))
        else:
            result = await self.perform(lambda: self.browser.info(request))
        response = JSONResponse(result)
        self.browser.csrf_cookie(response, result['csrf_token'], request)
        if request.method == 'POST':
            response.set_cookie(
                SESSION_COOKIE,
                session_token,
                max_age=self.config.session_ttl_seconds,
                httponly=True,
                samesite='lax',
                secure=self.browser.secure(request),
                path='/',
            )
        elif request.method == 'DELETE':
            response.delete_cookie(SESSION_COOKIE, path='/')
        return response

    async def own_password(self, request: Request) -> Response:
        """Change the current browser user password after verifying CSRF and credentials."""
        if 'authorization' in request.headers:
            raise ViewerError('invalid_grant', 'This route requires browser credentials.', 401)
        value = await self.body(request, {'current_password', 'new_password'})

        def change() -> dict:
            """Apply the authorized mutation while holding the service lock."""
            self.browser.check_csrf(request)
            actor = self.browser.session(request)
            if actor is None:
                raise ViewerError('auth_required', 'Authentication required.', 401)
            self.browser.authenticate(request, actor['login'], value['current_password'])
            self.db.set_password(actor['login'], value['new_password'])
            return {'changed': True}

        response = JSONResponse(await self.authenticate_perform(change))
        response.delete_cookie(SESSION_COOKIE, path='/')
        return response

    async def health(self, request: Request) -> Response:
        """Return a health response without accessing publication storage."""
        return JSONResponse({'status': 'ok'})

    async def auth(self, request: Request) -> Response:
        """Issue a single-operation grant after verifying credentials and request limits."""
        operation = request.path_params['operation']
        if operation not in ('publish', 'download', 'metadata'):
            raise ViewerError('not_found', 'Resource not found.', 404)
        fields = {'login', 'password'} | ({'run_id'} if operation != 'publish' else set())
        value = await self.body(request, fields)
        if operation != 'publish':
            require(isinstance(value['run_id'], str), 'invalid_run_id')

        def issue() -> dict:
            # Authenticate first, before revealing any owner/run information.
            """Create a short-lived grant for one authenticated operation."""
            grant = self.db.issue(
                value['login'],
                value['password'],
                operation,
                value.get('run_id'),
                verify=lambda login, password: self.browser.authenticate(request, login, password),
            )
            if operation != 'publish':
                try:
                    actor = self.db.actor(value['login'])
                    if actor is None:
                        raise ViewerError('auth_denied', 'Invalid credentials.', 401)
                    if operation == 'download' and actor['role'] == 'admin':
                        self.service._require_run(value['run_id'])
                    else:
                        self.service.require_owner(value['run_id'], value['login'])
                except ViewerError:
                    self.db.consume(grant['token'])
                    raise
            return grant

        return JSONResponse(await self.authenticate_perform(issue))

    async def get_baskets(self, request: Request) -> Response:
        """Return the filtered and sorted basket catalogue."""
        query = Query(request.query_params)
        return JSONResponse(await self.perform(lambda: catalogue(self.service, query)))

    async def get_basket_runs(self, request: Request) -> Response:
        """Return runs belonging to the selected frozen basket."""
        query = Query(request.query_params)
        return JSONResponse(await self.perform(lambda: basket_runs(self.service, request.path_params['hash'], query)))

    async def get_run(self, request: Request) -> Response:
        """Return the public run projection with the requested metric selection."""
        query = Query(request.query_params)
        return JSONResponse(
            await self.perform(lambda: self.service.run(request.path_params['run_id'], **query.selection))
        )

    async def get_tasks(self, request: Request) -> Response:
        """Return the selected run tasks with sorting and pagination."""
        query = TaskQuery(request.query_params)
        rid = request.path_params['run_id']

        def read() -> dict:
            """Read a bounded chunk and enforce the configured transfer limits."""
            sha = self.service.check_report(
                rid,
                request.query_params.get('expected_report_sha256'),
                request.query_params.get('expected_publication_sha256'),
            )
            rows = [public_task(t, query.file) for t in self.service.full_report(rid).tasks]
            return {
                **query.page(query.sorted(rows)),
                'sort': query.sort,
                'direction': query.direction,
                'report_sha256': sha,
                'publication_sha256': self.service._require_run(rid)['marker']['archive_sha256'],
            }

        return JSONResponse(await self.perform(read))

    async def get_config(self, request: Request) -> Response:
        """Return the validated public native configuration snapshot."""
        return JSONResponse(
            await self.perform(
                lambda: public_config(
                    self.service,
                    request.path_params['run_id'],
                    request.query_params.get('expected_report_sha256'),
                    request.query_params.get('expected_publication_sha256'),
                )
            )
        )

    async def get_task(self, request: Request) -> Response:
        """Return one task and its selected execution evidence."""
        query = Query(request.query_params)

        def read() -> dict:
            """Read a bounded chunk and enforce the configured transfer limits."""
            self.service.check_report(
                request.path_params['run_id'],
                request.query_params.get('expected_report_sha256'),
                request.query_params.get('expected_publication_sha256'),
            )
            _, task = find_task(self.service, request.path_params['run_id'], request.path_params['task_id'])
            return {**public_task(task, query.file), 'condition': task.agent_task, 'files': public_files(task)}

        return JSONResponse(await self.perform(read))

    async def get_file(self, request: Request) -> Response:
        """Return the source-role diff for one validated task file."""

        def read() -> dict:
            """Read a bounded chunk and enforce the configured transfer limits."""
            self.service.check_report(
                request.path_params['run_id'],
                request.query_params.get('expected_report_sha256'),
                request.query_params.get('expected_publication_sha256'),
            )
            return file_diff(
                self.service,
                request.path_params['run_id'],
                request.path_params['task_id'],
                request.path_params['file_id'],
            )

        return JSONResponse(await self.perform(read))

    async def pinned(self, request: Request) -> Response:
        """Change run pinning after administrative authorization."""
        value = await self.body(request, {'pinned'})

        def change() -> dict:
            """Apply the authorized mutation while holding the service lock."""
            self.require_admin(request)
            return self.service.set_pinned(request.path_params['run_id'], value['pinned'])

        return JSONResponse(await self.perform(change))

    async def metadata(self, request: Request) -> Response:
        """Update the run owner display metadata."""
        rid = request.path_params['run_id']
        value = await self.body(request, {'display_name', 'tags'}, required=set())

        def update() -> dict:
            """Apply the validated settings change under the service lock."""
            login = self.db.authorize(bearer(request), 'metadata', rid)
            self.service.require_owner(rid, login)
            self.service.validate_patch(value)
            self.db.consume(bearer(request))
            return self.service.patch_metadata(rid, login, value)

        return JSONResponse(await self.perform(update))

    async def publish(self, request: Request) -> Response:
        """Validate and publish an uploaded archive under the existing ownership rules."""
        rid = request.path_params['run_id']
        try:
            require(str(uuid.UUID(rid)) == rid, 'invalid_run_id')
        except ValueError:
            raise ViewerError('invalid_run_id', 'Invalid run ID.') from None
        overwrite = request.query_params.get('overwrite', '0')
        require(overwrite in ('0', '1'), 'invalid_overwrite')
        key = request.headers.get('idempotency-key', '')
        require(bool(key) and len(key) <= 128 and all(32 <= ord(c) < 127 for c in key), 'invalid_idempotency_key')
        archive_hash = request.headers.get('x-archive-sha256', '')
        report_hash = request.headers.get('x-report-sha256', '')
        require(bool(HEX.fullmatch(archive_hash)) and bool(HEX.fullmatch(report_hash)), 'invalid_digest')
        require(request.headers.get('content-type', '').split(';')[0] == 'application/gzip', 'invalid_content_type')
        # TTL is checked once at admission, not after a long upload.
        login = await self.perform(lambda: self.db.authorize(bearer(request), 'publish'))
        deadline = time.monotonic() + self.config.limits.transfer_seconds
        async with self.writes:
            if shutil.disk_usage(self.service.staging).free < self.config.limits.staging_free_bytes:
                raise ViewerError('staging_space', 'Insufficient staging space.', 503)
            root = Path(tempfile.mkdtemp(prefix='upload-', dir=self.service.staging))
            try:
                path = root / 'archive.tar.gz'
                hasher = hashlib.sha256()
                size = 0
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    with path.open('xb') as output:
                        os.chmod(path, 0o600)
                        async for data in request.stream():
                            size += len(data)
                            if size > self.config.limits.archive_bytes:
                                raise ViewerError('archive_limit', 'Compressed archive exceeds limit.', 413)
                            output.write(data)
                            hasher.update(data)
                        output.flush()
                        os.fsync(output.fileno())
                require(hasher.hexdigest() == archive_hash, 'archive_digest_mismatch')
                imported = await run_in_threadpool(
                    validate_archive, path, root / 'data', self.config.limits, deadline=deadline
                )
                require(
                    imported.report.run.id == rid and imported.manifest.report_sha256 == report_hash,
                    'publish_identity_mismatch',
                )
                existed = await self.perform(
                    lambda: rid in self.service.runs or self.db.receipt(login, key) is not None
                )
                result = await self.perform(
                    lambda: self.service.publish(
                        imported, login, key, overwrite=overwrite == '1', grant_token=bearer(request), deadline=deadline
                    )
                )
                return JSONResponse(result, status_code=200 if existed else 201)
            finally:
                shutil.rmtree(root, ignore_errors=True)

    async def download(self, request: Request) -> Response:
        """Return an authorized archive stream with bounded transfer lifetime."""
        rid = request.path_params['run_id']

        def admit() -> tuple[StoredBody, dict]:
            """Authorize the download and capture its immutable archive marker."""
            actor, consume = self.archive_actor(request, rid)
            if actor['role'] == 'admin':
                row = self.service._require_run(rid)
            else:
                login = actor['login']
                assert login is not None
                row = self.service.require_owner(rid, login)
            stream = self.service.store.open(row['prefix'] + 'archive.tar.gz')
            if consume:
                self.db.consume(bearer(request))
            return stream, dict(row['marker'])

        stream, marker = await self.perform(admit)
        deadline = time.monotonic() + self.config.limits.transfer_seconds

        async def chunks() -> AsyncIterator[bytes]:
            """Yield bounded response chunks until the stream is exhausted."""
            hasher = hashlib.sha256()
            size = 0
            try:
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError('Download deadline exceeded')
                    data = await run_in_threadpool(stream.read, 64 << 10)
                    if not data:
                        break
                    size += len(data)
                    hasher.update(data)
                    if size > marker['archive_size']:
                        raise OSError('Stored archive size differs')
                    yield data
                if size != marker['archive_size'] or hasher.hexdigest() != marker['archive_sha256']:
                    raise OSError('Stored archive digest differs')
            finally:
                await run_in_threadpool(stream.close)

        headers = {
            'Content-Length': str(marker['archive_size']),
            'X-Archive-SHA256': marker['archive_sha256'],
            'Content-Disposition': 'attachment; filename="' + rid + '.tar.gz"',
            'Cache-Control': 'no-store',
        }
        return StreamingResponse(chunks(), media_type='application/gzip', headers=headers)

    async def users(self, request: Request) -> Response:
        """List users or create an account after administrative authorization."""
        if request.method == 'GET':
            query = Query(request.query_params)
            details = request.query_params.get('details', '0')
            if details not in ('0', '1'):
                raise ViewerError('invalid_query', 'Invalid account detail selection.')

            def read() -> dict:
                """Read a bounded chunk and enforce the configured transfer limits."""
                self.require_admin(request)
                return query.page(self.db.list_users(details == '1'))

            return JSONResponse(await self.perform(read))
        value = await self.body(request, {'login', 'password'})

        def create() -> dict:
            """Create the authorized account and return its public attributes."""
            self.require_admin(request)
            return self.db.create_user(value['login'], value['password'])

        return JSONResponse(await self.perform(create), status_code=201)

    async def user_access(self, request: Request) -> Response:
        """Change account access without disabling the final administrator."""
        value = await self.body(request, {'enabled'})

        def update() -> dict:
            """Apply the validated settings change under the service lock."""
            self.require_admin(request)
            return self.db.update_user(request.path_params['login'], enabled=value['enabled'], users_only=True)

        return JSONResponse(await self.perform(update))

    async def user_password(self, request: Request) -> Response:
        """Reset a managed account password after administrative authorization."""
        value = await self.body(request, {'password'})

        def update() -> dict:
            """Apply the validated settings change under the service lock."""
            self.require_admin(request)
            return self.db.set_password(request.path_params['login'], value['password'], users_only=True)

        return JSONResponse(await self.perform(update))

    async def basket_order(self, request: Request) -> Response:
        """Update the configured order of visible baskets."""
        value = await self.body(request, {'items'})

        def update() -> dict:
            """Apply the validated settings change under the service lock."""
            self.require_admin(request)
            return self.service.set_order(value['items'])

        return JSONResponse(await self.perform(update))

    async def delete_run(self, request: Request) -> Response:
        """Delete a run after administrative authorization."""

        def remove() -> dict:
            """Delete the authorized run using its current publication identity."""
            self.require_admin(request)
            return self.service.delete(request.path_params['run_id'])

        return JSONResponse(await self.perform(remove))

    async def delete_owned_run(self, request: Request) -> Response:
        """Authorize the owner before recovering or deleting the run."""

        def remove() -> dict:
            # Authorize the browser action before triggering any durable recovery.
            """Delete the authorized run using its current publication identity."""
            with self.service.lock:
                actor = self.browser.session(request)
                if actor is None:
                    raise ViewerError('auth_required', 'Authentication required.', 401)
                self.browser.check_csrf(request)
                return self.service.delete(
                    request.path_params['run_id'],
                    actor=actor,
                    publication=request.query_params.get('expected_publication_sha256'),
                )

        return JSONResponse(await run_in_threadpool(remove))

    async def layout(self, request: Request) -> Response:
        """Read or update the persisted basket layout."""
        value = await self.body(request, {'expected_revision', 'items'}) if request.method == 'PUT' else None

        def read_or_write() -> dict:
            """Read settings or apply an authorized revision-checked update."""
            self.require_admin(request)
            if value is None:
                return self.service.settings()
            require(isinstance(value['items'], list), 'invalid_basket_display')
            order = []
            display = {}
            for item in value['items']:
                require(
                    isinstance(item, dict)
                    and set(item) == {'hash', 'hidden', 'collapsed'}
                    and isinstance(item['hash'], str),
                    'invalid_basket_display',
                )
                order.append(item['hash'])
                display[item['hash']] = {k: item[k] for k in ('hidden', 'collapsed')}
            return self.service.set_settings(value['expected_revision'], order, display)

        return JSONResponse(await self.perform(read_or_write))

    async def cache_stats(self, request: Request) -> Response:
        """Describe the current publication cache and storage coverage."""

        def read() -> dict:
            """Read a bounded chunk and enforce the configured transfer limits."""
            self.require_admin(request)
            return self.service.cache_stats()

        return JSONResponse(await self.perform(read))

    async def refresh(self, request: Request) -> Response:
        """Refresh the catalogue while retaining the previous snapshot on failure."""
        await self.body(request, set(), optional=True)

        def update() -> dict:
            """Apply the validated settings change under the service lock."""
            self.require_admin(request)
            return self.service.refresh()

        return JSONResponse(await self.perform(update))

    async def asset(self, request: Request) -> Response:
        """Serve only explicitly allowed packaged UI assets."""
        name = request.path_params.get('name', 'index.html')
        allowed = {
            'index.html': 'text/html',
            'app.js': 'text/javascript',
            'account.js': 'text/javascript',
            'settings.js': 'text/javascript',
            'config.js': 'text/javascript',
            'style.css': 'text/css',
            'favicon.svg': 'image/svg+xml',
        }
        if name not in allowed:
            raise ViewerError('not_found', 'Resource not found.', 404)
        try:
            content = files('viewer').joinpath('static', name).read_bytes()
        except FileNotFoundError:
            return Response('UI implementation in progress', media_type='text/plain', status_code=503)
        return Response(content, media_type=allowed[name])

    async def error(self, request: Request, exc: Exception) -> JSONResponse:
        """Translate application failures to safe JSON errors."""
        if isinstance(exc, ViewerError):
            status, code, message = exc.status, exc.code, exc.message
        elif isinstance(exc, HTTPException):
            status, code, message = (
                exc.status_code,
                'not_found' if exc.status_code == 404 else 'method_not_allowed',
                'Resource or method is unavailable.',
            )
        elif isinstance(exc, TimeoutError):
            status, code, message = 413, 'transfer_timeout', 'Request deadline exceeded.'
        else:
            status, code, message = 503, 'operation_failed', 'Operation could not be completed; retry.'
        return JSONResponse(
            {'error': {'code': code, 'message': message}},
            status_code=status,
            headers={'Retry-After': '1'} if status == 503 else None,
        )

    @asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        """Close the application database after the request-serving lifetime."""
        yield
        self.db.close()

    def build(self) -> Starlette:
        """Build routes and exception handlers for this application owner."""
        routes = [
            Route('/healthz', self.health),
            Route('/', self.asset),
            Route('/run', self.asset),
            Route('/settings', self.asset),
            Route('/admin/users', self.asset),
            Route('/assets/{name}', self.asset),
            Route('/api/v1/auth/session', self.session, methods=['GET', 'POST', 'DELETE']),
            Route('/api/v1/auth/password', self.own_password, methods=['PUT']),
            Route('/api/v1/auth/{operation}-token', self.auth, methods=['POST']),
            Route('/api/v1/baskets', self.get_baskets),
            Route('/api/v1/baskets/{hash}/runs', self.get_basket_runs),
            Route('/api/v1/runs/{run_id}/config', self.get_config),
            Route('/api/v1/runs/{run_id}', self.get_run, methods=['GET']),
            Route('/api/v1/runs/{run_id}', self.publish, methods=['PUT']),
            Route('/api/v1/runs/{run_id}', self.delete_owned_run, methods=['DELETE']),
            Route('/api/v1/runs/{run_id}/tasks', self.get_tasks),
            Route('/api/v1/runs/{run_id}/tasks/{task_id}', self.get_task),
            Route('/api/v1/runs/{run_id}/tasks/{task_id}/files/{file_id}', self.get_file),
            Route('/api/v1/runs/{run_id}/pinned', self.pinned, methods=['PUT']),
            Route('/api/v1/runs/{run_id}/metadata', self.metadata, methods=['PATCH']),
            Route('/api/v1/runs/{run_id}/archive', self.download),
            Route('/api/v1/admin/users', self.users, methods=['GET', 'POST']),
            Route('/api/v1/admin/users/{login}', self.user_access, methods=['PATCH']),
            Route('/api/v1/admin/users/{login}/password', self.user_password, methods=['PUT']),
            Route('/api/v1/admin/baskets/order', self.basket_order, methods=['PUT']),
            Route('/api/v1/admin/runs/{run_id}', self.delete_run, methods=['DELETE']),
            Route('/api/v1/admin/baskets/layout', self.layout, methods=['GET', 'PUT']),
            Route('/api/v1/admin/stats', self.cache_stats),
            Route('/api/v1/admin/refresh', self.refresh, methods=['POST']),
        ]
        app = Starlette(
            routes=routes,
            lifespan=self.lifespan,
            middleware=[Middleware(SecurityHeaders)],
            exception_handlers={
                ViewerError: self.error,
                HTTPException: self.error,
                TimeoutError: self.error,
                OSError: self.error,
                sqlite3.Error: self.error,
            },
        )
        app.state.service = self.service
        return app
