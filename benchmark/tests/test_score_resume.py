"""Host recovery selects scoring operations while native UUIDs, timing and outcomes stay fixed."""

from pathlib import Path

import pytest
from benchmark import attempt as executor
from benchmark import coordinator, recovery
from benchmark.artifact_refs import read_reference
from benchmark.commands import Command
from benchmark.errors import BenchmarkError
from benchmark.lifecycle import Stop
from benchmark.run_store import RunStore
from benchmark.storage import read_json, run_lock
from test_operation_workflows import stopped_run


def test_host_recovery_keeps_same_attempt_without_runtime_auth_or_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete one failed host score using only already-selected sealed source."""
    directory, config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    before = RunStore.open(directory).snapshot()['tasks'][0]
    selected = RunStore.open(directory).state['tasks'][0]
    outcome_path = directory / selected['outcome']['path']
    native_bytes = outcome_path.read_bytes()
    seal = read_reference(directory, selected['seal'])

    def forbidden(*args: object, **kwargs: object) -> object:
        """Reject runtime provisioning, OAuth refresh and any model execution."""
        raise AssertionError('Host-only recovery must not invoke native execution or authentication.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    monkeypatch.setattr(executor, '_oauth', forbidden)
    monkeypatch.setattr(executor, 'run_attempt', forbidden)
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        command = Command(directory, 'run', level='ERROR', progress_total=1)
        try:
            report = recovery.recover_host_scores(store, config, command, Stop())
            after = report['tasks'][0]
            assert after['scoring']['status'] == 'available'
            assert after['final_attempt'] == before['final_attempt']
            assert after['execution'] == before['execution'] and after['telemetry'] == before['telemetry']
            operation = store.last_operation('rescore')
            assert operation is not None
            again = recovery.recover_host_scores(store, config, command, Stop())
            assert again == report and store.last_operation('rescore') == operation
        finally:
            command.close()
    assert outcome_path.read_bytes() == native_bytes
    assert read_reference(directory, selected['seal'])['native_timing'] == seal['native_timing']
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1
    assert not list(outcome_path.parent.glob('outcome_before*'))


def test_repeated_host_failure_stays_explicit_without_native_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new host failure keeps native facts intact and never consumes native retry budget."""
    directory, config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    selected = RunStore.open(directory).state['tasks'][0]
    outcome = (directory / selected['outcome']['path']).read_bytes()

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
        current = store.state['tasks'][0]
        assert current['attempt'] == selected['attempt'] and current['retry_index'] == selected['retry_index']
        assert report['tasks'][0]['scoring']['error']['code'] == 'scoring_failed'
        assert report['tasks'][0]['infrastructure_error'] is None
        assert report['tasks'][0]['processing_status'] == 'completed'
    assert (directory / selected['outcome']['path']).read_bytes() == outcome


def test_damaged_sealed_source_is_rejected_before_any_new_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed source corruption blocks recovery rather than selecting a fabricated score."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    store = RunStore.open(directory)
    row = store.snapshot()['tasks'][0]
    manifest = directory / row['artifacts']['triplets_manifest']
    entry = next(item for item in read_json(manifest)['entries'] if item['path'] == 'new/source.py')
    (manifest.parent / entry['files']['ai']['path']).write_text('damaged')
    with pytest.raises(BenchmarkError):
        RunStore.open(directory)
    assert read_json(directory / '_meta/state.json')['tasks'][0]['attempt']['id'] == row['final_attempt']['id']


def test_real_resume_coordinator_uses_host_recovery_without_native_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public resume entrypoint restores a failed score without credentials or runtime."""
    directory, config, dataset = stopped_run(tmp_path, monkeypatch, score_error=True)
    original = RunStore.open(directory).state['tasks'][0]
    frozen = (directory / original['outcome']['path']).read_bytes()

    def forbidden(*args: object, **kwargs: object) -> dict:
        """Reject any attempt to turn host score recovery into an agent invocation."""
        raise AssertionError('Native preflight or execution is forbidden for host-only recovery.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    monkeypatch.setattr(executor, '_oauth', forbidden)
    code, restored = coordinator.run(config, dataset, resume=directory, attempt_function=forbidden, progress='off')
    assert code == 0 and restored == directory
    current = RunStore.open(directory)
    assert current.snapshot()['tasks'][0]['scoring']['status'] == 'available'
    assert current.state['tasks'][0]['attempt'] == original['attempt']
    assert (directory / original['outcome']['path']).read_bytes() == frozen
