"""Publication transport fixtures exercise lost replies and one-use grant semantics."""

import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
from benchmark.agent_input import benchmark_settings
from benchmark.artifact_refs import read_reference, reference
from benchmark.errors import BenchmarkError
from benchmark.inference import generated_groups
from benchmark.publish import publish
from benchmark.run_store import RunStore
from benchmark.storage import read_json, write_json
from test_archive import complete_run

TOKEN = 'fixture_token_for_tests_only'


def viewer_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """Read mock public metadata from the exact uploaded producer archive."""
    published: dict = {}
    archived_report: dict = {}
    native_config: dict = {}
    public_tasks: list = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        """Serve readback separately from token and idempotent upload responses."""
        if request.method == 'PUT':
            with tarfile.open(fileobj=io.BytesIO(request.content), mode='r:gz') as archive:
                report_file = archive.extractfile('report.json')
                basket_file = archive.extractfile('basket.json')
                assert report_file is not None and basket_file is not None
                report, basket = json.loads(report_file.read()), json.loads(basket_file.read())
                native_file = archive.extractfile('agent/opencode.json')
                identity_file = archive.extractfile('config_identity.json')
                assert native_file is not None and identity_file is not None
                native_config.clear(); native_config.update(json.loads(native_file.read()))
                identity = json.loads(identity_file.read())
                archived_report.clear(); archived_report.update(report)
                public_tasks[:] = [{'task_id': task['id']} for task in report['tasks']]
            published.update(
                run_id=report['run']['id'],
                report_sha256=request.headers['X-Report-SHA256'],
                publication_sha256=request.headers['X-Archive-SHA256'],
                basket_hash=report['basket']['sha256'],
                user='fixture-owner',
                display_name=report['agent']['name'],
                tags=report['run']['tags'],
                inference=report['inference'],
                tag_groups={**generated_groups(report['inference']), 'setup': report['run']['tags']},
                basket={'name': basket['meta']['name']},
                pinned=False,
                benchmark=benchmark_settings(identity),
            )
        if request.method == 'GET':
            if request.url.path.endswith('/config'):
                return httpx.Response(200, json={'projection_version': 2,
                    'report_sha256': published['report_sha256'],
                    'publication_sha256': published['publication_sha256'],
                    'identity_sha256': archived_report['config']['identity_sha256'],
                    'source': archived_report['agent_input']['config'], 'supported': True, 'config': native_config})
            if request.url.path.endswith('/tasks'):
                offset = int(request.url.params.get('offset', 0))
                return httpx.Response(200, json={'total': len(public_tasks),
                    'items': public_tasks[offset:offset + 200],
                    'report_sha256': published['report_sha256'],
                    'publication_sha256': published['publication_sha256']})
            return httpx.Response(200, json=published)
        return handler(request)

    return httpx.MockTransport(dispatch)


def pending_publication(directory: Path) -> dict | None:
    """Resolve the selected immutable journal instead of a mutable pending.json file."""
    store = RunStore.open(directory)
    selected = store.state['pending_publish']
    return None if selected is None else read_reference(directory, selected['request'])


def assert_native_unchanged(directory: Path, previous: dict) -> None:
    """Publication state may advance while native identity, results and labels remain fixed."""
    current = RunStore.open(directory).snapshot()
    assert current['tasks'] == previous['tasks']
    assert current['agent'] == previous['agent']
    assert current['inference'] == previous['inference']
    assert current['run']['tags'] == previous['run']['tags']


def receipt(request: httpx.Request) -> dict:
    """Confirm the uploaded bytes, idempotency key and authenticated author."""
    return {
        'run_id': request.url.path.rsplit('/', 1)[-1],
        'archive_sha256': request.headers['X-Archive-SHA256'],
        'report_sha256': request.headers['X-Report-SHA256'],
        'idempotency_key': request.headers['Idempotency-Key'],
        'author_login': 'fixture-owner',
    }


def test_dry_run_has_no_network_or_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify dry run has no network or auth."""
    run = complete_run(tmp_path, monkeypatch)

    def forbidden(request: httpx.Request) -> NoReturn:
        """Fail if the tested path unexpectedly starts native work or HTTP."""
        raise AssertionError('Dry run performed network access')

    client = httpx.Client(transport=httpx.MockTransport(forbidden))
    before = RunStore.open(run).snapshot()
    assert publish(run, 'https://viewer.example', dry_run=True, client=client) == 0
    assert_native_unchanged(run, before)
    assert not (run / 'publish/pending.json').exists()
    assert len(list((run / 'operations/publish').glob('*/payload.tar.gz'))) == 1


def test_lost_reply_reuses_exact_key_and_bytes_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify lost reply reuses exact key and bytes after restart."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    uploads = []
    results = {}
    consumed = set()

    def transport(request: httpx.Request) -> httpx.Response:
        """Simulate viewer responses for the publication protocol."""
        key = request.headers['Idempotency-Key']
        uploads.append((key, request.content, request.url.params['overwrite']))
        if key not in results:
            consumed.add(request.headers['Authorization'])
            results[key] = receipt(request)
            raise httpx.ReadError('Reply lost after server commit', request=request)
        return httpx.Response(200, json=results[key])

    client = httpx.Client(transport=viewer_transport(transport))
    with pytest.raises(BenchmarkError) as caught:
        publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client)
    assert caught.value.code == 'publish_failed'
    assert pending_publication(run) is not None
    monkeypatch.setenv('TEST_VIEWER_TOKEN', 'second_grant_for_same_owner')
    assert publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', overwrite=True, client=client) == 0
    assert len(uploads) == 2 and uploads[0] == uploads[1]
    assert len(consumed) == 1  # Replay does not spend the new grant in this server fixture.
    assert pending_publication(run) is None
    for path in run.rglob('*'):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()
            assert b'second_grant_for_same_owner' not in path.read_bytes()


def test_conflict_requires_explicit_overwrite_with_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify conflict requires explicit overwrite with new key."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    uploads = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Simulate viewer responses for the publication protocol."""
        uploads.append((request.headers['Idempotency-Key'], request.url.params['overwrite']))
        if request.url.params['overwrite'] == '0':
            return httpx.Response(409)
        return httpx.Response(201, json=receipt(request))

    client = httpx.Client(transport=viewer_transport(transport))
    before = RunStore.open(run).snapshot()
    assert publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client) == 1
    assert len(uploads) == 1
    assert 'HTTP 409' in capsys.readouterr().err
    assert publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', overwrite=True, client=client) == 0
    assert uploads[0][0] != uploads[1][0] and uploads[1][1] == '1'
    assert_native_unchanged(run, before)


def test_bad_receipt_keeps_pending_and_does_not_claim_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify bad receipt keeps pending and does not claim success."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'run_id': 'wrong'})))
    with pytest.raises(BenchmarkError) as caught:
        publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client)
    assert caught.value.code == 'publish_receipt_mismatch'
    assert pending_publication(run) is not None


@pytest.mark.parametrize('corruption', ['native_config', 'task_coverage', 'benchmark_metadata'])
def test_bad_public_readback_keeps_request_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    """An accepted upload is not completed when public configuration or coverage differs."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    original = viewer_transport(lambda request: httpx.Response(201, json=receipt(request)))

    def corrupted(request: httpx.Request) -> httpx.Response:
        response = original.handle_request(request)
        if request.method != 'GET':
            return response
        body = json.loads(response.content)
        if corruption == 'native_config' and request.url.path.endswith('/config'):
            body['config']['model'] = 'foreign/model'
        elif corruption == 'task_coverage' and request.url.path.endswith('/tasks'):
            body['items'] = []
        elif corruption == 'benchmark_metadata' and not request.url.path.endswith(('/config', '/tasks')):
            body['benchmark']['timeout_seconds'] += 1
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(corrupted)) as client:
        with pytest.raises(BenchmarkError) as caught:
            publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client)
    assert caught.value.code == 'publish_readback_mismatch'
    assert pending_publication(run) is not None


def test_interactive_credentials_stay_out_of_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify interactive credentials stay out of artifacts."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.delenv('PRSIM_VIEWER_TOKEN', raising=False)
    monkeypatch.setattr('benchmark.publish._interactive', lambda: None)
    monkeypatch.setattr('builtins.input', lambda prompt: 'fixture-login')
    monkeypatch.setattr('benchmark.publish.getpass.getpass', lambda prompt: 'fixture-hidden-password')
    events = []

    def transport(request: httpx.Request) -> httpx.Response:
        """Simulate viewer responses for the publication protocol."""
        events.append(request.url.path)
        if request.url.path.endswith('publish-token'):
            assert json.loads(request.content) == {'login': 'fixture-login', 'password': 'fixture-hidden-password'}
            return httpx.Response(200, json={'token': TOKEN, 'expires_at': '2099-01-01T00:00:00Z'})
        assert request.headers['Authorization'] == 'Bearer ' + TOKEN
        return httpx.Response(200, json=receipt(request))

    client = httpx.Client(transport=viewer_transport(transport))
    assert publish(run, 'https://viewer.example', client=client) == 0
    assert len(events) == 2  # Public GET is served by the archive-backed wrapper.
    for path in run.rglob('*'):
        if path.is_file():
            content = path.read_bytes()
            assert b'fixture-hidden-password' not in content and TOKEN.encode() not in content


@pytest.mark.parametrize(
    'url', ['http://viewer.example', 'https://name:password@viewer.example', 'https://viewer.example?token=x']
)
def test_insecure_or_credential_url_is_rejected_before_archive(tmp_path: Path, url: str) -> None:
    """Verify insecure or credential url is rejected before archive."""
    with pytest.raises(BenchmarkError) as caught:
        publish(tmp_path / 'absent', url, dry_run=True)
    assert caught.value.code == 'publish_url'


def test_missing_explicit_token_never_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify missing explicit token never falls back."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.delenv('NO_SUCH_TOKEN', raising=False)
    monkeypatch.setenv('PRSIM_VIEWER_TOKEN', TOKEN)
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        """Fail if the tested path unexpectedly starts native work or HTTP."""
        calls.append(request)
        raise AssertionError('HTTP must not be called')

    client = httpx.Client(transport=httpx.MockTransport(forbidden))
    with pytest.raises(BenchmarkError) as caught:
        publish(run, 'https://viewer.example', token_env='NO_SUCH_TOKEN', client=client)
    assert caught.value.code == 'publish_token_empty' and not calls


@pytest.mark.parametrize('response', [[], 'invalid', {'run_id': 'foreign'}])
def test_malformed_success_receipt_does_not_complete_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    """Verify malformed success receipt does not complete operation."""
    run = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    with pytest.raises(BenchmarkError) as caught:
        publish(run, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client)
    assert caught.value.code == 'publish_receipt_mismatch'
    assert pending_publication(run) is not None


def test_public_readback_mismatch_keeps_exact_request_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful PUT alone cannot claim that public model metadata matches the evidence."""
    directory = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    uploads = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Confirm uploaded bytes but deliberately serve a wrong public evidence record."""
        if request.method == 'PUT':
            uploads.append(request.headers['Idempotency-Key'])
            return httpx.Response(201, json=receipt(request))
        return httpx.Response(200, json={'run_id': 'wrong public run'})

    with pytest.raises(BenchmarkError) as caught:
        publish(
            directory,
            'https://viewer.example',
            token_env='TEST_VIEWER_TOKEN',
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
    assert caught.value.code == 'publish_readback_mismatch'
    pending = pending_publication(directory)
    assert pending is not None and pending['idempotency_key'] == uploads[0]
    assert not list((directory / 'operations/publish').glob('*/receipt.json'))


def test_pending_upload_survives_full_run_relocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative owned payload refs replay the same bytes after moving the whole run."""
    directory = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    uploads = []
    response_cache = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Lose one committed reply, then return the stored idempotent response."""
        key = request.headers['Idempotency-Key']
        uploads.append((key, request.content))
        if key not in response_cache:
            response_cache[key] = receipt(request)
            raise httpx.ReadError('Synthetic lost response', request=request)
        return httpx.Response(200, json=response_cache[key])

    client = httpx.Client(transport=viewer_transport(handler))
    with pytest.raises(BenchmarkError):
        publish(directory, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client)
    relocated = tmp_path / 'elsewhere' / 'renamed-run'
    relocated.parent.mkdir()
    directory.rename(relocated)
    assert publish(relocated, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client) == 0
    assert uploads[0] == uploads[1]
    assert pending_publication(relocated) is None


@pytest.mark.parametrize('kind', ['fingerprint', 'server_receipt'])
def test_omitted_historical_payload_does_not_relax_publication_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Freshly rehashed metadata still needs matching request and confirmation identities."""
    directory = complete_run(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_VIEWER_TOKEN', TOKEN)
    client = httpx.Client(transport=viewer_transport(lambda request: httpx.Response(200, json=receipt(request))))
    assert publish(directory, 'https://viewer.example', token_env='TEST_VIEWER_TOKEN', client=client) == 0
    state = read_json(directory / '_meta/state.json')
    selected = state['last_publish']
    request = read_reference(directory, selected['request'])
    (directory / request['payload']['path']).unlink()
    if kind == 'fingerprint':
        request['fingerprint']['run_id'] = '44444444-4444-4444-8444-444444444444'
        write_json(directory / selected['request']['path'], request)
        selected['request'] = reference(directory, selected['request']['path'])
    else:
        confirmed = read_reference(directory, selected['receipt'])
        confirmed['server_receipt']['report_sha256'] = '0' * 64
        write_json(directory / selected['receipt']['path'], confirmed)
        selected['receipt'] = reference(directory, selected['receipt']['path'])
    write_json(directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(directory, allow_omitted_publish_payloads=True)
    assert caught.value.code in ('publication_fingerprint_invalid', 'publication_receipt_invalid')
