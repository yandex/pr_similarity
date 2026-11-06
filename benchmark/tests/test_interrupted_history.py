"""Interrupted native owners survive recovery without invented execution facts."""

import multiprocessing
import os
import signal
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest
from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonValue
from benchmark.models.report import ReportAttempt
from benchmark.persistence import run_store
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json, write_json
from benchmark.publication.archive import build_manifest
from benchmark.runtime.recovery import recover_native_facts
from test_run_store import closed_fact, fresh_store, native_attempt


def _reserve_and_wait(directory: str, ready: Event) -> None:
    """Reserve a native owner in a child process, then await termination."""
    store = RunStore.open(Path(directory))
    store.reserve(
        store.state.tasks[0].id,
        ReportAttempt(
            id='33333333-3333-4333-8333-333333333333',
            invocation_id='22222222-2222-4222-8222-222222222222',
            sequence=1,
            retry_index=0,
            started_at='2026-11-05T10:00:00Z',
            finished_at=None,
        ),
    )
    ready.set()
    signal.pause()


def test_sigkill_reservation_recovers_explicit_interruption(tmp_path: Path) -> None:
    """Recover a killed reservation without inventing a native finish time."""
    store = fresh_store(tmp_path)
    context = multiprocessing.get_context('fork')
    ready = context.Event()
    child = context.Process(target=_reserve_and_wait, args=(str(store.directory), ready))
    child.start()
    try:
        assert ready.wait(15)
        assert child.pid is not None
        os.kill(child.pid, signal.SIGKILL)
        child.join(15)
        assert child.exitcode == -signal.SIGKILL
    finally:
        if child.is_alive():
            child.kill()
            child.join()
    store = RunStore.open(store.directory)
    start = store.state.tasks[0].start
    assert start is not None
    original = (store.directory / start.path).read_bytes()
    recover_native_facts(store, load_config(tmp_path / 'config.yaml'))
    selected = store.state.tasks[0]
    assert selected.interruption is not None
    assert selected.processing_status == 'interrupted'
    assert selected.seal is None and selected.outcome is None
    assert selected.attempt is not None and selected.start is not None
    assert selected.attempt.finished_at is None
    assert (store.directory / selected.start.path).read_bytes() == original
    before = (store.directory / '_meta/state.json').read_bytes()
    recover_native_facts(store, load_config(tmp_path / 'config.yaml'))
    assert (store.directory / '_meta/state.json').read_bytes() == before
    assert store.snapshot().costs.all_attempts.native_reported.amount is None


def _unfinished(tmp_path: Path) -> RunStore:
    """Create a reserved attempt without a native terminal fact."""
    store = fresh_store(tmp_path)
    store.reserve(
        store.state.tasks[0].id,
        ReportAttempt(
            id='33333333-3333-4333-8333-333333333333',
            invocation_id='22222222-2222-4222-8222-222222222222',
            sequence=1,
            retry_index=0,
            started_at='2026-11-05T10:00:00Z',
            finished_at=None,
        ),
    )
    return store


def test_recovery_fact_survives_crash_before_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse the exact interruption fact when the state write is interrupted."""
    store = _unfinished(tmp_path)
    original = run_store.write_json

    def fail_state(path: Path, value: object, **kwargs: object) -> None:
        """Fail only the state replacement after the interruption fact is saved."""
        if path.name == 'state.json':
            raise BenchmarkError('test_crash', 'Interrupted before state replace.')
        original(path, value)

    monkeypatch.setattr(run_store, 'write_json', fail_state)
    with pytest.raises(BenchmarkError, match='Interrupted before'):
        store.recover_interruptions()
    marker = next(store.directory.glob('tasks/*/attempts/001/interruption.json'))
    before = marker.read_bytes()
    monkeypatch.setattr(run_store, 'write_json', original)
    RunStore.open(store.directory).recover_interruptions()
    assert marker.read_bytes() == before
    assert RunStore.open(store.directory).state.schema_version == 3


@pytest.mark.parametrize(
    ('field', 'replacement'),
    [
        ('run_id', 'foreign'),
        ('task_id', 'foreign'),
        ('attempt_id', 'foreign'),
        ('missing', []),
        ('observed_at', 'foreign'),
        ('schema_version', True),
        ('schema_version', 1.0),
        ('schema_version', 2),
    ],
)
def test_foreign_or_invalid_interruption_is_rejected(tmp_path: Path, field: str, replacement: JsonValue) -> None:
    """Reject a foreign or malformed interruption even with an updated digest."""
    store = _unfinished(tmp_path)
    store.recover_interruptions()
    selected = store.state.tasks[0]
    receipt = selected.interruption
    assert receipt is not None
    path = store.directory / receipt.path
    value = read_json(path)
    value[field] = replacement
    write_json(path, value)
    state = read_json(store.directory / '_meta/state.json')
    tasks = state['tasks']
    assert isinstance(tasks, list) and isinstance(tasks[0], dict)
    tasks[0]['interruption'] = reference(store.directory, receipt.path).to_wire()
    write_json(store.directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError):
        RunStore.open(store.directory)


def test_interruption_does_not_hide_a_native_file(tmp_path: Path) -> None:
    """Reject native evidence that contradicts a recorded missing artifact."""
    store = _unfinished(tmp_path)
    store.recover_interruptions()
    path = store.directory / store.attempt_directory(store.state.tasks[0].id, 1) / 'seal.json'
    path.write_text('{}')
    with pytest.raises(BenchmarkError, match='marked missing'):
        RunStore.open(store.directory)


def test_missing_committed_start_cannot_be_repaired(tmp_path: Path) -> None:
    """Refuse to fabricate evidence for a missing committed start."""
    store = _unfinished(tmp_path)
    start = store.state.tasks[0].start
    assert start is not None
    (store.directory / start.path).unlink()
    with pytest.raises(BenchmarkError):
        RunStore.open(store.directory)
    assert not list(store.directory.glob('tasks/*/attempts/*/interruption.json'))


def test_current_interruption_cannot_make_archive_complete(tmp_path: Path) -> None:
    """Keep an interrupted current owner visibly incomplete in the archive."""
    store = _unfinished(tmp_path)
    store.recover_interruptions()
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    manifest = build_manifest(store.directory)
    assert not manifest.complete
    assert any(item.path is not None and item.path.endswith('/seal.json') for item in manifest.missing_artifacts)


def test_repeated_unsealed_attempts_keep_every_original_owner(tmp_path: Path) -> None:
    """Several hard stops retain all sequences and unknown costs independently."""
    store = _unfinished(tmp_path)
    frozen = {}
    for sequence in (1, 2, 3):
        store.recover_interruptions()
        selected = store.state.tasks[0]
        assert selected.interruption is not None
        frozen[selected.interruption.path] = (store.directory / selected.interruption.path).read_bytes()
        if sequence < 3:
            store.reserve(
                selected.id,
                ReportAttempt(
                    id=f'{sequence + 3:08d}-3333-4333-8333-333333333333',
                    invocation_id='22222222-2222-4222-8222-222222222222',
                    sequence=sequence + 1,
                    retry_index=0,
                    started_at='2026-11-05T10:00:00Z',
                    finished_at=None,
                ),
            )
    reopened = RunStore.open(store.directory)
    assert [item.attempt.sequence for item in reopened.state.tasks[0].history] == [1, 2]
    attempt = reopened.state.tasks[0].attempt
    assert attempt is not None and attempt.sequence == 3
    assert reopened.snapshot().costs.all_attempts.native_reported.coverage.total_attempts == 3
    assert reopened.snapshot().costs.all_attempts.native_reported.amount is None
    assert all((store.directory / name).read_bytes() == content for name, content in frozen.items())


def test_old_history_adopts_existing_native_facts_before_marking_missing(tmp_path: Path) -> None:
    """Valid files omitted from old state remain native evidence, not interrupted placeholders."""
    store = fresh_store(tmp_path)
    task_id = store.state.tasks[0].id
    store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    outcome = closed_fact(store, 'outcome', seal=seal)
    frozen = {item.path: (store.directory / item.path).read_bytes() for item in (seal, outcome)}
    # Construct the old state shape without changing any retained native bytes.
    for name in frozen:
        (store.directory / name).unlink()
    report = store.snapshot()
    report.tasks[0].processing_status = 'interrupted'
    store.commit(report)
    store.reserve(task_id, native_attempt(2))
    raw = read_json(store.directory / '_meta/state.json')
    tasks = raw['tasks']
    assert isinstance(tasks, list) and isinstance(tasks[0], dict)
    history = tasks[0]['history']
    assert isinstance(history, list) and isinstance(history[0], dict)
    marker = history[0].pop('interruption')
    assert isinstance(marker, dict) and isinstance(marker['path'], str)
    (store.directory / marker['path']).unlink()
    tasks[0].pop('interruption', None)
    raw['schema_version'] = 2
    write_json(store.directory / '_meta/state.json', raw)
    for name, content in frozen.items():
        (store.directory / name).write_bytes(content)
    recovered = RunStore.open(store.directory)
    recover_native_facts(recovered, load_config(tmp_path / 'config.yaml'))
    old = recovered.state.tasks[0].history[0]
    assert old.seal == seal and old.outcome == outcome
    assert old.interruption is None
    assert old.attempt.id == native_attempt().id
    assert all((store.directory / name).read_bytes() == content for name, content in frozen.items())
