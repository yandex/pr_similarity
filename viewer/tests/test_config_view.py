"""Native config and harness controls are separate, safe, digest-bound views."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest
from helpers import archive_fixture
from test_api import client, create_user
from viewer.agent_input import _identity_native
from viewer.archive import validate_archive
from viewer.config_view import native_projection, public_config
from viewer.inference import build_inference
from viewer.util import digest, json_bytes


def publish_config(client, tmp_path, tdd=False):
    native = {'model': 'openai/selected', 'permission': {'*': 'allow', '*.secret': 'deny'},
        'provider': {'openai': {'options': {'apiKey': '{env:OPENAI_API_KEY}'},
            'models': {'selected': {'options': {'reasoningEffort': 'medium'}}}}}}
    def configure(report, basket, files):
        identity = {'identity_version': 1, 'runtime': 'opencode:v1.18.33',
            'timeout_seconds': 1800, 'setup': None,
            'retries': {'max_retries': 2, 'categories': ['agent_error']},
            'diff_excludes': ['*.lock'], 'env': {'PRIVATE': 'MUST_NOT_LEAK'},
            'native_config': _identity_native(native), 'resources': [],
            'instructions': {'protocol': 'benchmark_task_v1', 'sha256': digest(b'')}}
        if tdd:
            content = b'Use a failing test first.\n'
            files['agent/resources/2-0-0/tdd/SKILL.md'] = content
            resource = {'field': 'paths', 'index': 0,
                'files': [{'path': 'SKILL.md', 'sha256': digest(content)}]}
            native['skills'] = {'paths': ['resources/2-0-0/tdd']}
            identity['native_config']['skills'] = {'paths': [{'resource': resource}]}
            identity['resources'] = [resource]
        files['agent/opencode.json'] = json_bytes(native)
        files['config_identity.json'] = json_bytes(identity)
        report['inference'] = build_inference(identity)
    if client.app.state.service.db.actor('alice') is None:
        create_user(client)
    path = tmp_path / ('tdd.tar.gz' if tdd else 'baseline.tar.gz')
    report, _ = archive_fixture(path, configure=configure, run_id='44444444-4444-4444-8444-000000000002' if tdd else None)
    service = client.app.state.service
    imported = validate_archive(path, tmp_path / ('tdd-data' if tdd else 'baseline-data'), service.config.limits)
    service.publish(imported, 'alice', 'tdd-config' if tdd else 'baseline-config')
    return report['run']['id']


def test_native_config_and_harness_metadata_are_separate(client, tmp_path):
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
    assert row['benchmark'] == {'runtime': 'opencode:v1.18.33', 'timeout_seconds': 1800,
        'setup_timeout_seconds': None, 'retries': {'max_retries': 2, 'categories': ['agent_error']},
        'diff_excludes': ['*.lock']}
    assert value['report_sha256'] == row['report_sha256']
    assert value['source']['path'] == 'agent/opencode.json'
    stale = client.get('/api/v1/runs/' + rid + '/config?expected_report_sha256=' + 'f' * 64)
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'run_changed'


def test_tdd_skills_are_visible_and_distinguish_the_native_config(client, tmp_path):
    baseline = publish_config(client, tmp_path)
    tdd = publish_config(client, tmp_path, tdd=True)
    first = client.get('/api/v1/runs/' + baseline + '/config').json()['config']
    second = client.get('/api/v1/runs/' + tdd + '/config').json()['config']
    assert first != second
    assert second['skills']['paths'] == ['resources/2-0-0/tdd']


def test_config_size_does_not_grow_with_effective_attempt_claims():
    raw = json_bytes({'model': 'openai/selected'})
    source = {'path': 'agent/opencode.json', 'size_bytes': len(raw), 'sha256': digest(raw)}
    row = {'prefix': 'fixture/', 'marker': {'archive_sha256': 'a' * 64}}
    report = {'agent_input': {'config': source}, 'config': {'identity_sha256': 'b' * 64},
        'inference': {'effective': {'attempts': []}}}
    # This boundary receives already validated report/storage evidence.
    service = SimpleNamespace(lock=threading.RLock(), check_report=lambda *_: 'c' * 64,
        full_report=lambda _: report, _require_run=lambda _: row,
        config=SimpleNamespace(limits=SimpleNamespace(json_bytes=1 << 20, config_display_bytes=65536)),
        store=SimpleNamespace(get=lambda *_: raw))
    first = public_config(service, 'fixture')
    report['inference']['effective']['attempts'] = [{} for _ in range(1000)]
    assert public_config(service, 'fixture') == first


def test_display_limit_and_native_digest_are_enforced(client, tmp_path):
    rid = publish_config(client, tmp_path)
    service = client.app.state.service
    original = service.config
    service.config = replace(original, limits=replace(original.limits, config_display_bytes=10))
    assert client.get('/api/v1/runs/' + rid + '/config').json()['reason'] == 'display_limit'
    service.config = original
    service.store.put(service.runs[rid]['prefix'] + 'data/agent/opencode.json', b'{"model":"forged"}')
    response = client.get('/api/v1/runs/' + rid + '/config')
    assert response.status_code == 422 and response.json()['error']['code'] == 'stored_digest_mismatch'


@pytest.mark.parametrize('native', [
    {'model': 'x', 'env': {'KEY': 'MUST_NOT_LEAK'}},
    {'provider': {'openai': {'options': {'apiKey': 'MUST_NOT_LEAK'}}}},
    {'mcp': {'search': {'headers': {'Authorization': 'Bearer MUST_NOT_LEAK'}}}},
    {'mcp': {'search': {'environment': {'OPENAI_API_KEY': 'MUST_NOT_LEAK'}}}},
    {'instructions': ['/Users/private/rules.md']},
    {'skills': {'paths': ['~/private']}},
    {'provider': {'custom': {'options': {'baseURL': 'https://user:password@example.org'}}}},
    {'provider': {'custom': {'options': {'baseURL': 'https://example.org?token=MUST_NOT_LEAK'}}}},
])
def test_private_native_config_is_refused_instead_of_silently_truncated(native):
    with pytest.raises(ValueError):
        native_projection(native)


def test_environment_bound_native_mcp_is_preserved():
    native = {'mcp': {'search': {'type': 'local', 'command': ['/sandbox-home/.local/bin/search'],
        'environment': {'API_KEY': '{env:SEARCH_KEY}'}}}}
    assert native_projection(native) == native


def test_native_default_agent_selector_and_its_model_options_are_preserved():
    native = {'default_agent': 'reviewer', 'agent': {'reviewer': {
        'model': 'openai/selected', 'options': {'reasoningEffort': 'high'}}}}
    assert native_projection(native) == native
