"""Frozen native inputs retain meaning, resource ownership and relocatability."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from benchmark.agent_input import freeze_agent_input, save_effective, validate_agent_input
from benchmark.artifact_refs import reference
from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.storage import read_json, write_json
from benchmark.opencode import Invocation
from benchmark.sandbox import Sandbox


def config_fixture(tmp_path: Path):
    """Declare real local resources and order-sensitive native permissions."""
    skills = tmp_path / 'tdd'
    skills.mkdir()
    (skills / 'SKILL.md').write_text('Use a failing test first.\n')
    (tmp_path / 'rules.md').write_text('Read the task.\n')
    native = {
        'model': 'openai/fixture',
        'provider': {'openai': {'options': {'apiKey': '{env:OPENAI_API_KEY}'}}},
        'permission': {'*': 'allow', '*.secret': 'deny'},
        'skills': {'paths': ['tdd']},
        'instructions': ['rules.md'],
    }
    path = tmp_path / 'agent.yaml'
    path.write_text(yaml.safe_dump({'schema_version': 1, 'agent': {
        'name': 'fixture', 'runtime': 'opencode:v1.18.33', 'config': {'opencode_json': native},
    }}, sort_keys=False))
    return load_config(path)


def test_frozen_input_is_portable_native_json_with_ordered_permissions(tmp_path):
    config = config_fixture(tmp_path)
    run = tmp_path / 'run'
    run.mkdir()
    selected = freeze_agent_input(run, config)
    native = read_json(run / selected['config']['path'])
    assert native['provider']['openai']['options']['apiKey'] == '{env:OPENAI_API_KEY}'
    assert list(native['permission']) == ['*', '*.secret']
    assert native['skills']['paths'] == ['resources/2-0-0/tdd']
    assert native['instructions'] == ['resources/0-0-0/rules.md']
    assert str(tmp_path) not in json.dumps(native)
    validate_agent_input(run, selected, config.identity())
    moved = tmp_path / 'moved'
    run.rename(moved)
    validate_agent_input(moved, selected, config.identity())


def test_changed_resource_after_loader_cannot_enter_frozen_input(tmp_path):
    config = config_fixture(tmp_path)
    (tmp_path / 'tdd/SKILL.md').write_text('Changed after validation.\n')
    run = tmp_path / 'run'
    run.mkdir()
    with pytest.raises(BenchmarkError, match='resource'):
        freeze_agent_input(run, config)


@pytest.mark.parametrize('field', ['model', 'permission', 'resource', 'foreign', 'secret'])
def test_rehashed_tampering_cannot_change_semantic_input(tmp_path, field):
    config = config_fixture(tmp_path)
    run = tmp_path / 'run'
    run.mkdir()
    selected = freeze_agent_input(run, config)
    native = read_json(run / selected['config']['path'])
    if field == 'model':
        native['model'] = 'openai/other'
    elif field == 'permission':
        native['permission'] = dict(reversed(list(native['permission'].items())))
    elif field == 'resource':
        receipt = next(r for r in selected['resources'] if r['path'].endswith('SKILL.md'))
        (run / receipt['path']).write_text('Forged resource.\n')
        receipt.update(reference(run, receipt['path']))
    elif field == 'secret':
        native['provider']['openai']['options']['apiKey'] = 'literal-credential'
    else:
        native['instructions'] = ['../foreign.md']
    write_json(run / selected['config']['path'], native)
    selected['config'] = reference(run, selected['config']['path'])
    with pytest.raises(BenchmarkError):
        validate_agent_input(run, selected, config.identity())


def test_effective_snapshot_is_atomic_and_shared_across_concurrent_attempts(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    native = {'model': 'openai/fixture', 'instructions': ['/benchmark-runtime/resources/0-0-0/rules.md']}
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _: save_effective(run, native), range(16)))
    assert all(ref == refs[0] for ref in refs)
    assert len(list(run.glob('agent/effective/*.json'))) == 1
    assert read_json(run / refs[0]['path'])['instructions'] == ['resources/0-0-0/rules.md']
    assert save_effective(run, {**native, 'model': 'openai/other'}) != refs[0]


def test_glob_resources_keep_their_single_semantic_input_slot(tmp_path):
    config = config_fixture(tmp_path)
    (tmp_path / 'extra.md').write_text('Another rule.\n')
    data = yaml.safe_load((tmp_path / 'agent.yaml').read_text())
    data['agent']['config']['opencode_json']['instructions'] = ['*.md']
    (tmp_path / 'agent.yaml').write_text(yaml.safe_dump(data, sort_keys=False))
    config = load_config(tmp_path / 'agent.yaml')
    run = tmp_path / 'run'
    run.mkdir()
    selected = freeze_agent_input(run, config)
    assert len(validate_agent_input(run, selected, config.identity())['instructions']) == 2


def test_ten_task_preparations_use_one_frozen_input_without_rereading_host_resources(tmp_path):
    """Each private launcher uses the frozen snapshot; durable resources stay shared."""
    config = config_fixture(tmp_path)
    run = tmp_path / 'run'
    run.mkdir()
    selected = freeze_agent_input(run, config)
    frozen_skill = (run / 'agent/resources/2-0-0/tdd/SKILL.md').read_bytes()
    (tmp_path / 'tdd/SKILL.md').write_text('Host resource changed after freeze.\n')
    for task in range(10):
        private = tmp_path / f'private-{task}'
        source = private / 'source'
        source.mkdir(parents=True)
        sandbox = Sandbox('/usr/bin/bwrap', source, source, private / 'home', private / 'runtime', Path('/usr/bin/true'))
        attempt = run / f'tasks/{task + 1:03d}-fixture/attempts/001'
        invocation = Invocation(config, sandbox, attempt, attempt, run_directory=run, agent_input=selected)
        invocation.prepare(f'Task {task}')
        assert (sandbox.runtime_files / 'resources/2-0-0/tdd/SKILL.md').read_bytes() == frozen_skill
        assert not (attempt / 'runtime').exists()
        assert read_json(attempt / 'launch.json')['agent_input'] == selected
    assert len(list(run.rglob('SKILL.md'))) == 1
    assert len(list(run.glob('agent/opencode.json'))) == 1


def test_effective_snapshot_rejects_a_symlink_ancestor_before_writing(tmp_path):
    run = tmp_path / 'run'
    outside = tmp_path / 'outside'
    run.mkdir()
    outside.mkdir()
    (run / 'agent').symlink_to(outside, target_is_directory=True)
    with pytest.raises(BenchmarkError):
        save_effective(run, {'model': 'openai/fixture'})
    assert list(outside.iterdir()) == []
