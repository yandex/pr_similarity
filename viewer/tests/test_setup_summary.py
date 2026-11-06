"""Setup summaries expose choices while preserving private configuration values."""

from pathlib import Path

import pytest
from helpers import service_for
from starlette.testclient import TestClient
from test_config_view import publish_config
from viewer.errors import ViewerError
from viewer.presentation import config_view as config_view
from viewer.util import json_bytes

pytest_plugins = ['test_config_view']


def test_setup_uses_bound_identity_without_env_values(client: TestClient, tmp_path: Path) -> None:
    """Read verified setup identity and reject modified bytes without exposing values."""
    rid = publish_config(client, tmp_path)
    service = service_for(client)
    result = config_view.public_config(service, rid)
    assert result['supported']
    assert result['setup_summary']['env_count'] == 1
    assert result['setup_summary']['setup_script'] is False
    assert result['setup_summary']['instructions_default'] is False
    assert result['setup_summary']['instructions_text'] is None
    assert 'MUST_NOT_LEAK' not in str(result)
    service.store.put(service.runs[rid]['prefix'] + 'data/config_identity.json', json_bytes({'env': {}}))
    with pytest.raises(ViewerError):
        config_view.public_config(service, rid)
