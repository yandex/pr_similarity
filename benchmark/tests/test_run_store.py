"""One state commit governs deterministic report snapshots and recovery."""

from pathlib import Path

import pytest
from python_fixtures import freeze_python, write_python_probe
from test_runner import config_and_basket

from benchmark.errors import BenchmarkError
from benchmark.json_types import JsonObject, JsonValue
from benchmark.models import Report, State
from benchmark.models.evidence import (
    NativeTiming,
    Outcome,
    ReservationNotice,
    ScoringEvidence,
    Seal,
    SealEvidence,
)
from benchmark.models.report import ReportAttempt, ReportError, ReportTaskExecution
from benchmark.persistence import run_store
from benchmark.persistence.artifact_refs import ArtifactReference
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import (
    bytes_sha256,
    canonical_bytes,
    read_json,
    write_json,
)
from benchmark.presentation.result import unavailable_scoring
from benchmark.runtime.runner import initial_report
from benchmark.validation.basket import load_basket


def _json_at(value: JsonValue, *path: str | int) -> JsonValue:
    """Locate a fixture field before intentionally corrupting its external JSON."""
    for part in path:
        if isinstance(part, int):
            assert isinstance(value, list)
            value = value[part]
        else:
            assert isinstance(value, dict)
            value = value[part]
    return value


def _object_at(value: JsonValue, *path: str | int) -> JsonObject:
    """Require an object at the raw malformed-artifact mutation boundary."""
    result = _json_at(value, *path)
    assert isinstance(result, dict)
    return result


def _array_at(value: JsonValue, *path: str | int) -> list[JsonValue]:
    """Require an array before mutating a fixture's signed inventory."""
    result = _json_at(value, *path)
    assert isinstance(result, list)
    return result


def fresh_store(tmp_path: Path) -> RunStore:
    """Create a real frozen basket/config registry with no native agent calls."""
    config, dataset = config_and_basket(tmp_path)
    basket = load_basket(dataset)
    directory = tmp_path / 'run'
    directory.mkdir()
    report = initial_report(
        config, basket, '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
    )
    return RunStore.create(directory, report, basket, config)


def test_report_rebuild_is_deterministic_and_has_numbered_navigation(tmp_path: Path) -> None:
    """Losing only a derived report never changes timestamps or requires execution."""
    store = fresh_store(tmp_path)
    source = (store.directory / 'report.json').read_bytes()
    snapshot = store.snapshot()
    row = snapshot.tasks[0]
    assert row.ordinal == 1 and row.directory.startswith('tasks/001-')
    assert snapshot.state.path == '_meta/state.json'
    (store.directory / 'report.json').unlink()
    RunStore.open(store.directory).rebuild()
    assert (store.directory / 'report.json').read_bytes() == source
    assert not (store.directory / 'checkpoint.json').exists()


def test_state_commit_survives_failure_before_derived_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A committed state wins even if the public projection remains on the old revision."""
    store = fresh_store(tmp_path)
    old_report = (store.directory / 'report.json').read_bytes()
    original = run_store.write_json

    def fail_projection(path: Path, value: object, **kwargs: object) -> None:
        """Inject a report-only failure after the authoritative state replace."""
        if path.name == 'report.json':
            raise BenchmarkError('artifact_write_failed', 'Synthetic projection interruption.')
        original(path, value)

    monkeypatch.setattr(run_store, 'write_json', fail_projection)
    snapshot = store.snapshot()
    snapshot.run.status = 'interrupted'
    with pytest.raises(BenchmarkError):
        store.commit(snapshot)
    assert (store.directory / 'report.json').read_bytes() == old_report
    monkeypatch.setattr(run_store, 'write_json', original)
    recovered = RunStore.open(store.directory)
    assert recovered.snapshot().run.status == 'interrupted'
    recovered.rebuild()
    assert Report.model_validate(read_json(store.directory / 'report.json')).run.status == 'interrupted'


def test_corrupt_state_and_old_report_are_not_reinterpreted_as_fresh_runs(tmp_path: Path) -> None:
    """Schema version one alone never licenses rebuilding ownership or issuing work."""
    store = fresh_store(tmp_path)
    state = read_json(store.directory / '_meta/state.json')
    _object_at(state, 'tasks', 0)['id'] = 'foreign'
    write_json(store.directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError):
        RunStore.open(store.directory)
    other = tmp_path / 'old'
    other.mkdir()
    write_json(other / 'report.json', {'schema_version': 1})
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(other)
    assert caught.value.code == 'unsupported_run_format'
    assert sorted(p.name for p in other.iterdir()) == ['report.json']


def test_relocation_preserves_registry_and_projection(tmp_path: Path) -> None:
    """All authoritative references remain relative after moving a complete run."""
    store = fresh_store(tmp_path)
    previous = store.snapshot()
    destination = tmp_path / 'renamed'
    store.directory.rename(destination)
    assert RunStore.open(destination).snapshot() == previous


def test_registry_owns_one_shared_native_input(tmp_path: Path) -> None:
    """Verify registry owns one shared native input."""
    store = fresh_store(tmp_path)
    assert store.snapshot().agent_input == store.registry.agent_input
    assert store.registry.agent_input.config.path == 'agent/opencode.json'
    assert (store.directory / 'agent/opencode.json').is_file()
    assert (store.directory / 'agent/task_instructions.txt').is_file()


def test_missing_shared_input_is_not_a_new_run(tmp_path: Path) -> None:
    """Verify missing shared input is not a new run."""
    store = fresh_store(tmp_path)
    (store.directory / 'agent/opencode.json').unlink()
    with pytest.raises(BenchmarkError):
        RunStore.open(store.directory)
    assert (store.directory / '_meta/state.json').is_file()


def test_prerelease_registry_without_shared_input_is_explicitly_rejected(tmp_path: Path) -> None:
    """Verify prerelease registry without shared input is explicitly rejected."""
    store = fresh_store(tmp_path)
    registry = read_json(store.directory / 'run.json')
    registry.pop('agent_input')
    write_json(store.directory / 'run.json', registry)
    previous = (store.directory / '_meta/state.json').read_bytes()
    with pytest.raises(BenchmarkError) as error:
        RunStore.open(store.directory)
    assert error.value.code == 'unsupported_run_format'
    assert (store.directory / '_meta/state.json').read_bytes() == previous


def test_independent_stale_writer_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    """Two snapshots at one revision cannot both publish their chosen state."""
    first = fresh_store(tmp_path)
    second = RunStore.open(first.directory)
    chosen = first.snapshot()
    chosen.run.status = 'interrupted'
    first.commit(chosen)
    stale = second.snapshot()
    stale.run.status = 'running'
    with pytest.raises(BenchmarkError) as caught:
        second.commit(stale)
    assert caught.value.code == 'stale_run_state'
    assert RunStore.open(first.directory).snapshot().run.status == 'interrupted'


def test_live_artifact_never_follows_an_escaping_symlink(tmp_path: Path) -> None:
    """Future locations do not license presenting symlink contents as evidence."""
    store = fresh_store(tmp_path)
    selected = store.state.tasks[0]
    selected.attempt = ReportAttempt.model_validate(
        {
            'id': '33333333-3333-4333-8333-333333333333',
            'invocation_id': '22222222-2222-4222-8222-222222222222',
            'sequence': 1,
            'retry_index': 0,
            'started_at': '2026-10-08T14:30:00Z',
            'finished_at': None,
        }
    )
    selected.next_sequence = 2
    directory = store.directory / store.attempt_directory(selected.id, 1)
    directory.mkdir(parents=True)
    outside = tmp_path / 'secret'
    outside.write_text('private')
    (directory / 'agent.log').symlink_to(outside)
    with pytest.raises(BenchmarkError):
        store.snapshot()


def test_reservation_persists_owner_and_locations_before_executor(tmp_path: Path) -> None:
    """The directory and start fact exist before a native child can be issued."""
    store = fresh_store(tmp_path)
    attempt = ReportAttempt.model_validate(
        {
            'id': '33333333-3333-4333-8333-333333333333',
            'invocation_id': '22222222-2222-4222-8222-222222222222',
            'sequence': 1,
            'retry_index': 0,
            'started_at': '2026-10-08T14:30:00Z',
            'finished_at': None,
        }
    )
    task_id = store.registry.tasks[0].id
    report = store.reserve(task_id, attempt)
    row = report.tasks[0]
    assert row.final_attempt is not None
    assert row.ordinal == 1 and row.final_attempt.id == attempt.id
    assert row.locations['agent_log'].endswith('/attempts/001/agent.log')
    start = store.directory / store.attempt_directory(task_id, 1) / 'start.json'
    assert read_json(start)['attempt'] == attempt.to_wire()
    assert RunStore.open(store.directory).state.tasks[0].next_sequence == 2
    with pytest.raises(BenchmarkError) as caught:
        store.reserve(task_id, attempt)
    assert caught.value.code == 'attempt_not_reservable'


def test_uncommitted_operation_is_ignored_and_foreign_selection_is_rejected(tmp_path: Path) -> None:
    """A result directory alone never replaces selected scores, even with newer timestamps."""
    store = fresh_store(tmp_path)
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    baseline = (store.directory / 'report.json').read_bytes()
    selection = store.prepare_operation(
        'rescore', [], invocation_id='22222222-2222-4222-8222-222222222222', metric=report.metric
    )
    assert RunStore.open(store.directory).snapshot().last_rescore is None
    assert (store.directory / 'report.json').read_bytes() == baseline
    rejected = selection.model_copy(deep=True)
    rejected.id = '33333333-3333-4333-8333-333333333333'
    with pytest.raises(BenchmarkError) as caught:
        store.accept_operation(rejected)
    assert caught.value.code == 'operation_owner_mismatch'
    projected = store.accept_operation(selection)
    assert projected.last_rescore is not None
    assert projected.last_rescore.receipt_path == selection.receipt.path
    assert RunStore.open(store.directory).snapshot() == projected


def test_offline_result_from_an_older_state_cannot_be_committed(tmp_path: Path) -> None:
    """A successful calculation does not permit overwriting intervening accepted selections."""
    store = fresh_store(tmp_path)
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    operation = store.prepare_operation(
        'rescore', [], invocation_id='22222222-2222-4222-8222-222222222222', metric=report.metric
    )
    store.commit(store.snapshot())
    with pytest.raises(BenchmarkError) as caught:
        store.accept_operation(operation)
    assert caught.value.code == 'operation_inputs_stale'
    assert store.snapshot().last_rescore is None


def native_attempt(sequence: int = 1) -> ReportAttempt:
    """Describe one reserved native child without executing a process."""
    return ReportAttempt(
        id=f'33333333-3333-4333-8333-{sequence:012d}',
        invocation_id='22222222-2222-4222-8222-222222222222',
        sequence=sequence,
        retry_index=sequence - 1,
        started_at='2026-10-08T14:30:00Z',
        finished_at=None,
    )


def test_failed_reservation_commit_preserves_memory_and_durable_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed authoritative replace never leaves the caller holding an uncommitted selection."""
    store = fresh_store(tmp_path)
    previous = store.snapshot()
    original = run_store.write_json

    def fail_state(path: Path, value: object, **kwargs: object) -> None:
        """Fail only the authoritative selection, after immutable start is written."""
        if path.name == 'state.json':
            raise BenchmarkError('artifact_write_failed', 'Synthetic state failure.')
        original(path, value)

    monkeypatch.setattr(run_store, 'write_json', fail_state)
    with pytest.raises(BenchmarkError):
        store.reserve(store.registry.tasks[0].id, native_attempt())
    assert store.snapshot() == previous
    assert RunStore.open(store.directory).snapshot() == previous
    start = store.directory / store.attempt_directory(previous.tasks[0].id, 1) / 'start.json'
    assert start.is_file()


def test_failed_phase_commit_preserves_previous_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A status observation cannot become lifecycle authority when persistence failed."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    previous = store.reserve(task_id, native_attempt())
    original = run_store.write_json

    def fail_state(path: Path, value: object, **kwargs: object) -> None:
        """Reject the observed phase at the state commit point."""
        if path.name == 'state.json':
            raise BenchmarkError('artifact_write_failed', 'Synthetic state failure.')
        original(path, value)

    monkeypatch.setattr(run_store, 'write_json', fail_state)
    with pytest.raises(BenchmarkError):
        store.observe(task_id, 'running', '2026-10-08T14:31:00Z')
    assert store.snapshot() == previous
    assert RunStore.open(store.directory).snapshot() == previous


def test_all_history_is_validated_and_sequence_gaps_are_rejected(tmp_path: Path) -> None:
    """Earlier native starts remain owned evidence when a newer attempt is selected."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    interrupted = store.reserve(task_id, native_attempt())
    interrupted.tasks[0].processing_status = 'interrupted'
    store.commit(interrupted)
    store.reserve(task_id, native_attempt(2))
    state = read_json(store.directory / '_meta/state.json')
    selected = State.model_validate(state).tasks[0]
    assert len(selected.history) == 1
    earlier = selected.history[0]
    assert earlier.start is not None
    start = store.directory / earlier.start.path
    start.write_bytes(start.read_bytes() + b'\n')
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(store.directory)
    assert caught.value.code == 'artifact_digest_mismatch'


def test_selected_receipts_without_native_owner_are_rejected(tmp_path: Path) -> None:
    """A nullable current attempt never allows orphan evidence refs to be ignored."""
    store = fresh_store(tmp_path)
    state = read_json(store.directory / '_meta/state.json')
    _object_at(state, 'tasks', 0)['start'] = {'path': 'missing.json', 'size_bytes': 0, 'sha256': '0' * 64}
    write_json(store.directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(store.directory)
    assert caught.value.code == 'attempt_owner_mismatch'


def test_forged_configured_model_is_rejected_even_when_registry_digest_matches(tmp_path: Path) -> None:
    """Rehashing a changed registry cannot turn invented model labels into configured facts."""
    store = fresh_store(tmp_path)
    registry = read_json(store.directory / 'run.json')
    fact = _object_at(registry, 'baseline', 'inference', 'configured', 'model')
    fact.update(status='known', value='openai/forged-model', reason=None)
    write_json(store.directory / 'run.json', registry)
    state = read_json(store.directory / '_meta/state.json')
    state['registry_sha256'] = run_store.reference(store.directory, 'run.json').sha256
    write_json(store.directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(store.directory)
    assert caught.value.code == 'inference_owner_mismatch'


def closed_fact(store: RunStore, kind: str, *, seal: ArtifactReference | None = None) -> ArtifactReference:
    """Create complete producer evidence through the registry-owned numbered path."""
    if store.state.python_runtime is None:
        snapshot = store.snapshot()
        snapshot.agent.python_runtime = freeze_python(store.directory)
        store.commit(snapshot)
    row = store.snapshot().tasks[0]
    assert row.final_attempt is not None
    assert store.state.python_runtime is not None
    python_reference = write_python_probe(
        store.directory,
        store.attempt_directory(row.id, row.final_attempt.sequence),
        store.state.python_runtime,
    )
    row.final_attempt.finished_at = '2026-10-08T14:31:00Z'
    row.execution = ReportTaskExecution(status='completed', exit_code=0, duration_seconds=1)
    if kind == 'outcome':
        assert seal is not None
        error = ReportError(code='fixture_unscored', message='Controlled score is unavailable.')
        row.scoring = unavailable_scoring('unavailable', error)
        score_path = store.attempt_directory(row.id, row.final_attempt.sequence) / 'scoring.json'
        score = ScoringEvidence(
            schema_family='pr_similarity_os.scoring',
            schema_version=2,
            run_id=store.registry.id,
            task_id=row.id,
            attempt_id=row.final_attempt.id,
            seal=seal,
            metric=store.state.metric,
            scoring=row.scoring,
            error=error,
            diagnostics=[],
        )
        write_json(store.directory / score_path, score)
        scoring = run_store.reference(store.directory, score_path.as_posix())
        value: Seal | Outcome = Outcome(
            schema_family='pr_similarity_os.outcome',
            schema_version=2,
            run_id=store.registry.id,
            basket_sha256=store.registry.baseline.basket.sha256,
            config_identity_sha256=store.registry.baseline.config.identity_sha256,
            agent_input=store.registry.agent_input,
            task=row,
            metric=store.state.metric,
            runtime_binary_sha256='a' * 64,
            python_runtime=store.state.python_runtime,
            seal=seal,
            scoring_result=scoring,
            scoring_error=error,
            fatal=None,
            archive_complete=False,
            artifact_receipts=[seal, scoring, store.state.python_runtime, python_reference],
        )
    else:
        value = Seal(
            schema_family='pr_similarity_os.seal',
            schema_version=2,
            run_id=store.registry.id,
            basket_sha256=store.registry.baseline.basket.sha256,
            config_identity_sha256=store.registry.baseline.config.identity_sha256,
            agent_input=store.registry.agent_input,
            task=row,
            metric=store.state.metric,
            runtime_binary_sha256='a' * 64,
            python_runtime=store.state.python_runtime,
            archive_complete=False,
            artifact_receipts=[store.state.python_runtime, python_reference],
            native_timing=NativeTiming(started_at=None, finished_at=None, duration_seconds=None),
            evidence=SealEvidence(),
        )
    relative = (store.attempt_directory(row.id, row.final_attempt.sequence) / (kind + '.json')).as_posix()
    write_json(store.directory / relative, value)
    return run_store.reference(store.directory, relative)


def test_only_explicit_native_fact_selection_changes_projection(tmp_path: Path) -> None:
    """An existing seal remains diagnostic until its exact receipt commits in state."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    reserved = store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    assert store.commit(store.snapshot()).tasks[0].execution == reserved.tasks[0].execution
    assert RunStore.open(store.directory).state.tasks[0].seal is None
    projected = store.accept_fact(task_id, 'seal', seal)
    assert projected.tasks[0].execution.status == 'completed'
    assert RunStore.open(store.directory).state.tasks[0].seal == seal
    outcome = closed_fact(store, 'outcome', seal=seal)
    completed = store.accept_fact(task_id, 'outcome', outcome, processing_status='completed')
    assert completed.tasks[0].processing_status == 'completed'
    assert RunStore.open(store.directory).snapshot() == completed


def test_final_outcome_cannot_reference_foreign_native_seal(tmp_path: Path) -> None:
    """Immutable outcome and evidence must describe the same closed native attempt."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    store.accept_fact(task_id, 'seal', seal)
    foreign = seal.model_copy(deep=True)
    foreign.sha256 = '0' * 64
    outcome = closed_fact(store, 'outcome', seal=foreign)
    with pytest.raises(BenchmarkError) as caught:
        store.accept_fact(task_id, 'outcome', outcome)
    assert caught.value.code == 'attempt_seal_mismatch'
    assert RunStore.open(store.directory).state.tasks[0].outcome is None


@pytest.mark.parametrize('identifier', ['../escape', '/tmp/escape', '', '33333333-3333-4333-8333-33333333333A'])
def test_unsafe_operation_identifier_is_rejected_before_any_artifact(tmp_path: Path, identifier: str) -> None:
    """Operation IDs cannot create a directory before their canonical UUID ownership is validated."""
    store = fresh_store(tmp_path)
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    previous = sorted(path.relative_to(store.directory).as_posix() for path in store.directory.rglob('*'))
    with pytest.raises(BenchmarkError) as caught:
        store.prepare_operation('rescore', [], invocation_id=report.run.current_invocation_id, operation_id=identifier)
    assert caught.value.code == 'invalid_operation_reference'
    assert sorted(path.relative_to(store.directory).as_posix() for path in store.directory.rglob('*')) == previous


def test_operation_never_traverses_symlink_to_external_directory(tmp_path: Path) -> None:
    """A UUID-shaped operation cannot write through a pre-existing parent symlink."""
    store = fresh_store(tmp_path)
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (store.directory / 'operations').symlink_to(outside, target_is_directory=True)
    with pytest.raises(BenchmarkError) as caught:
        store.prepare_operation('rescore', [], invocation_id=report.run.current_invocation_id)
    assert caught.value.code == 'artifact_unsafe'
    assert list(outside.iterdir()) == []


def test_mixed_initialization_is_not_overwritten_or_reinterpreted(tmp_path: Path) -> None:
    """A missing authoritative state requires preservation, rather than fresh-run reconstruction."""
    store = fresh_store(tmp_path)
    (store.directory / '_meta/state.json').unlink()
    previous = (store.directory / 'run.json').read_bytes()
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(store.directory)
    assert caught.value.code == 'incomplete_run_state'
    assert (store.directory / 'run.json').read_bytes() == previous
    assert not (store.directory / '_meta/state.json').exists()


def test_failed_reservation_can_resume_without_reusing_or_replaying_native_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery consumes the immutable orphan sequence before issuing any fresh attempt."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    first = native_attempt()
    original = run_store.write_json

    def fail_state(path: Path, value: object, **kwargs: object) -> None:
        """Crash after complete start publication and before queue selection."""
        if path.name == 'state.json':
            raise BenchmarkError('artifact_write_failed', 'Synthetic state failure.')
        original(path, value)

    monkeypatch.setattr(run_store, 'write_json', fail_state)
    with pytest.raises(BenchmarkError):
        store.reserve(task_id, first)
    monkeypatch.setattr(run_store, 'write_json', original)
    restored = RunStore.open(store.directory)
    assert restored.recover_reservations() == [ReservationNotice(task_id=task_id, attempt_id=first.id, sequence=1)]
    assert restored.state.tasks[0].processing_status == 'interrupted'
    assert restored.recover_reservations() == []
    second = native_attempt(2)
    restored.reserve(task_id, second)
    selected = RunStore.open(store.directory).state.tasks[0]
    assert selected.attempt == second and selected.history[0].attempt == first
    assert selected.next_sequence == 3


def test_completed_outcome_cannot_be_reserved_or_reopened(tmp_path: Path) -> None:
    """Terminal native work stays closed even when repeated resume APIs are called."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    store.accept_fact(task_id, 'seal', seal)
    outcome = closed_fact(store, 'outcome', seal=seal)
    store.observe(task_id, 'scoring', '2026-10-08T10:00:00Z')
    store.accept_fact(task_id, 'outcome', outcome, processing_status='completed')
    progress = store.snapshot().tasks[0].progress
    assert progress.phase == 'completed' and progress.updated_at == store.state.updated_at
    store.commit(store.snapshot())
    assert store.snapshot().tasks[0].progress == progress
    state_bytes = (store.directory / '_meta/state.json').read_bytes()
    outcome_bytes = (store.directory / outcome.path).read_bytes()
    with pytest.raises(BenchmarkError) as caught:
        store.reserve(task_id, native_attempt(2))
    assert caught.value.code == 'attempt_not_reservable'
    with pytest.raises(BenchmarkError) as caught:
        store.accept_fact(task_id, 'outcome', outcome, processing_status='interrupted')
    assert caught.value.code == 'terminal_task_closed'
    assert (store.directory / '_meta/state.json').read_bytes() == state_bytes
    assert (store.directory / outcome.path).read_bytes() == outcome_bytes
    assert not (store.directory / store.attempt_directory(task_id, 2)).exists()


def test_invalid_huge_sequence_is_rejected_without_allocating_missing_history(tmp_path: Path) -> None:
    """A corrupt counter cannot allocate a list proportional to its claimed sequence."""
    store = fresh_store(tmp_path)
    state = read_json(store.directory / '_meta/state.json')
    _object_at(state, 'tasks', 0)['next_sequence'] = 10**18
    write_json(store.directory / '_meta/state.json', state)
    with pytest.raises(BenchmarkError) as caught:
        RunStore.open(store.directory)
    assert caught.value.code == 'invalid_state'


def test_resume_budget_reset_preserves_native_owner_and_sequence(tmp_path: Path) -> None:
    """A new command epoch never rewrites immutable start ownership or spent native time."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    first = native_attempt()
    interrupted = store.reserve(task_id, first)
    interrupted.tasks[0].processing_status = 'interrupted'
    store.commit(interrupted)
    start = store.state.tasks[0].start
    assert start is not None
    first_start = store.directory / start.path
    bytes_before = first_start.read_bytes()
    store.prepare_invocation('44444444-4444-4444-8444-444444444444', resumed=True)
    selection = store.state.tasks[0]
    assert selection.retry_index == 0 and selection.retry_epoch == 1
    assert selection.attempt == first and selection.next_sequence == 2
    assert first_start.read_bytes() == bytes_before
    assert store.state.run.current_invocation_id == '44444444-4444-4444-8444-444444444444'


def test_state_refs_hash_actual_frozen_bytes_and_normalize_only_new_documents(tmp_path: Path) -> None:
    """Semantic-equivalent formatting remains distinct from an exact artifact byte receipt."""
    store = fresh_store(tmp_path)
    report = store.snapshot()
    report.run.status = 'interrupted'
    store.commit(report)
    path = store.directory / '_meta/state.json'
    original = canonical_bytes(read_json(path)) + b'\n'
    path.write_bytes(original)
    reopened = RunStore.open(store.directory)
    assert reopened.snapshot().state.sha256 == bytes_sha256(original)
    operation = reopened.prepare_operation(
        'rescore', [], invocation_id=report.run.current_invocation_id, metric=report.metric
    )
    frozen = store.directory / 'operations/rescore' / operation.id / 'input_state.json'
    assert frozen.read_bytes() == original
    accepted = reopened.accept_operation(operation)
    assert accepted.state.sha256 == bytes_sha256(path.read_bytes())
    assert path.read_bytes().startswith(b'{\n  ')
    assert RunStore.open(store.directory).snapshot() == accepted


def test_outcome_cannot_rewrite_native_duration_after_host_scoring(tmp_path: Path) -> None:
    """A host result never replaces measured execution timing inside an immutable native outcome."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    store.accept_fact(task_id, 'seal', seal)
    outcome = closed_fact(store, 'outcome', seal=seal)
    value = read_json(store.directory / outcome.path)
    _object_at(value, 'task', 'execution')['duration_seconds'] = 900
    write_json(store.directory / outcome.path, value)
    changed = run_store.reference(store.directory, outcome.path)
    with pytest.raises(BenchmarkError) as caught:
        store.accept_fact(task_id, 'outcome', changed, processing_status='completed')
    assert caught.value.code == 'native_fact_mismatch'
    assert store.state.tasks[0].outcome is None


def test_report_edit_cannot_reopen_a_completed_native_task(tmp_path: Path) -> None:
    """Editing a derived status cannot bypass terminal reservation protection."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    store.accept_fact(task_id, 'seal', seal)
    outcome = closed_fact(store, 'outcome', seal=seal)
    before = store.accept_fact(task_id, 'outcome', outcome, processing_status='completed')
    before.tasks[0].processing_status = 'retry_pending'
    with pytest.raises(BenchmarkError) as caught:
        store.commit(before)
    assert caught.value.code == 'terminal_task_closed'
    assert RunStore.open(store.directory).state.tasks[0].processing_status == 'completed'


@pytest.mark.parametrize('fault', ['missing', 'owner', 'seal', 'metric', 'inline', 'error', 'inventory', 'pending'])
def test_outcome_requires_its_verified_initial_host_score(tmp_path: Path, fault: str) -> None:
    """Freshly rehashed outcomes cannot replace the provenance of an initial host score."""
    store = fresh_store(tmp_path)
    task_id = store.registry.tasks[0].id
    store.reserve(task_id, native_attempt())
    seal = closed_fact(store, 'seal')
    store.accept_fact(task_id, 'seal', seal)
    receipt = closed_fact(store, 'outcome', seal=seal)
    outcome = read_json(store.directory / receipt.path)
    score_ref = ArtifactReference.model_validate(outcome['scoring_result'])
    score = read_json(store.directory / score_ref.path)
    if fault == 'missing':
        del outcome['scoring_result']
    elif fault == 'inventory':
        _array_at(outcome, 'artifact_receipts').remove(score_ref.to_wire())
    elif fault == 'inline':
        _object_at(outcome, 'task', 'scoring', 'error')['message'] = 'Forged inline score.'
    elif fault == 'error':
        outcome['scoring_error'] = None
    else:
        if fault == 'owner':
            score['attempt_id'] = '33333333-3333-4333-8333-333333333333'
        elif fault == 'seal':
            score['seal'] = None
        elif fault == 'metric':
            score['metric'] = {}
        elif fault == 'pending':
            _object_at(score, 'scoring')['status'] = 'pending'
            _object_at(outcome, 'task')['scoring'] = score['scoring']
        write_json(store.directory / score_ref.path, score)
        replacement = run_store.reference(store.directory, score_ref.path)
        outcome['scoring_result'] = replacement.to_wire()
        outcome['artifact_receipts'] = [seal.to_wire(), replacement.to_wire()]
    write_json(store.directory / receipt.path, outcome)
    replacement = run_store.reference(store.directory, receipt.path)
    state_bytes = (store.directory / '_meta/state.json').read_bytes()
    with pytest.raises(BenchmarkError) as caught:
        store.accept_fact(task_id, 'outcome', replacement, processing_status='completed')
    assert caught.value.code.startswith('scoring_')
    assert (store.directory / '_meta/state.json').read_bytes() == state_bytes
    assert RunStore.open(store.directory).state.tasks[0].outcome is None
