"""A real pinned binary receives native config inside the actual sandbox."""

import json
import sys
import threading
from pathlib import Path

import pytest
import yaml
from test_agent_input import native_object

from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.opencode import EventTracker, Invocation
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.transcript import Transcript
from benchmark.persistence.storage import read_json


def test_launch_events_record_root_without_db_lookup() -> None:
    """Verify launch events record root without db lookup."""
    tracker = EventTracker()
    tracker.accept('{"type":"step_start","sessionID":"root-session","part":{}}')
    tracker.accept('{"type":"error","sessionID":"root-session","error":{"name":"APIError"}}')
    assert tracker.root_session_id == 'root-session' and len(tracker.errors) == 1


def test_setup_runs_in_private_home_without_agent(tmp_path: Path) -> None:
    """Verify setup runs in private home without agent."""
    source = tmp_path / 'source'
    source.mkdir()
    script = tmp_path / 'setup.sh'
    script.write_text('echo installed > "$HOME/tool"\necho customization > source.py\n')
    data = {
        'schema_version': 1,
        'agent': {
            'name': 'test',
            'runtime': 'opencode:v1.18.33',
            'config': {'opencode_json': {'model': 'example/model'}},
            'setup': {'script': str(script), 'timeout_seconds': 5},
        },
    }
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    invocation = Invocation(load_config(path), sandbox, tmp_path / 'artifacts', tmp_path / 'logs')
    invocation.prepare('Task: a fixture')
    result = invocation.setup(threading.Event())
    assert result is not None and result.status == 'completed'
    assert (sandbox.home / 'tool').read_text().strip() == 'installed'
    assert (source / 'source.py').read_text().strip() == 'customization'
    assert not (source / 'prompt.txt').exists()


def test_changed_setup_cannot_mismatch_identity(tmp_path: Path) -> None:
    """Verify changed setup cannot mismatch identity."""
    source = tmp_path / 'source'
    source.mkdir()
    script = tmp_path / 'setup.sh'
    script.write_text('echo original\n')
    data = {
        'schema_version': 1,
        'agent': {
            'name': 'test',
            'runtime': 'opencode:v1.18.33',
            'config': {'opencode_json': {}},
            'setup': {'script': str(script)},
        },
    }
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    script.write_text('echo changed\n')
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    invocation = Invocation(config, sandbox, tmp_path / 'artifacts', tmp_path / 'logs')
    with pytest.raises(BenchmarkError) as caught:
        invocation.prepare('Task: fixture')
    assert caught.value.code == 'resource_changed'


def test_native_instruction_copy_is_relocated_and_verified(tmp_path: Path) -> None:
    """Verify native instruction copy is relocated and verified."""
    source = tmp_path / 'source'
    source.mkdir()
    rules = tmp_path / 'rules.md'
    rules.write_text('Follow this fixture instruction.\n')
    data = {
        'schema_version': 1,
        'agent': {
            'name': 'test',
            'runtime': 'opencode:v1.18.33',
            'config': {'opencode_json': {'instructions': ['rules.md']}},
        },
    }
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    invocation = Invocation(load_config(path), sandbox, tmp_path / 'artifacts', tmp_path / 'logs')
    invocation.prepare('Task: fixture')
    assert (sandbox.runtime_files / 'resources/0-0-0/rules.md').read_bytes() == rules.read_bytes()
    assert not (source / 'rules.md').exists()


def test_tdd_example_relocates_skill_and_archives_complete_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declared skills remain discoverable resources and never become scored source files."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-not-a-secret')
    examples = Path(__file__).resolve().parents[1] / 'examples'
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    invocation = Invocation(
        load_config(examples / 'openai-tdd.yaml'), sandbox, tmp_path / 'artifacts', tmp_path / 'logs'
    )
    invocation.prepare('Task: fix a behavior regression')
    native = read_json(sandbox.runtime_files / 'config.json')
    assert native_object(native['skills'])['paths'] == ['/benchmark-runtime/resources/2-0-0/test-driven-development']
    assert native['instructions'] == ['/benchmark-runtime/resources/0-0-0/agent-tdd.md']
    skill = Path('resources/2-0-0/test-driven-development')
    for name in ('SKILL.md', 'writing-good-tests.md', 'LICENSE', 'UPSTREAM.json'):
        expected = (examples / 'skills/test-driven-development' / name).read_bytes()
        assert (sandbox.runtime_files / skill / name).read_bytes() == expected
        assert (invocation.artifacts / 'runtime' / skill / name).read_bytes() == expected
    assert not list(source.iterdir())


def test_declared_resources_and_native_config_are_archived_separately(tmp_path: Path) -> None:
    """Verify declared resources and native config are archived separately."""
    source = tmp_path / 'source'
    source.mkdir()
    rules = tmp_path / 'rules.md'
    rules.write_text('Public fixture rules.\n')
    configpath = tmp_path / 'config.yaml'
    configpath.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent': {
                    'name': 'test',
                    'runtime': 'opencode:v1.18.33',
                    'config': {'opencode_json': {'instructions': ['rules.md']}},
                },
            }
        )
    )
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    invocation = Invocation(load_config(configpath), sandbox, tmp_path / 'artifacts', tmp_path / 'logs')
    invocation.prepare('Fixture task.')

    native = read_json(tmp_path / 'artifacts/runtime/config.json')
    assert native['instructions'] == ['/benchmark-runtime/resources/0-0-0/rules.md']
    assert (tmp_path / 'artifacts/runtime/resources/0-0-0/rules.md').read_bytes() == rules.read_bytes()
    launch = read_json(tmp_path / 'artifacts/launch.json')
    assert launch['prompt_transport'] == 'stdin' and launch['cwd'] == '.'


def test_known_credential_in_instructions_is_not_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify known credential in instructions is not saved."""
    source = tmp_path / 'source'
    source.mkdir()
    rules = tmp_path / 'rules.md'
    rules.write_text('fixture_secret_must_not_be_saved')
    configpath = tmp_path / 'config.yaml'
    configpath.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent': {
                    'name': 'test',
                    'runtime': 'opencode:v1.18.33',
                    'instructions_file': str(rules),
                    'config': {'opencode_json': {}},
                    'credentials': {'API_KEY': 'TEST_CREDENTIAL'},
                },
            }
        )
    )
    monkeypatch.setenv('TEST_CREDENTIAL', 'fixture_secret_must_not_be_saved')
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    invocation = Invocation(load_config(configpath), sandbox, tmp_path / 'artifacts', tmp_path / 'logs')
    with pytest.raises(BenchmarkError) as caught:
        invocation.prepare('Task')
    assert caught.value.code == 'resource_secret'
    assert not (tmp_path / 'artifacts/prompt.txt').exists()


def test_invocation_routes_native_output_and_classifies_after_formatter_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real launcher drain without requiring a provider or native binary."""
    path = tmp_path / 'config.yaml'
    path.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent': {
                    'name': 'test',
                    'runtime': 'opencode:v1.18.33',
                    'config': {'opencode_json': {'model': 'fixture/model'}},
                },
            }
        )
    )
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    native = [
        {'type': 'future', 'payload': 'retained'},
        {'type': 'text', 'sessionID': 'root', 'part': {'text': 'answer'}},
        {'type': 'error', 'sessionID': 'root', 'error': {'name': 'APIError', 'data': {'statusCode': 401}}},
    ]
    script = (
        'import sys; print('
        + repr('\n'.join(json.dumps(event) for event in native))
        + '); print("diagnostic",file=sys.stderr)'
    )
    monkeypatch.setattr(Sandbox, 'command', lambda _self, _argv: [sys.executable, '-c', script])

    def broken(self: Transcript, event: dict, raw_line: int | None) -> None:
        """Inject a presentation-only defect into the complete native launch path."""
        raise ValueError('formatter failed')

    monkeypatch.setattr(Transcript, 'render_event', broken)
    invocation = Invocation(
        load_config(path),
        sandbox,
        tmp_path / 'attempt',
        tmp_path / 'attempt',
        task_label='Task 003/100',
        attempt_sequence=2,
    )
    invocation._environment = {}
    result, tracker = invocation.run(b'fixture prompt', threading.Event())
    assert result.status == 'error' and result.exit_code == 0
    assert native_object(tracker.errors[0]['data'])['statusCode'] == 401
    assert tracker.root_session_id == 'root'
    assert 'transcript_failed' in result.warnings
    assert read_json(tmp_path / 'attempt/stream_receipt.json')['events_complete']
    assert len((tmp_path / 'attempt/events.jsonl').read_text().splitlines()) == 3
    assert '[stderr] diagnostic' in (tmp_path / 'attempt/runtime.log').read_text()


def test_conflicting_session_identity_is_unknown_without_losing_errors() -> None:
    """Multiple native root identities cannot silently bind telemetry to the first."""
    tracker = EventTracker()
    tracker.accept('{"type":"step_start","sessionID":"one"}')
    tracker.accept('{"type":"step_start","sessionID":"two"}')
    tracker.accept('{"type":"error","sessionID":"one","error":{"name":"APIError"}}')
    assert tracker.root_session_id is None
    assert tracker.warnings == ['ambiguous_root_session']
    assert len(tracker.errors) == 1
