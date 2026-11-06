"""Native configuration routes reject unknown alternatives with JSON errors."""

from pathlib import Path

from starlette.testclient import TestClient
from test_api import create_user, upload

pytest_plugins = ['test_api']


def test_unknown_config_route_is_absent(client: TestClient, tmp_path: Path) -> None:
    """Return JSON not-found for unsupported config routes while serving native config."""
    create_user(client)
    report, _, _, _ = upload(client, tmp_path)
    base = '/api/v1/runs/' + report['run']['id']
    assert client.get(base + '/config').status_code == 200
    response = client.get(base + '/unsupported-config')
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('application/json')
