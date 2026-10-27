"""Native sealing precedes host scoring and cannot be replaced by a retry."""

import copy
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from python_fixtures import freeze_python, python_probe
from test_basket import payload
from test_telemetry import native_db

from benchmark.config import RunConfig, load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.agent_input import freeze_agent_input
from benchmark.execution.provisioning import Runtime
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.transcript import Transcript
from benchmark.metrics.scoring import metric_receipt
from benchmark.models.base import ArtifactReceipt
from benchmark.models.basket import BasketRecord, BasketV3
from benchmark.models.evidence import (
    NativeIdentity,
    ScoringEvidence,
    Seal,
    StreamReceipt,
)
from benchmark.models.report import ReportAttempt
from benchmark.models.runtime import ProvisioningReceipt
from benchmark.models.triplets import TripletIndex
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.layout import TaskRegistration, attempt_path, task_registry
from benchmark.persistence.storage import read_json, write_json
from benchmark.runtime import attempt as executor
from benchmark.runtime.commands import Command
from benchmark.runtime.lifecycle import Stop
from benchmark.vcs.git import TreeEntry


def fixture_runtime() -> Runtime:
    """Provide a concrete verified-runtime receipt to the offline executor."""
    return Runtime(
        Path('/usr/bin/true'),
        ProvisioningReceipt(
            selector='opencode:v1.18.33',
            actual_version='1.18.33',
            asset_name='fixture',
            platform='fixture',
            archive_sha256='a' * 64,
            binary_sha256='c' * 64,
        ),
    )


class MemoryCache:
    """Expose fixed before/human blobs without contacting source hosting."""

    timeout = 5
    stop: Stop | None = None

    def __init__(self, before: str) -> None:
        """Bind the exact frozen before revision used by the fixture."""
        self.before = before

    def ensure(self, revisions: list[str], *, fetch_refs: list[str]) -> None:
        """Provide the already-owned fixture objects without discovery."""

    def verify_reference(self, row: BasketRecord) -> None:
        """Keep collector revision validation outside this native lifecycle fixture."""

    def tree(self, revision: str) -> dict[str, TreeEntry]:
        """Make the after tree differ while keeping it outside native source."""
        return {'new/source.py': TreeEntry('100644', 'before' if revision == self.before else 'human')}

    def blob(self, entry: TreeEntry) -> bytes:
        """Return distinct source contents for agent isolation assertions."""
        return b'base = 1\n' if entry.oid == 'before' else b'human = 2\n'


def reservation(
    tmp_path: Path, *, agent_overrides: dict | None = None
) -> tuple[RunConfig, TaskRegistration, NativeIdentity, ReportAttempt, Command]:
    """Prepare a coordinator-owned numbered directory and immutable start."""
    data = BasketV3.model_validate(payload())
    row = data.tasks[0]
    config_path = tmp_path / 'config.yaml'
    agent = {
        'name': 'fixture',
        'runtime': 'opencode:v1.18.33',
        'config': {'opencode_json': {'model': 'fixture/model'}},
        'retries': {'max_retries': 3, 'categories': ['agent_error', 'no_changes']},
    }
    agent.update(agent_overrides or {})
    config_path.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent': agent,
            }
        )
    )
    config = load_config(config_path)
    registration = task_registry([row], data.meta.repository)[0]
    write_json(tmp_path / 'run.json', {'agent_input': freeze_agent_input(tmp_path, config)})
    command = Command(tmp_path, 'run', progress_total=1)
    attempt = ReportAttempt(
        id='22222222-2222-4222-8222-222222222222',
        invocation_id=command.id,
        sequence=1,
        retry_index=0,
        started_at='2026-10-08T12:00:00Z',
        finished_at=None,
    )
    identity = NativeIdentity(
        run_id='11111111-1111-4111-8111-111111111111',
        basket_sha256='a' * 64,
        config_identity_sha256='b' * 64,
        runtime_binary_sha256='c' * 64,
        python_runtime=freeze_python(tmp_path),
        metric=metric_receipt(check_environment=False),
    )
    directory = tmp_path / attempt_path(registration, 1)
    directory.mkdir(parents=True)
    write_json(
        directory / 'start.json',
        {
            'schema_family': 'pr_similarity_os.start',
            'schema_version': 2,
            'run_id': identity.run_id,
            'task_id': row.id,
            'attempt': attempt,
            'basket_sha256': identity.basket_sha256,
            'config_identity_sha256': identity.config_identity_sha256,
        },
    )
    return config, registration, identity, attempt, command


def controlled_native(
    monkeypatch: pytest.MonkeyPatch, registration: TaskRegistration, *, errors: list[dict] | None = None
) -> list[Path]:
    """Use real stdout routing, SQLite capture, Git diff and triplet export offline."""
    cache = MemoryCache(registration.basket_row.revisions.before)
    monkeypatch.setattr(executor, 'GitCache', lambda *_args, **_kwargs: cache)
    monkeypatch.setattr(Sandbox, 'prepare_python', lambda *_args, **_kwargs: python_probe())
    monkeypatch.setattr(Sandbox, 'check_python', lambda *_args, **_kwargs: python_probe())
    homes = []

    def native_command(self: Sandbox, argv: list[str]) -> list[str]:
        """Substitute a provider-free child while retaining private native state."""
        if 'debug' in argv:
            return [sys.executable, '-c', 'print(\'{"model":"fixture/model"}\')']
        homes.append(self.home)
        database = self.home / 'data/opencode/opencode.db'
        database.parent.mkdir(parents=True, exist_ok=True)
        native_db(database).close()
        script = (
            'from pathlib import Path; import json; '
            'assert Path("source.py").read_text()=="base = 1\\n"; '
            'Path("source.py").write_text("agent = 3\\n"); '
            'print(json.dumps({"type":"text","sessionID":"root","part":{"text":"Finished fixture"}}))'
        )
        for error in errors or []:
            event = {'type': 'error', 'sessionID': 'root', 'error': error}
            script += '; print(' + repr(json.dumps(event)) + ')'
        script = 'import os; os.chdir(' + repr(str(self.cwd)) + '); ' + script
        return [sys.executable, '-c', script]

    monkeypatch.setattr(Sandbox, 'command', native_command)
    return homes


def test_native_seal_freezes_timing_inventory_and_real_run_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host scoring sees a closed native seal and cannot modify native timestamps."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    homes = controlled_native(monkeypatch, registration)
    directory = tmp_path / attempt_path(registration, 1)
    original_start = (directory / 'start.json').read_bytes()
    observed = []

    def scoring(*args: object, **kwargs: object) -> dict:
        """Observe sealing before injecting a host scoring failure."""
        seal = Seal.model_validate(read_json(directory / 'seal.json'))
        observed.append(copy.deepcopy(seal.task.final_attempt))
        assert seal.native_timing.duration_seconds is not None and seal.native_timing.duration_seconds > 0
        raise BenchmarkError('scoring_failed', 'Fixture host scoring failed.')

    monkeypatch.setattr(executor, 'score_saved', scoring)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
    finally:
        command.close()
    assert outcome.task.execution.status == 'completed'
    assert outcome.task.infrastructure_error is None
    assert outcome.scoring_error is not None
    assert outcome.scoring_error.code == 'scoring_failed'
    assert outcome.task.processing_status == 'completed'
    assert outcome.task.final_attempt == observed[0]
    assert attempt.finished_at is None and (directory / 'start.json').read_bytes() == original_start
    assert TripletIndex.model_validate(read_json(directory / 'triplets/index.json')).run_id == identity.run_id
    assert outcome.seal == reference(tmp_path, (directory / 'seal.json').relative_to(tmp_path).as_posix())
    paths = {item.path for item in Seal.model_validate(read_json(directory / 'seal.json')).artifact_receipts}
    assert not any(path.endswith('agent.log') or path.endswith('scoring.log') for path in paths)
    assert any(path.endswith('events.jsonl') for path in paths) and any(
        path.endswith('telemetry/usage.json') for path in paths
    )
    assert homes and not homes[0].exists()
    assert not (directory / 'ready.json').exists()


def test_registered_cancel_before_spawn_seals_without_native_retry(tmp_path: Path) -> None:
    """A reserved attempt survives cancellation with explicit not-started evidence."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    stop = Stop()
    stop.set()
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            stop,
            registration=registration,
        )
    finally:
        command.close()
    assert outcome.task.execution.status == 'not_started'
    assert outcome.task.processing_status == 'interrupted'
    assert outcome.task.telemetry.tokens.total is None
    assert (
        Seal.model_validate(read_json(tmp_path / attempt_path(registration, 1) / 'seal.json')).native_timing.started_at
        is None
    )


def test_complete_native_capture_scores_once_and_never_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real host scoring consumes exactly one usage capture and leaves native facts frozen."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    homes = controlled_native(monkeypatch, registration)
    captures = Mock(wraps=executor.capture_telemetry)
    monkeypatch.setattr(executor, 'capture_telemetry', captures)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
        directory = tmp_path / attempt_path(registration, 1)
        frozen = {name: (directory / name).read_bytes() for name in ('seal.json', 'outcome.json', 'scoring.json')}
        with pytest.raises(BenchmarkError, match='host recovery'):
            executor.run_attempt(
                config,
                registration.basket_row,
                tmp_path,
                identity,
                fixture_runtime(),
                Path('/usr/bin/bwrap'),
                command,
                attempt,
                Stop(),
                registration=registration,
            )
    finally:
        command.close()
    assert captures.call_count == 1 and len(homes) == 1
    assert outcome.task.scoring.status == 'available' and outcome.archive_complete
    assert outcome.task.telemetry.tokens.total == 64
    assert ScoringEvidence.model_validate(read_json(directory / 'scoring.json')).scoring == outcome.task.scoring
    for name, content in frozen.items():
        assert (directory / name).read_bytes() == content
    (directory / 'agent.log').write_text('Rebuilt human view\n')
    (directory / 'unrelated-note.txt').write_text('Unowned file\n')
    assert reference(tmp_path, (directory / 'seal.json').relative_to(tmp_path).as_posix()) == outcome.seal
    assert not any(item.path.endswith(('agent.log', 'unrelated-note.txt')) for item in outcome.artifact_receipts)


def test_raw_capture_and_error_sidecar_failure_preserve_auth_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory sink failure marks evidence incomplete without masking a later 401."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    controlled_native(monkeypatch, registration, errors=[{'name': 'APIError', 'data': {'statusCode': 401}}])
    directory = tmp_path / attempt_path(registration, 1)
    original_render = Transcript.render_event

    def broken(self: Transcript, event: dict, raw_line: int | None) -> None:
        """Replace raw and error sinks after the first event was classified."""
        if event['type'] == 'text':
            (directory / 'events.jsonl').unlink()
            (directory / 'events.jsonl').mkdir()
            (directory / 'agent-error.json').mkdir()
            raise ValueError('formatter failure')
        original_render(self, event, raw_line)

    monkeypatch.setattr(Transcript, 'render_event', broken)
    stop = Stop()
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            stop,
            registration=registration,
        )
    finally:
        command.close()
    assert outcome.fatal is not None
    assert stop.is_set() and outcome.fatal.code == 'auth_required'
    assert outcome.task.infrastructure_error is not None
    assert outcome.task.infrastructure_error.code == 'auth_required'
    assert not outcome.archive_complete
    seal = Seal.model_validate(read_json(directory / 'seal.json'))
    assert seal.evidence.streams is not None
    assert seal.evidence.streams.status == 'incomplete'
    assert seal.evidence.required is not None
    assert 'events.jsonl' in seal.evidence.required.missing


def test_formatter_failure_alone_preserves_native_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unavailable human transcript cannot invalidate capture or quality scoring."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    controlled_native(monkeypatch, registration)

    def broken(self: Transcript, event: dict, raw_line: int | None) -> None:
        """Inject a disposable presentation defect with all raw sinks intact."""
        raise ValueError('formatter failure')

    monkeypatch.setattr(Transcript, 'render_event', broken)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
    finally:
        command.close()
    assert outcome.archive_complete and outcome.task.scoring.status == 'available'
    assert (
        StreamReceipt.model_validate(
            read_json(tmp_path / attempt_path(registration, 1) / 'stream_receipt.json')
        ).transcript_complete
        is False
    )


def test_reservation_mismatch_refuses_native_launch(tmp_path: Path) -> None:
    """No guessed directory or mismatched attempt may reach native preparation."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    changed = attempt.model_copy(update={'id': '33333333-3333-4333-8333-333333333333'})
    try:
        with pytest.raises(BenchmarkError, match='reserved immutable start'):
            executor.run_attempt(
                config,
                registration.basket_row,
                tmp_path,
                identity,
                fixture_runtime(),
                Path('/usr/bin/bwrap'),
                command,
                changed,
                Stop(),
                registration=registration,
            )
    finally:
        command.close()
    assert not (tmp_path / attempt_path(registration, 1) / 'seal.json').exists()


def test_explicit_resource_inventory_contains_only_frozen_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native seal signs declared skills and instructions without directory globbing."""
    (tmp_path / 'rules.md').write_text('Fixture instruction.\n')
    skill = tmp_path / 'skill'
    skill.mkdir()
    (skill / 'SKILL.md').write_text('Fixture skill.\n')
    (skill / 'LICENSE').write_text('Fixture license.\n')
    config, registration, identity, attempt, command = reservation(
        tmp_path,
        agent_overrides={
            'config': {
                'opencode_json': {
                    'model': 'fixture/model',
                    'instructions': ['rules.md'],
                    'skills': {'paths': ['skill']},
                }
            }
        },
    )
    controlled_native(monkeypatch, registration)
    original = executor._Attempt.inventory

    def inventory(self: executor._Attempt) -> list[ArtifactReceipt]:
        """Add an unrelated file before capture closure to test the producer allowlist."""
        (self.artifacts / 'unowned.txt').write_text('Unowned fixture.\n')
        return original(self)

    monkeypatch.setattr(executor._Attempt, 'inventory', inventory)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
    finally:
        command.close()
    paths = {item.path for item in outcome.artifact_receipts}
    assert any(path.endswith('agent/resources/0-0-0/rules.md') for path in paths)
    assert any(path.endswith('agent/resources/2-0-0/skill/SKILL.md') for path in paths)
    assert any(path.endswith('agent/resources/2-0-0/skill/LICENSE') for path in paths)
    assert not any(path.endswith('runtime/unowned.txt') for path in paths)
    assert outcome.archive_complete


def test_known_credential_in_agent_source_is_withheld(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact source cannot become durable evidence when it includes a known credential."""
    secret = 'fixture-capture-credential-never-save'
    monkeypatch.setenv('PRSIM_CAPTURE_TEST_KEY', secret)
    config, registration, identity, attempt, command = reservation(
        tmp_path, agent_overrides={'credentials': {'CAPTURE_KEY': 'PRSIM_CAPTURE_TEST_KEY'}}
    )
    controlled_native(monkeypatch, registration)
    original = executor._Attempt.capture

    def capture(self: executor._Attempt) -> None:
        """Simulate an agent copying its private credential into a scored file."""
        (self.workspace.cwd / 'source.py').write_text(secret)
        original(self)

    monkeypatch.setattr(executor._Attempt, 'capture', capture)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
    finally:
        command.close()
    directory = tmp_path / attempt_path(registration, 1)
    assert not outcome.archive_complete
    assert outcome.task.infrastructure_error is not None
    assert outcome.task.infrastructure_error.code == 'capture_secret'
    assert outcome.task.artifacts.triplets_manifest is None
    assert not (directory / 'agent.diff').exists()
    for path in directory.rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_telemetry_failure_is_unknown_and_never_recaptured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure to save usage produces explicit incomplete capture and retains quality."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    controlled_native(monkeypatch, registration)
    captures = Mock(side_effect=BenchmarkError('artifact_write_failed', 'Fixture usage storage failed.'))
    monkeypatch.setattr(executor, 'capture_telemetry', captures)
    try:
        outcome = executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
    finally:
        command.close()
    assert captures.call_count == 1
    assert not outcome.archive_complete
    assert outcome.task.telemetry.tokens.total is None
    assert outcome.task.scoring.status == 'available'
    seal = Seal.model_validate(read_json(tmp_path / attempt_path(registration, 1) / 'seal.json'))
    assert seal.evidence.usage is not None
    assert seal.evidence.usage.reason == 'telemetry_receipt_failed'
    assert seal.evidence.required is not None
    assert 'telemetry/receipt.json' in seal.evidence.required.missing


def test_human_header_has_one_attempt_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Producer metadata and transcript rendering agree on one task/attempt header."""
    config, registration, identity, attempt, command = reservation(tmp_path)
    controlled_native(monkeypatch, registration)
    try:
        executor.run_attempt(
            config,
            registration.basket_row,
            tmp_path,
            identity,
            fixture_runtime(),
            Path('/usr/bin/bwrap'),
            command,
            attempt,
            Stop(),
            registration=registration,
        )
    finally:
        command.close()
    header = (tmp_path / attempt_path(registration, 1) / 'agent.log').read_text().splitlines()[0]
    assert 'Task 001' in header and 'fixture/model' in header
    assert header.count('attempt 1') == 1
