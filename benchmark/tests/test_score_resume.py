"""Host recovery selects scoring operations while native UUIDs, timing and outcomes stay fixed."""

import os
import signal
from pathlib import Path
from typing import NoReturn

import pytest
import yaml
from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.opencode import EventTracker
from benchmark.execution.provisioning import Runtime
from benchmark.json_types import JsonObject
from benchmark.models import Report, State
from benchmark.models.evidence import OperationSelection, Seal
from benchmark.models.triplets import TripletIndex
from benchmark.persistence.artifact_refs import read_reference
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json, run_lock
from benchmark.publication.archive import create_archive, verify_archive
from benchmark.runtime import attempt as executor
from benchmark.runtime import coordinator, recovery
from benchmark.runtime.commands import Command
from benchmark.runtime.lifecycle import Stop
from test_artifact_workflow import _inputs
from test_operation_workflows import stopped_run
from test_runner import fake_attempt, provision_fixture


@pytest.mark.parametrize('score_fails', [False, True])
def test_cancelled_sealed_resume_announces_actual_native_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, score_fails: bool
) -> None:
    """Plan paid work after host recovery using a real cancelled native attempt and immutable evidence."""
    inputs = _inputs(tmp_path)
    config_path = tmp_path / 'config.yaml'
    data = yaml.safe_load(config_path.read_text())
    data['agent']['credentials'] = {'OFFLINE_HOLD': 'PRSIM_CRASH_NATIVE'}
    config_path.write_text(yaml.safe_dump(data))
    config = load_config(config_path)
    monkeypatch.setenv('PRSIM_CRASH_NATIVE', 'hold-native-process-for-crash-test')
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: inputs.runtime)
    original_event = EventTracker.accept_event
    native_calls = []

    def event(self: EventTracker, value: JsonObject) -> None:
        """Cancel only after the real fixture has produced source changes and started native work."""
        original_event(self, value)
        if value.get('type') == 'step_start':
            native_calls.append(value)
            if len(native_calls) == 1:
                os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(EventTracker, 'accept_event', event)
    code, directory = coordinator.run(config, inputs.dataset, output=tmp_path / 'runs', progress='off')
    assert code == 130 and len(native_calls) == 1
    store = RunStore.open(directory)
    row = store.snapshot().tasks[0]
    selected = store.state.tasks[0]
    assert row.execution.status == 'cancelled' and row.scoring.status != 'available'
    assert selected.seal is not None and selected.outcome is not None
    frozen = {ref.path: (directory / ref.path).read_bytes() for ref in (selected.seal, selected.outcome)}
    before_log = (directory / 'run.log').read_text()
    expected = int(not score_fails)
    final_plan = (
        f'Resume plan after host recovery: reused {int(score_fails)}; '
        f'host-only recovery 0; new attempts {expected}; blocked 0.'
    )

    def provision(*_args: object, **_kwargs: object) -> Runtime:
        """Require the corrected final plan before any new provisioning or model work."""
        assert not score_fails
        assert final_plan in (directory / 'run.log').read_text()[len(before_log) :]
        return inputs.runtime

    def fail_score(*_args: object, **_kwargs: object) -> NoReturn:
        """Keep the existing policy for a failed host recovery unchanged."""
        raise BenchmarkError('scoring_failed', 'Fixture recovery failure.')

    if score_fails:
        monkeypatch.setattr(recovery, 'score_saved', fail_score)
    monkeypatch.setenv('PRSIM_CRASH_NATIVE', 'continue-native-process-after-crash-test')
    monkeypatch.setattr(coordinator, 'provision_opencode', provision)
    code, resumed = coordinator.run(config, inputs.dataset, resume=directory, progress='off')
    assert resumed == directory
    if not score_fails:
        assert code == 0
    log = (directory / 'run.log').read_text()[len(before_log) :]
    assert 'host recovery 1; new attempts at least 0' in log
    assert 'no agent provisioning' not in log
    assert final_plan in log
    assert len(native_calls) == 1 + expected
    assert all((directory / path).read_bytes() == content for path, content in frozen.items())


def test_host_recovery_keeps_same_attempt_without_runtime_auth_or_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Complete one failed host score using only already-selected sealed source."""
    directory, config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    before = RunStore.open(directory).snapshot().tasks[0]
    selected = RunStore.open(directory).state.tasks[0]
    assert selected.outcome is not None
    outcome_path = directory / selected.outcome.path
    native_bytes = outcome_path.read_bytes()
    assert selected.seal is not None
    seal = read_reference(directory, selected.seal, Seal)

    def forbidden(*args: object, **kwargs: object) -> object:
        """Reject runtime provisioning, OAuth refresh and any model execution."""
        raise AssertionError('Host-only recovery must not invoke native execution or authentication.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    monkeypatch.setattr(executor, '_oauth', forbidden)
    monkeypatch.setattr(executor, 'run_attempt', forbidden)
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        command = Command(directory, 'run', level='INFO', progress_mode='plain', progress_total=1)
        try:
            report = recovery.recover_host_scores(store, config, command, Stop())
            after = report.tasks[0]
            assert after.scoring.status == 'available'
            assert after.final_attempt == before.final_attempt
            assert after.execution == before.execution and after.telemetry == before.telemetry
            operation = store.last_operation('rescore')
            assert operation is not None
            again = recovery.recover_host_scores(store, config, command, Stop())
            assert again == report and store.last_operation('rescore') == operation
            assert command.progress is not None
            command.progress.poll(final=True)
        finally:
            command.close()
    text = capsys.readouterr().err
    assert f'Mf={after.scoring.Mf_mlp:.3f} LCf={after.scoring.LCf_mlp:.3f}' in text
    assert outcome_path.read_bytes() == native_bytes
    assert read_reference(directory, selected.seal, Seal).native_timing == seal.native_timing
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1
    assert not list(outcome_path.parent.glob('outcome_before*'))


def test_repeated_host_failure_stays_explicit_without_native_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new host failure keeps native facts intact and never consumes native retry budget."""
    directory, config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    selected = RunStore.open(directory).state.tasks[0]
    assert selected.outcome is not None
    outcome = (directory / selected.outcome.path).read_bytes()

    def fail_score(*args: object, **kwargs: object) -> dict:
        """Simulate an independent metric process error on saved source."""
        raise BenchmarkError('scoring_failed', 'Fixture host recovery failed.')

    monkeypatch.setattr(recovery, 'score_saved', fail_score)
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        command = Command(directory, 'run', level='ERROR', progress_total=1)
        try:
            report = recovery.recover_host_scores(store, config, command, Stop())
        finally:
            command.close()
        current = store.state.tasks[0]
        assert current.attempt == selected.attempt and current.retry_index == selected.retry_index
        assert report.tasks[0].scoring.error is not None
        assert report.tasks[0].scoring.error.code == 'scoring_failed'
        assert report.tasks[0].infrastructure_error is None
        assert report.tasks[0].processing_status == 'completed'
    assert selected.outcome is not None
    assert (directory / selected.outcome.path).read_bytes() == outcome


def test_damaged_sealed_source_is_rejected_before_any_new_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed source corruption blocks recovery rather than selecting a fabricated score."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    store = RunStore.open(directory)
    row = store.snapshot().tasks[0]
    assert row.artifacts.triplets_manifest is not None
    manifest = directory / row.artifacts.triplets_manifest
    entry = next(
        item for item in TripletIndex.model_validate(read_json(manifest)).entries if item.path == 'new/source.py'
    )
    assert entry.files.ai is not None
    (manifest.parent / entry.files.ai.path).write_text('damaged')
    with pytest.raises(BenchmarkError):
        RunStore.open(directory)
    saved_attempt = State.model_validate(read_json(directory / '_meta/state.json')).tasks[0].attempt
    assert saved_attempt is not None and row.final_attempt is not None
    assert saved_attempt.id == row.final_attempt.id


def test_real_resume_coordinator_uses_host_recovery_without_native_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public resume entrypoint restores a failed score without credentials or runtime."""
    directory, config, dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    original = RunStore.open(directory).state.tasks[0]
    assert original.outcome is not None
    frozen = (directory / original.outcome.path).read_bytes()

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        """Reject any attempt to turn host score recovery into an agent invocation."""
        raise AssertionError('Native preflight or execution is forbidden for host-only recovery.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    monkeypatch.setattr(executor, '_oauth', forbidden)
    code, restored = coordinator.run(config, dataset, resume=directory, attempt_function=forbidden, progress='off')
    assert code == 0 and restored == directory
    current = RunStore.open(directory)
    assert current.snapshot().tasks[0].scoring.status == 'available'
    assert current.state.tasks[0].attempt == original.attempt
    assert original.outcome is not None
    assert (directory / original.outcome.path).read_bytes() == frozen


@pytest.mark.parametrize('score_fails', [False, True])
def test_quota_host_recovery_keeps_native_retry_and_immutable_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, score_fails: bool
) -> None:
    """Rescoring a quota-stopped partial solution must not complete its native task."""
    directory, config, _dataset = stopped_run(tmp_path, monkeypatch, quota=True)
    selected = RunStore.open(directory).state.tasks[0]
    assert selected.seal is not None and selected.outcome is not None
    frozen = {ref.path: (directory / ref.path).read_bytes() for ref in [selected.seal, selected.outcome]}

    def fail_score(*args: object, **kwargs: object) -> NoReturn:
        """Fail offline scoring without exhausting the pending native retry."""
        raise BenchmarkError('scoring_failed', 'Synthetic score failure.')

    if score_fails:
        monkeypatch.setattr(recovery, 'score_saved', fail_score)
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        command = Command(directory, 'run', level='ERROR', progress_total=1)
        stop = Stop()
        try:
            report = recovery.recover_host_scores(store, config, command, stop)
        finally:
            command.close()
        row = report.tasks[0]
        assert row.processing_status == 'retry_pending'
        assert row.execution.status == 'error'
        assert row.scoring.status == ('unavailable' if score_fails else 'available')
        assert not stop.is_set()
        assert store.state.tasks[0].attempt == selected.attempt
    assert all((directory / name).read_bytes() == value for name, value in frozen.items())


@pytest.mark.parametrize('score_fails', [False, True])
def test_quota_crash_after_seal_resumes_with_new_native_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, score_fails: bool
) -> None:
    """A real crash before outcome must not strand a sealed quota task after host recovery."""

    def crash_before_score(self: executor._Attempt) -> None:
        """Crash after the immutable native seal but before any outcome can be written."""
        raise OSError('Synthetic crash after seal.')

    with monkeypatch.context() as local:
        local.setattr(executor._Attempt, 'score', crash_before_score)
        with pytest.raises(OSError, match='Synthetic crash'):
            stopped_run(tmp_path, local, quota=True)
    directory = tmp_path / 'run'
    seal_path = next(directory.glob('tasks/*/attempts/001/seal.json'))
    frozen = seal_path.read_bytes()
    assert not list(directory.glob('tasks/*/attempts/001/outcome.json'))
    config = load_config(tmp_path / 'config.yaml')

    def fail_score(*args: object, **kwargs: object) -> NoReturn:
        """Fail derivative scoring while the quota still requires fresh native work."""
        raise BenchmarkError('scoring_failed', 'Synthetic recovery scoring failure.')

    if score_fails:
        monkeypatch.setattr(recovery, 'score_saved', fail_score)
    provision_fixture(monkeypatch)
    monkeypatch.setattr(coordinator, 'auth_snapshot', lambda *_args, **_kwargs: None)
    code, _ = coordinator.run(
        config, tmp_path / 'basket.json', resume=directory, attempt_function=fake_attempt(['success'])
    )
    assert code == 0
    store = RunStore.open(directory)
    row = store.snapshot().tasks[0]
    assert row.processing_status == 'completed'
    assert row.final_attempt is not None and row.final_attempt.sequence == 2
    previous = store.state.tasks[0].history[0]
    assert previous.seal is not None and previous.outcome is None
    assert previous.scoring is not None
    assert seal_path.read_bytes() == frozen
    archive = tmp_path / 'recovered.tar.gz'
    create_archive(directory, archive, require_complete=False)
    manifest = verify_archive(archive)
    assert manifest.complete
    assert previous.interruption is not None
    assert previous.outcome is None


def test_quota_resume_after_signal_preserves_saved_score_and_replays_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal after durable host recovery cannot strand the next quota resume."""

    def crash_before_score(self: executor._Attempt) -> None:
        """Leave a real quota seal without an outcome."""
        raise OSError('Synthetic crash after seal.')

    with monkeypatch.context() as local:
        local.setattr(executor._Attempt, 'score', crash_before_score)
        with pytest.raises(OSError, match='Synthetic crash'):
            stopped_run(tmp_path, local, quota=True)
    directory = tmp_path / 'run'
    config = load_config(tmp_path / 'config.yaml')
    provision_fixture(monkeypatch)
    monkeypatch.setattr(coordinator, 'auth_snapshot', lambda *_args, **_kwargs: None)
    original_accept = RunStore.accept_operation

    def interrupt_after_commit(store: RunStore, selection: OperationSelection) -> Report:
        """Deliver SIGINT only after the real scoring operation is durably selected."""
        result = original_accept(store, selection)
        os.kill(os.getpid(), signal.SIGINT)
        return result

    with monkeypatch.context() as local:
        local.setattr(RunStore, 'accept_operation', interrupt_after_commit)
        code, _ = coordinator.run(
            config, tmp_path / 'basket.json', resume=directory, attempt_function=fake_attempt(['success'])
        )
    assert code == 130
    store = RunStore.open(directory)
    selected = store.state.tasks[0]
    assert selected.processing_status == 'interrupted'
    assert store.snapshot().tasks[0].scoring.status == 'available'
    assert selected.seal is not None and selected.scoring is not None and selected.outcome is None
    frozen = {
        ref.path: (directory / ref.path).read_bytes()
        for ref in [selected.seal, selected.scoring.receipt, selected.scoring.result]
    }

    def forbidden_score(*args: object, **kwargs: object) -> NoReturn:
        """Reject recomputation of a score already saved by the first resume."""
        raise AssertionError('Saved host score must be reused.')

    monkeypatch.setattr(recovery, 'score_saved', forbidden_score)
    code, _ = coordinator.run(
        config, tmp_path / 'basket.json', resume=directory, attempt_function=fake_attempt(['success'])
    )
    assert code == 0
    final = RunStore.open(directory)
    assert final.state.tasks[0].attempt is not None and final.state.tasks[0].attempt.sequence == 2
    assert final.state.tasks[0].history[0].scoring == selected.scoring
    assert all((directory / path).read_bytes() == content for path, content in frozen.items())
