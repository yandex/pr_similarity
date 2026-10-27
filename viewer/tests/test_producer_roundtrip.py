"""Portable offline producer-to-viewer contract roundtrip with real native artifacts."""

import os
import subprocess
from pathlib import Path

import pytest
from helpers import service_for
from playwright.sync_api import Page, expect
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viewer.config import Config
from viewer.util import digest
from viewer.validation.archive import validate_archive
from viewer.web.api import create_app

pytest_plugins = ['test_ui']


def _producer_archive(tmp_path: Path, basket_version: int = 3) -> Path:
    """Generate a bounded provider-free archive through the benchmark's real artifact workflow."""
    supplied = os.environ.get('PRSIM_PRODUCER_ARCHIVE')
    if supplied and basket_version == 3:
        archive = Path(supplied)
        assert archive.is_file()
        return archive
    benchmark = Path(__file__).parents[2] / 'benchmark'
    interpreter = benchmark / '.venv/bin/python'
    if not interpreter.is_file():
        pytest.skip('requires the sibling benchmark module venv')
    exported = tmp_path / 'producer'
    environment = {
        **os.environ,
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONHASHSEED': '42',
        'PRSIM_ARTIFACT_FIXTURE_DIR': str(exported),
    }
    result = subprocess.run(
        [
            str(interpreter),
            '-m',
            'pytest',
            f'tests/test_artifact_workflow.py::test_public_runner_native_artifact_workflow[{basket_version}]',
            '-q',
        ],
        cwd=benchmark,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = exported / 'producer.tar.gz'
    assert archive.is_file()
    return archive


@pytest.mark.parametrize(('basket_version', 'legacy'), [(1, False), (2, False), (3, False), (2, True)])
def test_actual_producer_archive_publishes_and_serves_all_role_pairs(
    tmp_path: Path,
    basket_version: int,
    legacy: bool,
) -> None:
    """Exercise producer archive validation, ViewerService publication, API config and absent/empty diffs."""
    archive = (
        Path(__file__).parent / 'fixtures/legacy/producer-v1-basket2.tar.gz'
        if legacy
        else _producer_archive(tmp_path, basket_version)
    )
    token = tmp_path / 'admin-token'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'viewer-runtime', admin_token_file=token, storage='mock')
    with TestClient(create_app(config)) as client:
        assert (
            client.post(
                '/api/v1/admin/users',
                json={'login': 'alice', 'password': 'secret'},
                headers={'Authorization': 'Bearer ' + 'A' * 32},
            ).status_code
            == 201
        )
        imported = validate_archive(archive, tmp_path / 'validated', config.limits)
        service = service_for(client)
        receipt = service.publish(imported, 'alice', 'producer-roundtrip')
        rid = receipt['run_id']
        row = client.get('/api/v1/runs/' + rid).json()
        assert imported.basket.meta.schema_version == basket_version
        assert set(row['basket']) == {'name', 'description', 'task_source'}
        service.refresh()
        assert client.get('/api/v1/runs/' + rid).json()['basket'] == row['basket']
        assert row['tag_groups']['model'] == ['provider/model [medium]']
        assert row['tag_groups']['effort'] == ['medium']
        assert row['tag_groups']['setup'] == [
            'model:user-label',
            'Setup:Офлайн',
            'Setup:Офлайн',
        ]
        config_response = client.get('/api/v1/runs/' + rid + '/config').json()
        assert (
            config_response['supported']
            and config_response['projection_version'] == 2
            and config_response['config']['model'] == 'provider/model'
        )
        assert row['benchmark']['timeout_seconds'] == 10
        assert 'timeout_seconds' not in config_response['config']
        task = client.get('/api/v1/runs/' + rid + '/tasks').json()['items'][0]
        details = client.get('/api/v1/runs/' + rid + '/tasks/' + task['task_id']).json()
        files = {row['path']: row['file_id'] for row in details['files']}
        for path in ('src/value.py', 'src/deleted.py', 'src/added.py'):
            payload = client.get('/api/v1/runs/' + rid + '/tasks/' + task['task_id'] + '/files/' + files[path]).json()
            assert set(payload['exists']) == {'base', 'human', 'ai'}
            assert set(payload) >= {'human_diff', 'agent_diff'}
        deleted = client.get(
            '/api/v1/runs/' + rid + '/tasks/' + task['task_id'] + '/files/' + files['src/deleted.py']
        ).json()
        assert deleted['exists'] == {'base': True, 'human': False, 'ai': False}
        added = client.get(
            '/api/v1/runs/' + rid + '/tasks/' + task['task_id'] + '/files/' + files['src/added.py']
        ).json()
        assert added['exists'] == {'base': False, 'human': True, 'ai': True}
        assert digest(archive.read_bytes()) == receipt['archive_sha256']


@pytest.mark.parametrize(('basket_version', 'legacy'), [(1, False), (2, False), (3, False), (2, True)])
def test_actual_producer_archive_is_navigable_in_browser(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path, basket_version: int, legacy: bool
) -> None:
    """Open real producer evidence in the browser without replacing synthetic catalogue rows."""
    page, origin, app, errors = browser_page
    archive = (
        Path(__file__).parent / 'fixtures/legacy/producer-v1-basket2.tar.gz'
        if legacy
        else _producer_archive(tmp_path, basket_version)
    )
    imported = validate_archive(archive, tmp_path / 'validated', app.state.service.config.limits)
    receipt = app.state.service.publish(imported, 'alice', 'producer-browser')
    page.goto(origin)
    row = page.locator('tr.run-row').filter(has=page.get_by_role('link', name='Offline artifact workflow', exact=True))
    row.wait_for()
    assert 'provider/model' in row.inner_text() and 'medium' in row.inner_text()
    assert row.inner_text().count('Setup:Офлайн') == 2 and 'model:user-label' in row.inner_text()
    row.get_by_role('link', name='Offline artifact workflow', exact=True).click()
    page.locator('.task>summary').wait_for()
    page.locator('.task-toggle').click()
    page.locator('button.file').first.click()
    page.locator('.diff-grid').wait_for()
    assert page.locator('.diff-panel h4').all_text_contents() == [
        'Human diff',
        'Agent diff',
    ]
    page.get_by_role('button', name='Close diff', exact=True).click()
    page.locator('button.config-button').click()
    page.locator('#config-dialog').wait_for()
    expect(page.locator('#config-content pre')).to_contain_text('provider/model')
    assert receipt['run_id'] in page.url and errors == []
