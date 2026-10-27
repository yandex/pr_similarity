"""Native config and harness controls are separate, safe, digest-bound views."""

from dataclasses import replace
from pathlib import Path

import pytest
from helpers import archive_fixture, service_for
from starlette.testclient import TestClient
from test_api import client as _api_client
from test_api import create_user

from viewer.models.report import ReportInferenceEffectiveAttemptItem
from viewer.presentation.config_view import native_projection, public_config
from viewer.presentation.inference import build_inference
from viewer.util import digest, json_bytes
from viewer.validation.agent_input import _identity_native
from viewer.validation.archive import validate_archive

client = _api_client


def publish_config(client: TestClient, tmp_path: Path, tdd: bool = False) -> str:
    """Publish native configuration with optional frozen TDD skill resources."""
    native = {
        'model': 'openai/selected',
        'permission': {'*': 'allow', '*.secret': 'deny'},
        'provider': {
            'openai': {
                'options': {'apiKey': '{env:OPENAI_API_KEY}'},
                'models': {'selected': {'options': {'reasoningEffort': 'medium'}}},
            }
        },
    }

    def configure(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Bind native configuration, harness settings and optional skills to the fixture."""
        identity = {
            'identity_version': 1,
            'runtime': 'opencode:v1.18.33',
            'timeout_seconds': 1800,
            'setup': None,
            'retries': {'max_retries': 2, 'categories': ['agent_error']},
            'diff_excludes': ['*.lock'],
            'env': {'PRIVATE': 'MUST_NOT_LEAK'},
            'native_config': _identity_native(native),
            'resources': [],
            'instructions': {'protocol': 'benchmark_task_v1', 'sha256': digest(b'')},
        }
        if tdd:
            content = b'Use a failing test first.\n'
            files['agent/resources/2-0-0/tdd/SKILL.md'] = content
            resource = {'field': 'paths', 'index': 0, 'files': [{'path': 'SKILL.md', 'sha256': digest(content)}]}
            native['skills'] = {'paths': ['resources/2-0-0/tdd']}
            identity['native_config']['skills'] = {'paths': [{'resource': resource}]}
            identity['resources'] = [resource]
        files['agent/opencode.json'] = json_bytes(native)
        files['config_identity.json'] = json_bytes(identity)
        report['inference'] = build_inference(identity)

    if service_for(client).db.actor('alice') is None:
        create_user(client)
    path = tmp_path / ('tdd.tar.gz' if tdd else 'baseline.tar.gz')
    report, _ = archive_fixture(
        path, configure=configure, run_id='44444444-4444-4444-8444-000000000002' if tdd else None
    )
    service = service_for(client)
    imported = validate_archive(path, tmp_path / ('tdd-data' if tdd else 'baseline-data'), service.config.limits)
    service.publish(imported, 'alice', 'tdd-config' if tdd else 'baseline-config')
    return report['run']['id']


def test_native_config_and_harness_metadata_are_separate(client: TestClient, tmp_path: Path) -> None:
    """Verify native config and harness metadata are separate."""
    rid = publish_config(client, tmp_path)
    response = client.get('/api/v1/runs/' + rid + '/config')
    value = response.json()
    assert value['supported'] and value['projection_version'] == 2
    native = value['config']
    assert native['model'] == 'openai/selected'
    assert list(native['permission']) == ['*', '*.secret']
    assert native['provider']['openai']['models']['selected']['options']['reasoningEffort'] == 'medium'
    assert native['provider']['openai']['options']['apiKey'] == '{env:OPENAI_API_KEY}'
    assert not ({'inference', 'timeout_seconds', 'retries', 'diff_excludes'} & set(native))
    assert 'MUST_NOT_LEAK' not in response.text
    row = client.get('/api/v1/runs/' + rid).json()
    assert row['benchmark'] == {
        'runtime': 'opencode:v1.18.33',
        'timeout_seconds': 1800,
        'setup_timeout_seconds': None,
        'retries': {'max_retries': 2, 'categories': ['agent_error']},
        'diff_excludes': ['*.lock'],
    }
    assert value['report_sha256'] == row['report_sha256']
    assert value['source']['path'] == 'agent/opencode.json'
    stale = client.get('/api/v1/runs/' + rid + '/config?expected_report_sha256=' + 'f' * 64)
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'run_changed'


def test_tdd_skills_are_visible_and_distinguish_the_native_config(client: TestClient, tmp_path: Path) -> None:
    """Verify tdd skills are visible and distinguish the native config."""
    baseline = publish_config(client, tmp_path)
    tdd = publish_config(client, tmp_path, tdd=True)
    first = client.get('/api/v1/runs/' + baseline + '/config').json()['config']
    second = client.get('/api/v1/runs/' + tdd + '/config').json()['config']
    assert first != second
    assert second['skills']['paths'] == ['resources/2-0-0/tdd']


def test_config_size_does_not_grow_with_effective_attempt_claims(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep public input projection independent of accumulated effective claims."""
    rid = publish_config(client, tmp_path)
    service = service_for(client)
    report = service.full_report(rid)
    monkeypatch.setattr(service, 'full_report', lambda _: report)
    first = public_config(service, rid)
    facts = report.inference.configured
    attempt = ReportInferenceEffectiveAttemptItem(
        attempt_id='attempt',
        source_ref='effective.json',
        provider=facts.provider,
        model=facts.model,
        effort=facts.effort,
    )
    report.inference.effective.attempts = [attempt.model_copy() for _ in range(1000)]
    assert public_config(service, rid) == first


def test_display_limit_and_native_digest_are_enforced(client: TestClient, tmp_path: Path) -> None:
    """Verify display limit and native digest are enforced."""
    rid = publish_config(client, tmp_path)
    service = service_for(client)
    original = service.config
    service.config = replace(original, limits=replace(original.limits, config_display_bytes=10))
    assert client.get('/api/v1/runs/' + rid + '/config').json()['reason'] == 'display_limit'
    service.config = original
    service.store.put(service.runs[rid]['prefix'] + 'data/agent/opencode.json', b'{"model":"forged"}')
    response = client.get('/api/v1/runs/' + rid + '/config')
    assert response.status_code == 422 and response.json()['error']['code'] == 'stored_digest_mismatch'


@pytest.mark.parametrize(
    'native',
    [
        {'model': 'x', 'env': {'KEY': 'MUST_NOT_LEAK'}},
        {'provider': {'openai': {'options': {'apiKey': 'MUST_NOT_LEAK'}}}},
        {'mcp': {'search': {'headers': {'Authorization': 'Bearer MUST_NOT_LEAK'}}}},
        {'mcp': {'search': {'environment': {'OPENAI_API_KEY': 'MUST_NOT_LEAK'}}}},
        {'instructions': ['/Users/private/rules.md']},
        {'skills': {'paths': ['~/private']}},
        {'provider': {'custom': {'options': {'baseURL': 'https://user:password@example.org'}}}},
        {'provider': {'custom': {'options': {'baseURL': 'https://example.org?token=MUST_NOT_LEAK'}}}},
    ],
)
def test_private_native_config_is_refused_instead_of_silently_truncated(native: dict) -> None:
    """Verify private native config is refused instead of silently truncated."""
    with pytest.raises(ValueError):
        native_projection(native)


def test_environment_bound_native_mcp_is_preserved() -> None:
    """Verify environment bound native mcp is preserved."""
    native = {
        'mcp': {
            'search': {
                'type': 'local',
                'command': ['/sandbox-home/.local/bin/search'],
                'environment': {'API_KEY': '{env:SEARCH_KEY}'},
            }
        }
    }
    assert native_projection(native) == native


def test_native_default_agent_selector_and_its_model_options_are_preserved() -> None:
    """Verify native default agent selector and its model options are preserved."""
    native = {
        'default_agent': 'reviewer',
        'agent': {'reviewer': {'model': 'openai/selected', 'options': {'reasoningEffort': 'high'}}},
    }
    assert native_projection(native) == native


def test_native_config_display_rules_match_offline_report() -> None:
    """Both independently installed consumers reject the same private config fields."""
    root = Path(__file__).parents[2]
    assert (root / 'viewer/src/presentation/native_config.py').read_bytes() == (
        root / 'benchmark/src/presentation/native_config.py'
    ).read_bytes()
