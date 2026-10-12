"""Configured native effort agrees with run labels while sources stay outside native JSON."""
from pathlib import Path
from starlette.testclient import TestClient
from test_config_view import publish_config

pytest_plugins = ['test_api']


def test_public_effort_and_origin_match_report_without_provider_secrets(client: TestClient, tmp_path: Path) -> None:
    rid = publish_config(client, tmp_path)
    value = client.get('/api/v1/runs/' + rid + '/config').json()
    run = client.get('/api/v1/runs/' + rid).json()
    effort = value['config']['provider']['openai']['models']['selected']['options']['reasoningEffort']
    assert effort == run['inference']['configured']['effort']['value'] == 'medium'
    assert run['inference']['configured']['effort']['source']['field'] == 'native_config.provider.openai.models.selected.options.reasoningEffort'
    assert run['tag_groups']['model'] == ['openai/selected [medium]']
    assert run['tag_groups']['effort'] == ['medium']
    assert 'inference' not in value['config'] and 'MUST_NOT_LEAK' not in str(value)
