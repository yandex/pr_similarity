"""One-use HTTPS grants and durable same-byte/key publication recovery."""

import getpass
import os
import re
import stat
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx

from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.execution.agent_input import benchmark_settings
from benchmark.models import Report, WireModel
from benchmark.models.base import ArtifactReceipt
from benchmark.models.evidence import (
    ConfigIdentity,
    PublicMetadata,
    PublishFingerprint,
    PublishMetadata,
    PublishReceipt,
    PublishRequest,
    ServerReceipt,
)
from benchmark.persistence.artifact_refs import read_reference, reference
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import (
    RunLease,
    bytes_sha256,
    document_bytes,
    read_json,
    run_lock,
    sha256,
    utc_now,
    write_json,
)
from benchmark.presentation.inference import generated_groups
from benchmark.publication.archive import create_archive_locked, verify_archive
from benchmark.publication.publication_progress import PublicationProgress
from benchmark.runtime.commands import Command
from benchmark.validation.basket import load_basket


class PublicationValidation(WireModel):
    """Record a dry-run archive check without selecting or sending a publication."""

    schema_version: int
    status: str
    request: ArtifactReceipt


def server_url(value: str) -> str:
    """Require an HTTPS origin without embedded credentials or query data."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ('', '/')
    ):
        raise BenchmarkError('publish_url', 'URL must be an HTTPS server origin without credentials, path or query.')
    try:
        if parsed.port is not None and not 0 < parsed.port < 65536:
            raise ValueError('invalid port')
    except ValueError as error:
        raise BenchmarkError('publish_url', 'URL contains an invalid port.') from error
    return value.rstrip('/')


def _interactive() -> None:
    """Require a terminal before requesting hidden credentials."""
    if not sys.stdin.isatty():
        raise BenchmarkError('publish_auth_required', 'No interactive terminal; supply TOKEN_ENV or TOKEN_FILE.')


def _token(
    client: httpx.Client,
    url: str,
    run_dir: Path,
    command: Command,
    token_file: Path | None,
    token_env: str | None,
    token_prompt: bool,
) -> str:
    """Select one explicit token source or request an interactive server grant."""
    if token_file is not None:
        path = token_file.expanduser().resolve()
        metadata = token_file.expanduser().lstat()
        if (
            token_file.expanduser().is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise BenchmarkError('publish_token_file', 'TOKEN_FILE must be an owner-only regular file (mode 600).')
        if path.is_relative_to(run_dir):
            raise BenchmarkError('publish_token_file', 'TOKEN_FILE must be outside the run.')
        token = path.read_text().strip()
    elif token_env is not None:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', token_env):
            raise BenchmarkError('publish_token_env', 'TOKEN_ENV must be an environment variable name.')
        token = os.environ.get(token_env, '').strip()
    elif token_prompt:
        _interactive()
        token = getpass.getpass('Publish token: ').strip()
    elif 'PRSIM_VIEWER_TOKEN' in os.environ:
        token = os.environ['PRSIM_VIEWER_TOKEN'].strip()
    else:
        _interactive()
        login = input('Login: ').strip()
        password = getpass.getpass('Password: ')
        command.add_secret(login)
        command.add_secret(password)
        if not login or not password:
            raise BenchmarkError('publish_auth_empty', 'Login and password must not be empty.')
        response = client.post(url + '/api/v1/auth/publish-token', json={'login': login, 'password': password})
        if response.status_code != 200:
            raise BenchmarkError(
                'publish_auth_denied', 'Server refused login or token issuance; check credentials.', exit_code=1
            )
        data = response.json()
        if not isinstance(data, dict):
            raise BenchmarkError('publish_auth_denied', 'Server returned an invalid token response.', exit_code=1)
        token = data.get('token')
        if isinstance(token, str):
            command.add_secret(token)
        expires = datetime.fromisoformat(data['expires_at'])
        if expires.tzinfo is None or expires <= datetime.now(UTC):
            raise BenchmarkError(
                'publish_token_expired', 'Server issued an expired grant; request a new token.', exit_code=1
            )
    if not isinstance(token, str) or not token or any(ch in token for ch in '\r\n'):
        raise BenchmarkError('publish_token_empty', 'The selected token source is empty or invalid.')
    command.add_secret(token)
    return token


def _new_request(store: RunStore, lease: RunLease, url: str, overwrite: bool) -> PublishRequest:
    """Freeze exact archive bytes before publishing the request pointer in state."""
    identifier = str(uuid.uuid4())
    relative = Path('operations/publish') / identifier
    payload_ref = (relative / 'payload.tar.gz').as_posix()
    # Request and its payload appear only after freezing the preceding run snapshot.
    with TemporaryDirectory(prefix='benchmark-publication-', dir=store.directory.parent) as temporary:
        archive = create_archive_locked(store.directory, Path(temporary) / 'payload.tar.gz', store=store, lease=lease)
        report = store.snapshot()
        directory = store.directory / relative
        artifact_path(store.directory, (relative / 'request.json').as_posix())
        directory.mkdir(parents=True, exist_ok=False)
        os.replace(archive.path, store.directory / payload_ref)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    payload = reference(store.directory, payload_ref)
    fingerprint = PublishFingerprint(
        server=url,
        run_id=store.registry.id,
        archive_sha256=payload.sha256,
        report_sha256=archive.manifest.report_sha256,
        overwrite=overwrite,
    )
    basket = load_basket(store.directory / 'basket.json')
    identity = ConfigIdentity.model_validate(read_json(store.directory / 'config_identity.json'))
    value = PublishRequest(
        schema_family='pr_similarity_os.publish_request',
        schema_version=2,
        id=identifier,
        run_id=store.registry.id,
        basket_sha256=report.basket.sha256,
        config_identity_sha256=report.config.identity_sha256,
        idempotency_key=identifier,
        fingerprint=fingerprint,
        fingerprint_sha256=sha256(fingerprint),
        payload=payload,
        created_at=utc_now(),
        metadata=PublishMetadata(
            display_name=report.agent.name,
            tags=report.run.tags,
            basket_name=basket.data.meta.name,
            inference=report.inference,
            agent_input=report.agent_input,
            benchmark=benchmark_settings(identity),
            task_ids=[task.id for task in report.tasks],
        ),
    )
    write_json(directory / 'request.json', value)
    return value


def _validate_pending(store: RunStore, pending: PublishRequest, url: str) -> Path:
    """Refuse changed replay inputs while retaining the original upload key and settings."""
    fingerprint = pending.fingerprint
    if fingerprint.server != url or fingerprint.run_id != store.registry.id:
        raise BenchmarkError('publish_pending', 'Recover the pending publication with its original URL first.')
    if sha256(fingerprint) != pending.fingerprint_sha256:
        raise BenchmarkError('publish_pending_damaged', 'Publication fingerprint changed; restore its saved request.')
    payload = pending.payload
    path = artifact_path(store.directory, payload.path, must_exist=True)
    if reference(store.directory, payload.path) != payload or payload.sha256 != fingerprint.archive_sha256:
        raise BenchmarkError('publish_pending_damaged', 'Frozen upload bytes changed; restore the exact payload.')
    manifest = verify_archive(path)
    if (
        manifest.run_id != store.registry.id
        or manifest.report_sha256 != fingerprint.report_sha256
        or not manifest.complete
    ):
        raise BenchmarkError('publish_pending_damaged', 'Frozen archive differs from its publication fingerprint.')
    return path


def _receipt_data(response: httpx.Response, pending: PublishRequest, token: str) -> ServerReceipt:
    """Require exact server confirmation without storing a credential-bearing response."""
    data = response.json()
    expected = {
        'run_id': pending.run_id,
        'archive_sha256': pending.fingerprint.archive_sha256,
        'report_sha256': pending.fingerprint.report_sha256,
        'idempotency_key': pending.idempotency_key,
    }
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        raise BenchmarkError(
            'publish_receipt_mismatch', 'Server did not confirm these exact upload bytes; repeat publish.'
        )
    author = data.get('author_login')
    if not isinstance(author, str) or not author or token in author or len(author) > 256:
        raise BenchmarkError('publish_receipt_mismatch', 'Server receipt has invalid authenticated author metadata.')
    return ServerReceipt(
        run_id=pending.run_id,
        archive_sha256=pending.fingerprint.archive_sha256,
        report_sha256=pending.fingerprint.report_sha256,
        idempotency_key=pending.idempotency_key,
        author_login=author,
    )


def _readback_tasks(
    client: httpx.Client, pending: PublishRequest, receipt: ServerReceipt, query: dict[str, str]
) -> None:
    """Verify every published task ID in the original frozen order."""
    task_ids = []
    while len(task_ids) < len(pending.metadata.task_ids):
        response = client.get(
            pending.fingerprint.server + '/api/v1/runs/' + pending.run_id + '/tasks',
            params={**query, 'offset': len(task_ids), 'limit': 200},
        )
        page = response.json() if response.status_code == 200 else None
        if (
            not isinstance(page, dict)
            or type(page.get('total')) is not int
            or page.get('total') != len(pending.metadata.task_ids)
            or page.get('report_sha256') != receipt.report_sha256
            or page.get('publication_sha256') != receipt.archive_sha256
            or not isinstance(page.get('items'), list)
            or not page['items']
            or any(not isinstance(item, dict) for item in page['items'])
        ):
            raise BenchmarkError('publish_readback_mismatch', 'Public task coverage differs from the frozen run.')
        task_ids.extend(item.get('task_id') for item in page['items'])
    if task_ids != pending.metadata.task_ids:
        raise BenchmarkError('publish_readback_mismatch', 'Public task order or identity differs from the frozen run.')


def _readback(
    client: httpx.Client, pending: PublishRequest, receipt: ServerReceipt, command: Command
) -> PublicMetadata:
    """Check public evidence, generated groups and actual owner metadata after upload."""
    response = client.get(pending.fingerprint.server + '/api/v1/runs/' + pending.run_id)
    if response.status_code != 200:
        raise BenchmarkError(
            'publish_readback_failed', 'Uploaded run is not confirmed by public readback; repeat publish.'
        )
    data = response.json()
    expected = {
        'run_id': pending.run_id,
        'report_sha256': receipt.report_sha256,
        'publication_sha256': receipt.archive_sha256,
        'basket_hash': pending.basket_sha256,
        'user': receipt.author_login,
        'inference': pending.metadata.inference.to_wire(),
    }
    if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
        raise BenchmarkError('publish_readback_mismatch', 'Public run evidence differs from the frozen publication.')
    groups = data.get('tag_groups')
    generated = generated_groups(pending.metadata.inference)
    if (
        not isinstance(groups, dict)
        or set(groups) != set(generated) | {'setup'}
        or any(groups.get(key) != value for key, value in generated.items())
    ):
        raise BenchmarkError('publish_readback_mismatch', 'Public generated Model/Effort differs from frozen config.')
    name, tags = data.get('display_name'), data.get('tags')
    if not isinstance(name, str) or not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise BenchmarkError('publish_readback_mismatch', 'Public owner metadata is invalid.')
    if groups.get('setup') != tags or data.get('basket', {}).get('name') != pending.metadata.basket_name:
        raise BenchmarkError(
            'publish_readback_mismatch', 'Public Setup labels or basket metadata differs from evidence.'
        )
    differences = name != pending.metadata.display_name or tags != pending.metadata.tags
    if differences:
        message = (
            'Overwrite preserved the viewer display name and Setup labels.'
            if pending.fingerprint.overwrite
            else 'Viewer display name or Setup labels differ from local metadata.'
        )
        command.message(message, level='WARNING')
    query = {
        'expected_report_sha256': receipt.report_sha256,
        'expected_publication_sha256': receipt.archive_sha256,
    }
    config_response = client.get(
        pending.fingerprint.server + '/api/v1/runs/' + pending.run_id + '/config', params=query
    )
    config = config_response.json() if config_response.status_code == 200 else None
    if (
        not isinstance(config, dict)
        or config.get('projection_version') != 2
        or config.get('source') != pending.metadata.agent_input.config.to_wire()
        or config.get('report_sha256') != receipt.report_sha256
        or config.get('publication_sha256') != receipt.archive_sha256
        or config.get('identity_sha256') != pending.config_identity_sha256
        or type(config.get('supported')) is not bool
        or data.get('benchmark') != pending.metadata.benchmark.to_wire()
    ):
        raise BenchmarkError('publish_readback_mismatch', 'Public native config or benchmark metadata differs.')
    if config['supported']:
        source = config['source']
        content = document_bytes(config['config'])
        if len(content) != source['size_bytes'] or bytes_sha256(content) != source['sha256']:
            raise BenchmarkError(
                'publish_readback_mismatch', 'Public native config differs from the frozen input bytes.'
            )
    else:
        command.message(
            'Native config is unavailable in the public view: ' + str(config.get('reason')), level='WARNING'
        )
    _readback_tasks(client, pending, receipt, query)
    return PublicMetadata.model_validate(
        {
            'display_name': name,
            'tags': tags,
            'tag_groups': groups,
            'inference': data['inference'],
            'pinned': data.get('pinned'),
            'config_source': config['source'],
            'config_supported': config['supported'],
            'benchmark': data['benchmark'],
        }
    )


def _close_request(
    store: RunStore,
    pending: PublishRequest,
    status: str,
    *,
    receipt: ServerReceipt | None = None,
    metadata: PublicMetadata | None = None,
) -> None:
    """Write one immutable operation receipt before selecting its confirmation in state."""
    selected = store.state.pending_publish
    assert selected is not None
    value = PublishReceipt(
        schema_family='pr_similarity_os.publish_receipt',
        schema_version=2,
        id=pending.id,
        run_id=pending.run_id,
        status=status,
        finished_at=utc_now(),
        request=selected.request,
        server_receipt=receipt,
        public_metadata=metadata,
    )
    relative = 'operations/publish/' + pending.id + '/receipt.json'
    target = artifact_path(store.directory, relative)
    if target.exists():
        if PublishReceipt.model_validate(read_json(target)) != value:
            # A durable receipt can precede its interrupted state commit.
            existing = PublishReceipt.model_validate(read_json(target))
            if (existing.id, existing.run_id, existing.status, existing.request, existing.server_receipt) != (
                value.id,
                value.run_id,
                value.status,
                value.request,
                value.server_receipt,
            ):
                raise BenchmarkError(
                    'publish_receipt_mismatch', 'Existing publication receipt describes another result.'
                )
    else:
        write_json(target, value)
    store.finish_publication(reference(store.directory, relative))


def _confirm_upload(
    response: httpx.Response,
    pending: PublishRequest,
    store: RunStore,
    command: Command,
    token: str,
    client: httpx.Client,
    feedback: PublicationProgress,
) -> int:
    """Close only confirmed uploads; lost or mismatched replies keep the pending request."""
    if response.status_code == 409:
        _close_request(store, pending, 'conflict')
        command.message(
            'Publication exists (HTTP 409); use OVERWRITE=1 for replacement with a server backup.', level='WARNING'
        )
        command.finish('conflict')
        return 1
    if response.status_code in (400, 401, 403, 404, 413, 422):
        _close_request(store, pending, 'rejected')
        raise BenchmarkError(
            'publish_rejected',
            f'Upload rejected (HTTP {response.status_code}); run results are preserved.',
            exit_code=1,
        )
    if response.status_code not in (200, 201):
        raise BenchmarkError(
            'publish_unconfirmed',
            'Upload outcome is unconfirmed; repeat publish to recover the same request.',
            exit_code=1,
        )
    receipt = _receipt_data(response, pending, token)
    metadata = _readback(client, pending, receipt, command)
    _close_request(store, pending, 'completed', receipt=receipt, metadata=metadata)
    feedback.pause()
    command.message('Publication and public Model/Effort/Setup metadata confirmed.')
    command.finish('completed')
    return 0


def _publish_locked(
    store: RunStore,
    lease: RunLease,
    report: Report,
    command: Command,
    url: str,
    *,
    dry_run: bool,
    overwrite: bool,
    token_file: Path | None,
    token_env: str | None,
    token_prompt: bool,
    client: httpx.Client | None,
    feedback: PublicationProgress,
) -> int:
    """Run one locked publication journal without changing frozen native results."""
    run_dir = store.directory
    owned_client = client is None
    try:
        feedback.phase('Preparing archive')
        selected = store.state.pending_publish
        if selected is not None and not dry_run:
            pending = read_reference(run_dir, selected.request, PublishRequest)
            path = _validate_pending(store, pending, url)
            command.message(
                'Recovering the unresolved upload with its original bytes, key and overwrite setting.',
                level='WARNING',
            )
        else:
            pending = _new_request(store, lease, url, overwrite)
            path = artifact_path(run_dir, pending.payload.path, must_exist=True)
            if dry_run:
                relative = 'operations/publish/' + pending.id + '/validation.json'
                write_json(
                    run_dir / relative,
                    PublicationValidation(
                        schema_version=2,
                        status='dry_run',
                        request=reference(run_dir, 'operations/publish/' + pending.id + '/request.json'),
                    ),
                )
                command.message(f'Validated archive: {path}; no HTTP or authorization.')
                command.finish('completed')
                return 0
            store.select_publication(
                pending.id, reference(run_dir, 'operations/publish/' + pending.id + '/request.json')
            )
        command.message(
            'Model/Effort come from frozen native config; overwrite preserves viewer display name and Setup labels.'
        )
        if client is None:
            client = httpx.Client(timeout=httpx.Timeout(120, connect=20), follow_redirects=False, verify=True)
        feedback.phase('Waiting for login', active=False)
        token = _token(client, url, run_dir, command, token_file, token_env, token_prompt)
        command.message('Publishing to object storage may take several minutes. Please wait.')
        feedback.phase('Publishing · sending archive')
        fingerprint = pending.fingerprint
        headers = {
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/gzip',
            'Idempotency-Key': pending.idempotency_key,
            'X-Archive-SHA256': fingerprint.archive_sha256,
            'X-Report-SHA256': fingerprint.report_sha256,
        }
        with path.open('rb') as stream:

            def chunks() -> Iterator[bytes]:
                """Stream the archive and report when upload switches to the server wait."""
                while chunk := stream.read(64 << 10):
                    yield chunk
                feedback.phase('Publishing · waiting for server', reset=False)

            response = client.put(
                url + '/api/v1/runs/' + fingerprint.run_id,
                params={'overwrite': '1' if fingerprint.overwrite else '0'},
                headers=headers,
                content=chunks(),
                timeout=httpx.Timeout(120, connect=20, read=960),
            )
        feedback.phase('Verifying publication')
        return _confirm_upload(response, pending, store, command, token, client, feedback)
    except OPERATION_ERRORS as error:
        command.diagnostic(error)
        command.finish('failed')
        if isinstance(error, BenchmarkError):
            raise
        raise BenchmarkError(
            'publish_failed', 'Publication failed; repeat publish to recover its saved operation.', exit_code=1
        ) from error
    finally:
        feedback.close()
        if owned_client and client is not None:
            client.close()
        command.close()


def publish(
    run_dir: Path,
    url: str,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    token_file: Path | None = None,
    token_env: str | None = None,
    token_prompt: bool = False,
    level: str = 'INFO',
    client: httpx.Client | None = None,
    progress: str = 'auto',
) -> int:
    """Freeze and publish one stopped run, replaying unresolved requests with identical bytes.

    Args:
        run_dir: Complete registry/state run with sealed native evidence.
        url: Explicit HTTPS viewer origin.
        dry_run: Validate a local archive without authorization or HTTP.
        overwrite: Request replacement while preserving server display name and Setup.
        token_file: Owner-only publication grant outside the run directory.
        token_env: Environment variable containing a grant.
        token_prompt: Read a hidden grant directly from the terminal.
        level: Console threshold; warnings remain visible.
        client: Optional injected HTTPS transport for bounded verification.
        progress: Disposable auto/plain/off phase and elapsed feedback.

    Returns:
        Zero after validated dry run or confirmed public readback, one on conflict.

    Raises:
        BenchmarkError: If evidence, grants or public confirmation are invalid.

    """
    url = server_url(url)
    if progress not in ('auto', 'plain', 'off'):
        raise BenchmarkError('invalid_arguments', 'PROGRESS accepts auto, plain or off.')
    run_dir = run_dir.expanduser().resolve()
    if sum((token_file is not None, token_env is not None, token_prompt)) > 1:
        raise BenchmarkError('publish_token_sources', 'Choose one of TOKEN_FILE, TOKEN_ENV or TOKEN_PROMPT.')
    if token_file is not None and token_file.expanduser().resolve().is_relative_to(run_dir):
        raise BenchmarkError('publish_token_file', 'TOKEN_FILE must be outside the run directory.')
    with run_lock(run_dir) as lease:
        store = RunStore.open(run_dir, lease=lease)
        report = store.rebuild()
        if report.run.status in ('running', 'stopping'):
            raise BenchmarkError('run_active', 'Publication requires a stopped run.')
        feedback = PublicationProgress(progress if level in ('DEBUG', 'INFO') else 'off')
        try:
            command = Command(
                run_dir,
                'publish',
                level=level,
                previous=report.run.current_invocation_id,
                console_writer=feedback.write_message,
            )
            return _publish_locked(
                store,
                lease,
                report,
                command,
                url,
                dry_run=dry_run,
                overwrite=overwrite,
                token_file=token_file,
                token_env=token_env,
                token_prompt=token_prompt,
                client=client,
                feedback=feedback,
            )
        finally:
            feedback.close()
