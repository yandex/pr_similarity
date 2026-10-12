"""Observable configuration validation and resume identity invariants."""

import shutil
from pathlib import Path

import pytest
import yaml
from benchmark.config import load_config
from benchmark.errors import BenchmarkError


@pytest.mark.parametrize('name', ['openai-api-key', 'chatgpt-oauth', 'openai-endpoint', 'openai-tdd'])
def test_quickstart_examples_are_valid_without_credentials(name: str) -> None:
    """Ship configs accepted by the owning parser without login, network or embedded secrets."""
    path = Path(__file__).resolve().parents[1] / 'examples' / (name + '.yaml')
    config = load_config(path)
    assert config.data['workers'] == 1
    assert config.data['agent']['runtime'] == 'opencode:v1.18.33'
    expected = 'chatgpt_oauth' if name == 'chatgpt-oauth' else 'api_key'
    assert config.data['agent']['auth']['mode'] == expected


def test_tdd_skill_contents_participate_in_resume_identity(tmp_path: Path) -> None:
    """A customized agent cannot silently resume after its declared skill changes."""
    examples = Path(__file__).resolve().parents[1] / 'examples'
    shutil.copytree(examples, tmp_path / 'examples')
    path = tmp_path / 'examples/openai-tdd.yaml'
    before = load_config(path)
    skill = path.parent / 'skills/test-driven-development/SKILL.md'
    skill.write_text(skill.read_text() + '\nAdditional local workflow guidance.\n')
    after = load_config(path)
    assert before.identity() != after.identity()


def write_config(directory: Path, **overrides: object) -> Path:
    """Write a minimal native agent configuration with optional overrides."""
    data = {
        'schema_version': 1,
        'agent': {
            'name': 'test',
            'runtime': 'opencode:v1.18.33',
            'config': {'opencode_json': {'model': 'example/model'}},
        },
    }
    data.update(overrides)
    path = directory / 'config.yaml'
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    return path


def test_defaults_and_technical_overrides(tmp_path: Path) -> None:
    """Verify defaults and technical overrides."""
    path = write_config(tmp_path)
    one = load_config(path)
    two = load_config(path, workers=3)
    assert one.data['workers'] == 1
    assert two.data['workers'] == 3
    assert one.data['agent']['auth']['mode'] == 'api_key'
    assert one.identity() == two.identity()
    assert one.data['provisioning']['cache_dir'].startswith('/')
    assert one.identity()['instructions']['protocol'] == 'benchmark_task_v1'


@pytest.mark.parametrize(
    'text',
    [
        'schema_version: 1\nschema_version: 1\n',
        'schema_version: 1\nworkers: true\n',
        'schema_version: 1\nworkers: 1.0\n',
        'schema_version: 1\nlog_level: DEBUG\n',
        'schema_version: 1\nschema_family: wrong\n',
        'schema_version: 1\nagent: !!python/object:bad {}\n',
        'schema_version: 1\nworkers: .nan\n',
    ],
)
def test_reject_ambiguous_config(tmp_path: Path, text: str) -> None:
    """Verify reject ambiguous config."""
    path = tmp_path / 'invalid.yaml'
    if 'agent:' not in text:
        text += 'agent: {name: test, runtime: opencode:v1.18.33, config: {opencode_json: {}}}\n'
    path.write_text(text)
    with pytest.raises(BenchmarkError):
        load_config(path)


def test_input_and_native_permission_order_affect_identity(tmp_path: Path) -> None:
    """Verify input and native permission order affect identity."""
    path = write_config(tmp_path)
    baseline = load_config(path).identity()
    data = yaml.safe_load(path.read_text())
    data['agent']['timeout_seconds'] = 3
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).identity() != baseline
    data['agent']['config']['opencode_json']['permission'] = [{'a': 'allow'}, {'b': 'deny'}]
    path.write_text(yaml.safe_dump(data))
    ordered = load_config(path).identity()
    data['agent']['config']['opencode_json']['permission'].reverse()
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).identity() != ordered


def test_setup_digest_follows_content_not_path(tmp_path: Path) -> None:
    """Verify setup digest follows content not path."""
    path = write_config(tmp_path)
    script = tmp_path / 'first.sh'
    script.write_text('echo ready\n')
    data = yaml.safe_load(path.read_text())
    data['agent']['setup'] = {'script': 'first.sh'}
    path.write_text(yaml.safe_dump(data))
    baseline = load_config(path).identity()
    second = tmp_path / 'second.sh'
    second.write_bytes(script.read_bytes())
    data['agent']['setup']['script'] = 'second.sh'
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).identity() == baseline
    second.write_text('echo changed\n')
    assert load_config(path).identity() != baseline


def test_native_instructions_are_content_bound(tmp_path: Path) -> None:
    """Verify native instructions are content bound."""
    path = write_config(tmp_path)
    instructions = tmp_path / 'rules.txt'
    instructions.write_text('Use type hints.\n')
    data = yaml.safe_load(path.read_text())
    data['agent']['config']['opencode_json']['instructions'] = ['rules.txt']
    path.write_text(yaml.safe_dump(data))
    before = load_config(path).identity()
    instructions.write_text('Use different rules.\n')
    assert load_config(path).identity() != before


@pytest.mark.parametrize(
    'env,credentials',
    [
        ({'HOME': '/elsewhere'}, {}),
        ({'OPENCODE_CONFIG_CONTENT': '{}'}, {}),
        ({'GIT_DIFF_OPTS': 'anything'}, {}),
        ({'OPENAI_API_KEY': 'value'}, {'OPENAI_API_KEY': 'HOST_KEY'}),
    ],
)
def test_reserved_and_colliding_environment(tmp_path: Path, env: dict, credentials: dict) -> None:
    """Verify reserved and colliding environment."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent'].update(env=env, credentials=credentials)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(BenchmarkError):
        load_config(path)


def test_embedded_secret_rejected_without_echoing_value(tmp_path: Path) -> None:
    """Verify embedded secret rejected without echoing value."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent']['config']['opencode_json']['provider'] = {'test': {'options': {'apiKey': 'DO_NOT_ECHO'}}}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(BenchmarkError) as caught:
        load_config(path)
    assert 'DO_NOT_ECHO' not in str(caught.value)


def test_credentials_rotate_without_entering_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify credentials rotate without entering identity."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent']['credentials'] = {'OPENAI_API_KEY': 'TEST_HOST_KEY'}
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv('TEST_HOST_KEY', 'first-secret')
    one = load_config(path)
    monkeypatch.setenv('TEST_HOST_KEY', 'second-secret')
    two = load_config(path)
    assert one.identity() == two.identity()
    assert 'first-secret' not in str(one.data)
    assert 'second-secret' not in str(two.identity())
    assert two.credential_environment()['OPENAI_API_KEY'] == 'second-secret'


def test_oauth_mode_is_semantic_profile_is_technical(tmp_path: Path) -> None:
    """Verify oauth mode is semantic profile is technical."""
    path = write_config(tmp_path)
    api = load_config(path).identity()
    data = yaml.safe_load(path.read_text())
    data['agent']['auth'] = {'mode': 'chatgpt_oauth', 'profile': 'private-a'}
    path.write_text(yaml.safe_dump(data))
    oauth = load_config(path).identity()
    assert oauth != api
    data['agent']['auth']['profile'] = 'private-b'
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).identity() == oauth


def test_oauth_rejects_conflicting_openai_key(tmp_path: Path) -> None:
    """Verify oauth rejects conflicting openai key."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent'].update(
        auth={'mode': 'chatgpt_oauth', 'profile': 'private'}, credentials={'OPENAI_API_KEY': 'HOST_KEY'}
    )
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(BenchmarkError):
        load_config(path)


def test_native_credential_reference_is_not_semantic(tmp_path: Path) -> None:
    """Verify native credential reference is not semantic."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent']['config']['opencode_json']['provider'] = {
        'test': {'options': {'apiKey': '{env:HOST_FIRST}', 'baseURL': 'https://api.example/v1'}}
    }
    path.write_text(yaml.safe_dump(data))
    first = load_config(path).identity()
    data['agent']['config']['opencode_json']['provider']['test']['options']['apiKey'] = '{env:HOST_SECOND}'
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).identity() == first
    data['agent']['config']['opencode_json']['provider']['test']['options']['baseURL'] = 'https://other.example/v1'
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).identity() != first


def test_plaintext_secret_cannot_use_nonsensitive_env(tmp_path: Path) -> None:
    """Verify plaintext secret cannot use nonsensitive env."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent']['env'] = {'OPENAI_API_KEY': 'DO_NOT_ECHO'}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(BenchmarkError) as caught:
        load_config(path)
    assert 'DO_NOT_ECHO' not in str(caught.value)


def test_duplicate_keys_and_recursive_aliases_in_native_config(tmp_path: Path) -> None:
    """Verify duplicate keys and recursive aliases in native config."""
    path = tmp_path / 'invalid.yaml'
    for text in [
        'schema_version: 1\nagent: {config: {opencode_json: {a: 1, a: 2}}}',
        'schema_version: 1\nagent: &self {config: *self}',
    ]:
        path.write_text(text)
        with pytest.raises(BenchmarkError):
            load_config(path)


def test_permission_mapping_order_is_semantic(tmp_path: Path) -> None:
    """Verify permission mapping order is semantic."""
    path = write_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['agent']['config']['opencode_json']['permission'] = {'bash': {'*': 'allow', 'rm *': 'deny'}}
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    before = load_config(path).identity()
    data['agent']['config']['opencode_json']['permission']['bash'] = {'rm *': 'deny', '*': 'allow'}
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    assert load_config(path).identity() != before


def test_setup_tags_preserve_order_case_duplicates_and_exclude_identity(tmp_path: Path) -> None:
    """User labels are presentation metadata, including prefix-shaped labels."""
    plain = load_config(write_config(tmp_path))
    tags = ['model:custom', 'Baseline', 'Baseline', ' effort:high ']
    labelled = load_config(write_config(tmp_path, tags=tags))
    assert labelled.data['tags'] == tags
    assert plain.data['tags'] == []
    assert plain.identity() == labelled.identity()


@pytest.mark.parametrize('tags', [[''], [' '], ['bad\nlabel'], ['bad\x7flabel'], ['x' * 257], ['x'] * 129, [1]])
def test_invalid_setup_tags_rejected(tmp_path: Path, tags: list) -> None:
    """Reject invalid labels before any runtime or run artifacts exist."""
    with pytest.raises(BenchmarkError):
        load_config(write_config(tmp_path, tags=tags))
