"""HTTP ownership/auth boundaries and bounded transfer admission."""

import asyncio
import hashlib
import hmac
import html
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from importlib.resources import files
from pathlib import Path

import anyio
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send
from viewer import __version__
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.local_service import LocalViewerService
from viewer.models.report import ReportTask
from viewer.monitoring import ActivityTracker, activity_snapshot
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
    task_changes,
)
from viewer.resources import ResourceSnapshots, local_resources
from viewer.service import CatalogueEntry, ViewerService
from viewer.util import digest, parse_json
from viewer.validation.archive import ImportedRun, validate_archive
from viewer.validation.contracts import require
from viewer.web.auth import SESSION_COOKIE, BrowserAuth
from viewer.web.cache import conditional_json
from viewer.web.local_reads import ClosingStreamResponse, local_read
from viewer.web.middleware import SecurityHeaders, StartupGate

HEX = re.compile(r'[0-9a-f]{64}\Z')


class PublicationResponse(JSONResponse):
    """Activate durable backup intent after the response send attempt finishes."""

    def __init__(self, value: dict, activate: Callable[[], None], *, status_code: int = 200) -> None:
        """Bind a committed receipt to its deferred backup activation."""
        self.activated = False
        self.activate = activate
        super().__init__(value, status_code=status_code, background=BackgroundTask(self.start_backup))

    def start_backup(self) -> None:
        """Start the idempotent backup dispatcher after local commit and send."""
        self.activate()
        self.activated = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Activate after delivery or after the transport rejects the response."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self.activated:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(self.start_backup)


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


def create_app(
    config: Config,
    store: Store | None = None,
    clock: Callable[[], float] | None = None,
    *,
    deferred_startup: bool = False,
    dev: bool = False,
) -> Starlette:
    """Create one application with its own database, admission locks and handlers."""
    return _Application(config, store, clock, deferred_startup=deferred_startup, dev=dev).build()


class _Application:
    """Own request handlers and the private resources shared by one application."""

    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        clock: Callable[[], float] | None = None,
        *,
        deferred_startup: bool = False,
        dev: bool = False,
    ) -> None:
        """Create account storage, publication services and browser authentication."""
        self.build_label = 'v' + __version__ + (' [dev]' if dev else '')
        self.config, self.store, self.clock = config, store, clock
        self.token = admin_token(self.config.admin_token_file)
        self.db = Database(
            self.config.runtime,
            ttl=self.config.grant_ttl_seconds,
            **({'clock': self.clock} if self.clock else {}),
        )
        if (
            config.publication_storage == 'legacy'
            and self.db.connection.execute(
                'SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=\'local_heads\''
            ).fetchone()
        ):
            self.db.close()
            raise ViewerError(
                'local_storage_mode_required',
                'This runtime contains a local catalogue; enable local publication storage.',
                503,
            )
        self.store = self.store or (
            MockFileStore(self.config.runtime / 'mock-storage')
            if self.config.storage == 'mock'
            else S3Store(self.config)
        )
        try:
            if config.publication_storage == 'local':
                self.service = LocalViewerService(self.config, self.store, self.db, initialize=not deferred_startup)
            else:
                self.service = ViewerService(self.config, self.store, self.db, initialize=not deferred_startup)
        except Exception:
            self.db.close()
            raise
        self.uploads = ActivityTracker(clock=self.db.clock)
        self.resources = ResourceSnapshots(config, self.store)
        self.writes = asyncio.Lock()
        self.publication_slots = asyncio.Semaphore(config.publish_workers)
        self.browser = BrowserAuth(self.db, self.config)
        self.password_checks = asyncio.Semaphore(self.config.password_checks)

    def _local_service(self) -> 'LocalViewerService':
        """Narrow handlers whose routes require the local storage mode."""
        assert isinstance(self.service, LocalViewerService)
        return self.service

    async def authenticate_perform[T](self, function: Callable[[], T]) -> T:
        # Bound admission rather than accumulating an unbounded hash-work queue.
        """Bound concurrent password checks before running the protected callback."""
        if self.password_checks.locked():
            raise ViewerError('login_busy', 'Password verification is busy; retry.', 429)
        async with self.password_checks:
            return await self.perform(function)

    async def perform[T](self, function: Callable[[], T]) -> T:
        """Run a short callback under the service lock without triggering recovery."""

        def invoke() -> T:
            """Reject shutdown before invoking the caller under the service lock."""
            if getattr(self.service, 'read_is_pinned', lambda: False)():
                if self.service.maintenance.stopping:
                    raise ViewerError('service_stopping', 'Service is stopping.', 503)
                return function()
            with self.service.lock:
                if self.service.maintenance.stopping:
                    raise ViewerError('service_stopping', 'Service is stopping.', 503)
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
        session_token: str | None = None
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
            assert session_token is not None
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

    async def ready(self, request: Request) -> Response:
        """Report startup progress without waiting on storage or the writer lock."""
        state = dict(self.service.startup)
        if self.service.maintenance.stopping:
            state.update(status='stopping', phase='shutdown')
        return JSONResponse(state, status_code=200 if state['status'] == 'ready' else 503)

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

    async def catalogue_result(self, request: Request, build: Callable[[], dict]) -> Response:
        """Revalidate a catalogue projection cached by query and current publication state."""

        def read() -> dict:
            """Build or reuse the projection while holding the short catalogue lock."""
            cache = getattr(self.service, 'catalogue_response', None)
            if cache is None:
                return build()
            key = (request.url.path, tuple(sorted(request.query_params.multi_items())))
            return cache(key, build)

        return conditional_json(request, await self.perform(read))

    async def get_baskets(self, request: Request) -> Response:
        """Return the filtered and sorted basket catalogue."""
        query = Query(request.query_params)
        return await self.catalogue_result(request, lambda: catalogue(self.service, query))

    async def get_basket_runs(self, request: Request) -> Response:
        """Return runs belonging to the selected frozen basket."""
        query = Query(request.query_params)
        return await self.catalogue_result(
            request, lambda: basket_runs(self.service, request.path_params['hash'], query)
        )

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
            """Validate report identity and project the sorted, paginated task list."""
            sha = self.service.check_report(
                rid,
                request.query_params.get('expected_report_sha256'),
                request.query_params.get('expected_publication_sha256'),
            )
            rows = [
                public_task(t, query.file, self.service._require_run(rid)['measurements'])
                for t in self.service.full_report(rid).tasks
            ]
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
        if request.query_params.get('view') == 'changes':
            rid = request.path_params['run_id']

            def snapshot() -> tuple[CatalogueEntry, ReportTask, str, str]:
                """Capture the selected task and publication identities under the service lock."""
                sha = self.service.check_report(
                    rid,
                    request.query_params.get('expected_report_sha256'),
                    request.query_params.get('expected_publication_sha256'),
                )
                run, task = find_task(self.service, rid, request.path_params['task_id'])
                return run, task, sha, run['marker']['archive_sha256']

            run, task, sha, publication = await self.perform(snapshot)
            try:
                changes = await run_in_threadpool(task_changes, self.service, run, task)
            except ViewerError:
                await self.perform(lambda: self.service.check_report(rid, sha, publication))
                raise
            await self.perform(lambda: self.service.check_report(rid, sha, publication))
            return JSONResponse(changes)

        query = Query(request.query_params)

        def read() -> dict:
            """Validate report identity and project the selected task and its files."""
            self.service.check_report(
                request.path_params['run_id'],
                request.query_params.get('expected_report_sha256'),
                request.query_params.get('expected_publication_sha256'),
            )
            row, task = find_task(self.service, request.path_params['run_id'], request.path_params['task_id'])
            return {
                **public_task(task, query.file, row['measurements']),
                'condition': task.agent_task,
                'files': public_files(task),
            }

        return JSONResponse(await self.perform(read))

    async def get_file(self, request: Request) -> Response:
        """Return the source-role diff for one validated task file."""

        def read() -> dict:
            """Validate report identity before reading the requested file diff."""
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
                full=request.query_params.get('view') == 'full',
            )

        return JSONResponse(await self.perform(read))

    async def pinned(self, request: Request) -> Response:
        """Change run pinning after administrative authorization."""
        value = await self.body(request, {'pinned'})

        await self.perform(lambda: self.require_admin(request))
        return JSONResponse(
            await run_in_threadpool(lambda: self.service.set_pinned(request.path_params['run_id'], value['pinned']))
        )

    async def metadata(self, request: Request) -> Response:
        """Update display metadata with a one-use owner or administrative grant."""
        rid = request.path_params['run_id']
        value = await self.body(request, {'display_name', 'tags'}, required=set())

        def update() -> dict:
            """Authorize and persist metadata under writer admission."""
            with self.service.write_lock:
                with self.service.lock:
                    if self.service.maintenance.stopping:
                        raise ViewerError('service_stopping', 'Service is stopping.', 503)
                    login, admin = self.metadata_actor(request, rid)
                    if admin:
                        self.service._require_run(rid)
                    else:
                        self.service.require_owner(rid, login)
                    self.service.validate_patch(value)
                    self.service.check_write_admission()
                    self.db.consume(bearer(request))
                if admin:
                    return self._local_service().patch_metadata(rid, login, value, admin=True)
                return self.service.patch_metadata(rid, login, value)

        return JSONResponse(await run_in_threadpool(update))

    def metadata_actor(self, request: Request, rid: str) -> tuple[str, bool]:
        """Resolve an unused metadata grant without publication replay privileges."""
        token = bearer(request)
        try:
            return self.db.authorize(token, 'metadata', rid), False
        except ViewerError:
            if self.config.publication_storage != 'local':
                raise
        login = self.db.authorize(token, 'admin-overwrite')
        actor = self.db.actor(login)
        if actor is None or actor['role'] != 'admin':
            raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
        return login, True

    async def capabilities(self, request: Request) -> Response:
        """Advertise the publication protocol implemented by this storage mode."""
        local = self.config.publication_storage == 'local'
        return JSONResponse({'protocols': ['local-durable-v1'] if local else [], 'operation_status': local})

    async def admin_overwrite_token(self, request: Request) -> Response:
        """Issue a one-use administrative replacement or metadata grant after role checks."""
        if self.config.publication_storage != 'local':
            raise ViewerError('not_found', 'Resource not found.', 404)
        value = await self.body(request, {'login', 'password'})

        def issue() -> dict:
            """Create the grant in the same transaction as role verification."""

            def verify(login: str, password: str) -> None:
                """Authenticate before revealing administrative authorization."""
                self.browser.authenticate(request, login, password)
                actor = self.db.actor(login)
                if actor is None or actor['role'] != 'admin':
                    raise ViewerError('admin_denied', 'Administrative authorization required.', 403)

            return self.db.issue(
                value['login'],
                value['password'],
                'admin-overwrite',
                verify=verify,
                ttl_seconds=self.config.admin_overwrite_token_ttl_seconds,
            )

        return JSONResponse(await self.authenticate_perform(issue))

    def publication_actor(self, request: Request, key: str, *, status: bool = False) -> tuple[str, bool]:
        """Resolve current grants or the exact token admitted for a saved operation."""
        token = bearer(request)
        if not token or len(token) > 1024:
            raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
        for operation in ('publish', 'admin-overwrite'):
            try:
                login = self.db.authorize(token, operation)
            except ViewerError:
                continue
            actor = self.db.actor(login)
            if actor is None or (operation == 'admin-overwrite' and actor['role'] != 'admin'):
                raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
            return login, operation == 'admin-overwrite'
        found = self._local_service().repository.operation_by_grant(key, digest(token.encode()))
        if found is not None:
            login, operation = found
            actor = self.db.actor(login)
            admin = bool(operation.get('admin'))
            if actor is not None and (not admin or actor['role'] == 'admin'):
                if status or operation.get('phase') == 'available':
                    return login, admin
        raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)

    async def publication_status(self, request: Request) -> Response:
        """Read an actor-bound operation using its complete publication identity."""
        if self.config.publication_storage != 'local':
            raise ViewerError('not_found', 'Resource not found.', 404)
        key = request.path_params['key']
        identity = {
            field: request.query_params.get(field, '') for field in ('run_id', 'archive_sha256', 'report_sha256')
        }
        require(
            bool(HEX.fullmatch(identity['archive_sha256'])) and bool(HEX.fullmatch(identity['report_sha256'])),
            'invalid_digest',
        )

        def read() -> dict:
            """Validate operation credentials before reading its public status."""
            login, _ = self.publication_actor(request, key, status=True)
            operation = self._local_service().repository.operation(login, key)
            actor = self.db.actor(login)
            assert actor is not None
            if operation and operation.get('admin') and actor['role'] != 'admin':
                raise ViewerError('admin_denied', 'Administrative authorization required.', 403)
            return self._local_service().publication_status(login, key, **identity)

        return JSONResponse(await self.perform(read))

    async def authorize_download(self, request: Request) -> None:
        """Check archive access before a cold load; admission checks it again later."""

        def authorize() -> None:
            """Check the current account and immutable archive ownership."""
            rid = request.path_params['run_id']
            actor, _ = self.archive_actor(request, rid)
            if actor['role'] == 'admin':
                self.service._require_run(rid)
            else:
                login = actor['login']
                assert login is not None
                self.service.require_owner(rid, login)

        await self.perform(authorize)

    async def receive_local_archive(
        self,
        request: Request,
        root: Path,
        archive_hash: str,
        deadline: float,
        *,
        progress: Callable[[int], None] | None = None,
    ) -> Path:
        """Receive bounded archive bytes and fsync them before saving the durable upload copy."""
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
                    await run_in_threadpool(output.write, data)
                    hasher.update(data)
                    if progress is not None:
                        progress(size)
                await run_in_threadpool(output.flush)
                await run_in_threadpool(os.fsync, output.fileno())
        require(hasher.hexdigest() == archive_hash, 'archive_digest_mismatch')
        return path

    async def finish_local_upload(
        self,
        service: LocalViewerService,
        login: str,
        key: str,
        root: Path | None,
        operation_id: str | None,
        error: BaseException | None,
    ) -> None:
        """Record failure before releasing staging and reservations on every exit path."""
        with anyio.CancelScope(shield=True):
            try:
                if error is not None:
                    await run_in_threadpool(service.publication_failed, login, key, error)
            finally:
                try:
                    if root is not None:
                        await run_in_threadpool(shutil.rmtree, root, ignore_errors=True)
                        if root.exists():
                            logging.getLogger('viewer.upload').warning(
                                'upload_cleanup_incomplete operation_id=%s',
                                operation_id,
                            )
                finally:
                    await run_in_threadpool(service.release_upload, login, key)

    def validate_local_upload(
        self,
        service: LocalViewerService,
        login: str,
        key: str,
        operation_id: str,
        path: Path,
        root: Path,
        deadline: float,
    ) -> ImportedRun:
        """Preserve durable upload phases while validating outside the service lock."""

        def phase(value: str) -> None:
            """Record the durable phase before updating its optional display observation."""
            service.publication_phase(login, key, value)
            self.uploads.update(operation_id, phase=value)

        self.uploads.update(operation_id, phase='extracting')
        service.publication_phase(login, key, 'extracting', archive_path=str(path))
        return validate_archive(path, root / 'data', self.config.limits, deadline=deadline, on_phase=phase)

    async def publish_local(
        self,
        request: Request,
        rid: str,
        key: str,
        archive_hash: str,
        report_hash: str,
        overwrite: bool,
    ) -> Response:
        """Admit and validate a local publication without the global upload lock."""
        if request.headers.get('x-publication-protocol') != 'local-durable-v1':
            raise ViewerError('publication_protocol_required', 'Use publication protocol local-durable-v1.', 409)
        service = self._local_service()
        expected = request.query_params.get('expected_publication_sha256')
        if expected is not None:
            require(bool(HEX.fullmatch(expected)), 'invalid_digest')
        login, admin = await self.perform(lambda: self.publication_actor(request, key))
        if admin and (not overwrite or expected is None):
            raise ViewerError(
                'admin_overwrite_required',
                'Administrative overwrite requires overwrite and expected publication hash.',
                409,
            )
        if self.publication_slots.locked():
            raise ViewerError('publication_busy', 'Publication workers are busy; retry.', 503)
        async with self.publication_slots:
            status = await run_in_threadpool(
                lambda: service.admit_publication(
                    login,
                    key,
                    rid,
                    archive_hash,
                    report_hash,
                    overwrite=overwrite,
                    admin=admin,
                    expected_publication_sha256=expected,
                    grant_token=bearer(request),
                    reserve_upload=True,
                )
            )
            if status.get('phase') == 'available':
                return PublicationResponse(status['receipt'], lambda: service.activate_backup(rid))
            deadline = time.monotonic() + self.config.limits.transfer_seconds
            root = None
            completed = False
            operation_id = status['operation_id']
            declared = request.headers.get('content-length', '')
            total_bytes = int(declared) if declared.isdecimal() and len(declared) <= 20 else None
            self.uploads.start(operation_id, run_id=rid, actor=login, total_bytes=total_bytes)
            try:
                if shutil.disk_usage(service.staging).free < self.config.limits.staging_free_bytes:
                    raise ViewerError('staging_space', 'Insufficient staging space.', 503)
                root = Path(tempfile.mkdtemp(prefix='upload-', dir=service.staging))
                path = await self.receive_local_archive(
                    request,
                    root,
                    archive_hash,
                    deadline,
                    progress=lambda size: self.uploads.update(operation_id, received_bytes=size),
                )
                imported = await run_in_threadpool(
                    self.validate_local_upload, service, login, key, operation_id, path, root, deadline
                )
                require(
                    imported.report.run.id == rid and imported.manifest.report_sha256 == report_hash,
                    'publish_identity_mismatch',
                )
                existed = await self.perform(lambda: rid in service.runs)
                self.uploads.update(operation_id, phase='committing')
                result = await run_in_threadpool(
                    lambda: service.publish(
                        imported,
                        login,
                        key,
                        overwrite=overwrite,
                        grant_token=bearer(request),
                        deadline=deadline,
                        admin=admin,
                        expected_publication_sha256=expected,
                    )
                )
                response = PublicationResponse(
                    result,
                    lambda: service.activate_backup(rid),
                    status_code=200 if existed else 201,
                )
                completed = True
                return response
            finally:
                try:
                    await self.finish_local_upload(
                        service,
                        login,
                        key,
                        root,
                        status.get('operation_id'),
                        sys.exception() if not completed else None,
                    )
                finally:
                    self.uploads.finish(operation_id)

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
        require(
            request.headers.get('content-type', '').split(';')[0] == 'application/gzip',
            'invalid_content_type',
        )
        if self.config.publication_storage == 'local':
            return await self.publish_local(request, rid, key, archive_hash, report_hash, overwrite == '1')
        # TTL is checked once at admission, not after a long upload.
        login = await self.perform(lambda: self.db.authorize(bearer(request), 'publish'))
        deadline = time.monotonic() + self.config.limits.transfer_seconds
        async with self.writes:
            if shutil.disk_usage(self.service.staging).free < self.config.limits.staging_free_bytes:
                raise ViewerError('staging_space', 'Insufficient staging space.', 503)
            root = Path(tempfile.mkdtemp(prefix='upload-', dir=self.service.staging))
            activity_id = str(uuid.uuid4())
            length = request.headers.get('content-length', '')
            total_bytes = int(length) if length.isdecimal() and len(length) <= 20 else None
            self.uploads.start(activity_id, run_id=rid, actor=login, total_bytes=total_bytes)
            try:
                path = root / 'archive.tar.gz'
                hasher = hashlib.sha256()
                size = 0
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    with path.open('xb') as output:
                        os.chmod(path, 0o600)
                        async for data in request.stream():
                            size += len(data)
                            self.uploads.update(activity_id, received_bytes=size)
                            if size > self.config.limits.archive_bytes:
                                raise ViewerError('archive_limit', 'Compressed archive exceeds limit.', 413)
                            output.write(data)
                            hasher.update(data)
                        output.flush()
                        os.fsync(output.fileno())
                require(hasher.hexdigest() == archive_hash, 'archive_digest_mismatch')
                imported = await run_in_threadpool(
                    validate_archive,
                    path,
                    root / 'data',
                    self.config.limits,
                    deadline=deadline,
                    on_phase=lambda phase: self.uploads.update(activity_id, phase=phase),
                )
                require(
                    imported.report.run.id == rid and imported.manifest.report_sha256 == report_hash,
                    'publish_identity_mismatch',
                )
                existed = await self.perform(
                    lambda: rid in self.service.runs or self.db.receipt(login, key) is not None
                )
                self.uploads.update(activity_id, phase='committing')
                result = await run_in_threadpool(
                    lambda: self.service.publish(
                        imported,
                        login,
                        key,
                        overwrite=overwrite == '1',
                        grant_token=bearer(request),
                        deadline=deadline,
                    )
                )
                return JSONResponse(result, status_code=200 if existed else 201)
            finally:
                self.uploads.finish(activity_id)
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
            self.service._require_available(rid)
            with ExitStack() as admission:
                stream = admission.enter_context(self.service.store.open(row['prefix'] + 'archive.tar.gz'))
                if consume:
                    self.db.consume(bearer(request))
                marker = dict(row['marker'])
                # Streaming owns the body only after admission has completed.
                admission.pop_all()
                return stream, marker

        stream, marker = await self.perform(admit)
        deadline = time.monotonic() + self.config.limits.transfer_seconds

        async def chunks() -> AsyncIterator[bytes]:
            """Yield bounded response chunks until the stream is exhausted."""
            hasher = hashlib.sha256()
            size = 0
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

        headers = {
            'Content-Length': str(marker['archive_size']),
            'X-Archive-SHA256': marker['archive_sha256'],
            'Content-Disposition': 'attachment; filename="' + rid + '.tar.gz"',
            'Cache-Control': 'no-store',
        }
        return ClosingStreamResponse(chunks(), stream.close, media_type='application/gzip', headers=headers)

    async def users(self, request: Request) -> Response:
        """List users or create an account after administrative authorization."""
        if request.method == 'GET':
            query = Query(request.query_params)
            details = request.query_params.get('details', '0')
            if details not in ('0', '1'):
                raise ViewerError('invalid_query', 'Invalid account detail selection.')

            def read() -> dict:
                """Authorize the administrator and return the requested page of user accounts."""
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

        await self.perform(lambda: self.require_admin(request))
        return JSONResponse(await run_in_threadpool(lambda: self.service.set_order(value['items'])))

    async def delete_run(self, request: Request) -> Response:
        """Delete a run after administrative authorization."""
        await self.perform(lambda: self.require_admin(request))
        return JSONResponse(await run_in_threadpool(lambda: self.service.delete(request.path_params['run_id'])))

    async def delete_owned_run(self, request: Request) -> Response:
        """Authorize the owner before admitting a serialized deletion."""

        def remove() -> dict:
            # Authentication stays inside the short shared lock; deletion owns the writer lock.
            """Delete the authorized run using its current publication identity."""
            with self.service.lock:
                if self.service.maintenance.stopping:
                    raise ViewerError('service_stopping', 'Service is stopping.', 503)
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

        await self.perform(lambda: self.require_admin(request))

        def read_or_write() -> dict:
            """Read settings or apply an authorized revision-checked update."""
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

        return JSONResponse(await run_in_threadpool(read_or_write))

    async def cache_stats(self, request: Request) -> Response:
        """Describe the current publication cache and storage coverage."""

        def read() -> dict:
            """Authorize the administrator and read current cache statistics."""
            self.require_admin(request)
            result = self.service.cache_stats()
            result['activity'] = activity_snapshot(self.service, self.uploads, result)
            return result

        return JSONResponse(await self.perform(read))

    async def resource_stats(self, request: Request) -> Response:
        """Request a snapshot explicitly or read it without scanning object storage."""
        await self.perform(lambda: self.require_admin(request))
        refresh = request.query_params.get('refresh', '0')
        require(refresh in ('0', '1'), 'invalid_query')
        if refresh == '1':
            local = await self.perform(lambda: local_resources(self.service))
            value = self.resources.request(local)
        else:
            value = self.resources.current()
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    async def refresh(self, request: Request) -> Response:
        """Refresh the catalogue while retaining the previous snapshot on failure."""
        await self.body(request, set(), optional=True)

        def update() -> dict:
            """Authorize and queue a refresh; counts describe the currently visible version."""
            self.require_admin(request)
            job = self.service.request_refresh()
            return {**self.service.snapshot.counts(), 'refresh_job': job}

        return JSONResponse(await self.perform(update))

    async def resume_refresh(self, request: Request) -> Response:
        """Resume one incomplete refresh against its saved discovery boundary."""
        if self.config.publication_storage != 'local':
            raise ViewerError('not_found', 'Resource not found.', 404)
        await self.body(request, set(), optional=True)

        def resume() -> dict:
            """Authorize the administrator before selecting the persisted audit plan."""
            self.require_admin(request)
            job = self._local_service().resume_refresh(request.path_params['request_id'])
            return {**self.service.snapshot.counts(), 'refresh_job': job}

        return JSONResponse(await self.perform(resume))

    async def checkpoint(self, request: Request) -> Response:
        """Queue a recovery checkpoint without waiting for remote transfers."""
        if self.config.publication_storage != 'local':
            raise ViewerError('not_found', 'Resource not found.', 404)
        await self.body(request, set(), optional=True)

        def queue() -> dict:
            """Require administration before starting the background checkpoint job."""
            self.require_admin(request)
            return {'checkpoint_job': self._local_service().request_checkpoint()}

        return JSONResponse(await self.perform(queue))

    async def retry_backup(self, request: Request) -> Response:
        """Retry blocked backup work after an administrator fixes storage access."""
        if self.config.publication_storage != 'local':
            raise ViewerError('not_found', 'Resource not found.', 404)
        await self.body(request, set(), optional=True)

        def retry() -> dict:
            """Authorize the selected run's backup retry without changing publication data."""
            self.require_admin(request)
            return self._local_service().retry_backup(request.path_params['run_id'])

        return JSONResponse(await self.perform(retry))

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
            return Response('Packaged UI resource is unavailable.', media_type='text/plain', status_code=503)
        if name == 'index.html':
            content = content.replace(b'{{BUILD_INFO}}', html.escape(self.build_label).encode())
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
        self.service.start_background()
        try:
            yield
        finally:
            stopped, resources_stopped = await asyncio.gather(
                run_in_threadpool(self.service.close), run_in_threadpool(self.resources.close)
            )
            if not stopped or not resources_stopped:
                raise RuntimeError(
                    'Maintenance shutdown timed out; retain database and runtime lock until process exit.'
                )
            self.db.close()

    def build(self) -> Starlette:
        """Build routes and exception handlers for this application owner."""

        def read(
            handler: Callable[[Request], Awaitable[Response]],
            before: Callable[[Request], Awaitable[None]] | None = None,
        ) -> Callable[[Request], Awaitable[Response]]:
            """Wrap detail routes only when immutable local leases are available."""
            return (
                local_read(self._local_service(), handler, before)
                if self.config.publication_storage == 'local'
                else handler
            )

        routes = [
            Route('/healthz', self.health),
            Route('/readyz', self.ready),
            Route('/', self.asset),
            Route('/run', self.asset),
            Route('/settings', self.asset),
            Route('/admin/users', self.asset),
            Route('/assets/{name}', self.asset),
            Route('/api/v1/auth/session', self.session, methods=['GET', 'POST', 'DELETE']),
            Route('/api/v1/auth/password', self.own_password, methods=['PUT']),
            Route('/api/v1/publication-capabilities', self.capabilities),
            Route('/api/v1/publication-operations/{key:path}', self.publication_status),
            Route('/api/v1/auth/admin-overwrite-token', self.admin_overwrite_token, methods=['POST']),
            Route('/api/v1/auth/{operation}-token', self.auth, methods=['POST']),
            Route('/api/v1/baskets', self.get_baskets),
            Route('/api/v1/baskets/{hash}/runs', self.get_basket_runs),
            Route('/api/v1/runs/{run_id}/config', read(self.get_config)),
            Route('/api/v1/runs/{run_id}', read(self.get_run), methods=['GET']),
            Route('/api/v1/runs/{run_id}', self.publish, methods=['PUT']),
            Route('/api/v1/runs/{run_id}', self.delete_owned_run, methods=['DELETE']),
            Route('/api/v1/runs/{run_id}/tasks', read(self.get_tasks)),
            Route('/api/v1/runs/{run_id}/tasks/{task_id}', read(self.get_task)),
            Route('/api/v1/runs/{run_id}/tasks/{task_id}/files/{file_id}', read(self.get_file)),
            Route('/api/v1/runs/{run_id}/pinned', self.pinned, methods=['PUT']),
            Route('/api/v1/runs/{run_id}/metadata', self.metadata, methods=['PATCH']),
            Route('/api/v1/runs/{run_id}/archive', read(self.download, self.authorize_download)),
            Route('/api/v1/admin/users', self.users, methods=['GET', 'POST']),
            Route('/api/v1/admin/users/{login}', self.user_access, methods=['PATCH']),
            Route('/api/v1/admin/users/{login}/password', self.user_password, methods=['PUT']),
            Route('/api/v1/admin/baskets/order', self.basket_order, methods=['PUT']),
            Route('/api/v1/admin/runs/{run_id}', self.delete_run, methods=['DELETE']),
            Route('/api/v1/admin/runs/{run_id}/backup/retry', self.retry_backup, methods=['POST']),
            Route('/api/v1/admin/baskets/layout', self.layout, methods=['GET', 'PUT']),
            Route('/api/v1/admin/stats', self.cache_stats),
            Route('/api/v1/admin/resources', self.resource_stats),
            Route('/api/v1/admin/checkpoint', self.checkpoint, methods=['POST']),
            Route('/api/v1/admin/refresh', self.refresh, methods=['POST']),
            Route('/api/v1/admin/refresh/{request_id}/resume', self.resume_refresh, methods=['POST']),
        ]
        app = Starlette(
            routes=routes,
            lifespan=self.lifespan,
            middleware=[Middleware(SecurityHeaders), Middleware(StartupGate, service=self.service)],
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
