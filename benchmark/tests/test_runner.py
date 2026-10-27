"""Scheduler state transitions are checked independently of paid model behaviour."""

import json
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Literal, NoReturn

import pytest
import yaml
from python_fixtures import (
    freeze_python,
    python_probe,
    python_receipt,
    write_python_probe,
)
from test_basket import payload
from test_telemetry import native_db

from benchmark.config import RunConfig, load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.opencode import EventTracker, Invocation
from benchmark.execution.process import ProcessResult, run_process
from benchmark.execution.provisioning import Runtime
from benchmark.execution.python_runtime import PythonRuntime
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.telemetry import capture_telemetry
from benchmark.metrics import scoring
from benchmark.models import Report, Run, State
from benchmark.models.basket import BasketRecord, BasketV3, Generation
from benchmark.models.evidence import (
    EvidenceStatus,
    NativeIdentity,
    NativeTiming,
    Outcome,
    ScoringEvidence,
    Seal,
    SealEvidence,
    Start,
)
from benchmark.models.report import (
    ReportAgentResolvedRuntime,
    ReportAttempt,
    ReportCounts,
    ReportError,
    ReportInfra,
    ReportTaskChanges,
    ReportTaskExecution,
    ReportTaskScoring,
    TaskDraft,
)
from benchmark.models.runtime import ProvisioningReceipt, SandboxReceipt
from benchmark.models.sources import task_identity
from benchmark.models.telemetry import TelemetryReceipt
from benchmark.models.triplets import TripletIndex, TripletSource
from benchmark.persistence.artifact_refs import ArtifactReference, reference
from benchmark.persistence.layout import TaskRegistration, attempt_path
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import (
    file_lock,
    read_json,
    run_lock,
    sha256,
    utc_now,
    write_json,
)
from benchmark.presentation.report import pending_task, unavailable_scoring
from benchmark.publication.archive import create_archive, verify_archive
from benchmark.runtime import attempt as attempt_module
from benchmark.runtime import coordinator, runner
from benchmark.runtime.attempt import AttemptExecutor
from benchmark.runtime.commands import Command
from benchmark.runtime.runner import Stop, run
from benchmark.vcs.git import GitCache
from benchmark.vcs.workspace import CapturedChanges, Workspace


def config_and_basket(
    tmp_path: Path, *, retries: int = 0, categories: list[str] | None = None
) -> tuple[RunConfig, Path]:
    """Write one single-agent configuration and a complete frozen task basket."""
    data = {
        'schema_version': 1,
        'workers': 2,
        'agent': {
            'name': 'fixture',
            'runtime': 'opencode:v1.18.33',
            'config': {'opencode_json': {}},
            'retries': {'max_retries': retries, 'categories': categories or []},
        },
    }
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump(data))
    basket = tmp_path / 'basket.json'
    write_json(basket, payload())
    return load_config(cfg), basket


def provision_fixture(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Substitute a verified runtime receipt for offline scheduler tests."""
    runtime = Runtime(
        Path('/usr/bin/true'),
        ProvisioningReceipt(
            selector='opencode:v1.18.33',
            actual_version='1.18.33',
            asset_name='opencode-linux-x64-baseline.tar.gz',
            platform='linux-x64-glibc-baseline',
            archive_sha256='a' * 64,
            binary_sha256='b' * 64,
        ),
    )
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        coordinator, 'prepare_python', lambda *_args, **_kwargs: PythonRuntime(Path('/usr'), python_receipt())
    )
    monkeypatch.setattr(Sandbox, 'prepare_python', lambda *_args, **_kwargs: python_probe())
    monkeypatch.setattr(Sandbox, 'check_python', lambda *_args, **_kwargs: python_probe())
    return coordinator


def _fixture_native_row(
    basket_row: BasketRecord,
    metadata: ReportAttempt,
    kind: str,
    stop: Stop,
    relative: Path,
    directory: Path,
    identity: NativeIdentity,
) -> TaskDraft:
    """Close controlled native facts and source receipts before any host score fixture."""
    row = pending_task(basket_row)
    row.processing_status = 'completed'
    row.final_attempt = metadata.model_copy(deep=True)
    row.final_attempt.finished_at = utc_now()
    row.artifacts.outcome = (relative / 'outcome.json').as_posix()
    row.artifacts.triplets_manifest = (relative / 'triplets/index.json').as_posix()
    row.setup.status = 'skipped'
    row.execution = ReportTaskExecution(
        status='timeout' if kind == 'timeout' else 'error' if kind in ('agent_error', 'auth') else 'completed',
        exit_code=0,
        duration_seconds=1,
    )
    (directory / 'agent.diff').write_text('')
    row.changes = ReportTaskChanges(status='empty', files_count=0, diff=(relative / 'agent.diff').as_posix())
    write_json(
        directory / 'triplets/index.json',
        TripletIndex(
            version=1,
            run_id=identity.run_id,
            task_id=row.id,
            attempt_id=metadata.id,
            source=TripletSource(before=row.before, after=row.after),
            entries=[],
        ),
    )
    if kind in ('infra', 'auth'):
        row.infrastructure_error = ReportInfra(
            stage='agent' if kind == 'auth' else 'prepare',
            code='auth_required' if kind == 'auth' else 'fixture',
            message='Synthetic native failure',
            paths=[],
        )
        row.scoring = unavailable_scoring('unavailable', row.infrastructure_error)
        row.artifacts.triplets_manifest = None
    if kind == 'interrupt':
        row.processing_status = 'interrupted'
        row.execution.status, row.execution.exit_code = 'cancelled', None
        stop.signal(signal.SIGINT, None)
    if kind == 'auth':
        stop.set()
    return row


def _fixture_score(row: TaskDraft, scoring_error: ReportError | None) -> None:
    """Keep host score availability separate from native infrastructure errors."""
    if row.infrastructure_error is not None or scoring_error is not None:
        row.scoring = unavailable_scoring('unavailable', row.infrastructure_error or scoring_error)
    else:
        row.scoring = ReportTaskScoring(
            status='available',
            error=None,
            Mf_mlp=1,
            LCf_mlp=1,
            files=[],
            counts=ReportCounts(
                human_files=0,
                agent_files=0,
                matched_files=0,
                missed_files=0,
                extra_files=0,
                human_loc=0,
                agent_loc=0,
                loc_weight=0,
            ),
        )


def _fixture_streams(directory: Path, relative: Path, identity: NativeIdentity, row: TaskDraft) -> None:
    """Save explicit empty no-native streams and usage receipts for the controlled fixture."""
    for name in ('events.jsonl', 'runtime.log', 'agent.log'):
        (directory / name).write_bytes(b'')
    write_json(
        directory / 'stream_receipt.json',
        {
            'schema_version': 1,
            'status': 'not_started',
            'events_complete': True,
            'runtime_complete': True,
            'transcript_complete': False,
            'events_count': 0,
            'runtime_lines': 0,
            'warnings': [],
        },
    )
    if (directory / 'telemetry/receipt.json').exists():
        return
    assert row.final_attempt is not None
    capture_telemetry(
        directory / 'no-private-native-db',
        directory / 'telemetry',
        run_id=identity.run_id,
        task_id=row.id,
        attempt_id=row.final_attempt.id,
        runtime='opencode:v1.18.33',
        root_id=None,
        opencode_started=False,
        path_prefix=(relative / 'telemetry').as_posix(),
    )


def _fixture_seal(
    run_dir: Path, relative: Path, identity: NativeIdentity, row: TaskDraft
) -> tuple[list[ArtifactReference], ArtifactReference]:
    """Freeze complete fixture evidence before constructing any host score result."""
    directory = run_dir / relative
    receipts = [
        reference(run_dir, (relative / name).as_posix())
        for name in (
            'start.json',
            'agent.diff',
            'triplets/index.json',
            'events.jsonl',
            'runtime.log',
            'stream_receipt.json',
        )
    ]
    telemetry_receipt = directory / 'telemetry/receipt.json'
    if telemetry_receipt.is_file():
        receipts.append(reference(run_dir, telemetry_receipt.relative_to(run_dir).as_posix()))
    for receipt in (row.telemetry.session_db, row.telemetry.usage_ledger):
        if receipt is not None:
            receipts.append(reference(run_dir, receipt.path))
    assert row.final_attempt is not None
    if identity.python_runtime is not None:
        receipts.append(identity.python_runtime)
        receipts.append(write_python_probe(run_dir, relative, identity.python_runtime))
    seal = Seal(
        schema_family='pr_similarity_os.seal',
        schema_version=2,
        run_id=identity.run_id,
        basket_sha256=identity.basket_sha256,
        config_identity_sha256=identity.config_identity_sha256,
        metric=identity.metric,
        runtime_binary_sha256=identity.runtime_binary_sha256,
        python_runtime=identity.python_runtime,
        agent_input=Run.model_validate(read_json(run_dir / 'run.json')).agent_input,
        task=row.model_copy(deep=True),
        native_timing=NativeTiming(
            started_at=row.final_attempt.started_at, finished_at=row.final_attempt.finished_at, duration_seconds=1
        ),
        evidence=SealEvidence(
            required=EvidenceStatus(status='saved', missing=[]),
            streams=EvidenceStatus(status='saved'),
            usage=EvidenceStatus(status='saved', archive_complete=True),
        ),
        archive_complete=True,
        artifact_receipts=receipts,
    )
    write_json(directory / 'seal.json', seal)
    return receipts, reference(run_dir, (relative / 'seal.json').as_posix())


def fake_attempt(
    sequence: list[str],
    *,
    token_counts: list[int] | None = None,
    before_seal: Callable[[TaskDraft, Path, Path, NativeIdentity], None] | None = None,
) -> AttemptExecutor:
    """Build registered fixtures that write immutable native seal and host/outcome facts."""

    def attempt(
        config: RunConfig,
        basket_row: BasketRecord,
        run_dir: Path,
        identity: NativeIdentity,
        runtime: Runtime,
        bwrap: Path,
        command: Command,
        metadata: ReportAttempt,
        stop: Stop,
        *,
        registration: TaskRegistration,
    ) -> Outcome:
        """Assert reservation authority before closing explicitly owned fixture evidence."""
        relative = attempt_path(registration, metadata.sequence)
        directory = run_dir / relative
        assert directory.is_dir()
        start = Start.model_validate(read_json(directory / 'start.json'))
        assert start.attempt == metadata and start.task_id == basket_row.id
        state = State.model_validate(read_json(run_dir / '_meta/state.json'))
        selected = next(row for row in state.tasks if row.id == basket_row.id)
        assert selected.attempt == metadata and selected.start == reference(run_dir, str(relative / 'start.json'))
        assert (run_dir / 'run.json').is_file()
        assert command.metadata.config_snapshot is not None and command.metadata.config_snapshot.startswith(
            '_meta/invocations/'
        )
        kind = sequence.pop(0)
        row = _fixture_native_row(basket_row, metadata, kind, stop, relative, directory, identity)
        if token_counts:
            row.telemetry.tokens.total = token_counts.pop(0)
            row.telemetry.total_source = 'reported'
        if before_seal is not None:
            before_seal(row, directory, run_dir, identity)
        _fixture_streams(directory, relative, identity, row)
        receipts, seal_ref = _fixture_seal(run_dir, relative, identity, row)
        scoring_error = (
            ReportError(code='scoring_worker_failed', message='Synthetic host failure')
            if kind == 'score_error'
            else None
        )
        _fixture_score(row, scoring_error)
        score = ScoringEvidence(
            schema_family='pr_similarity_os.scoring',
            schema_version=2,
            run_id=identity.run_id,
            task_id=row.id,
            attempt_id=metadata.id,
            seal=seal_ref,
            scoring=row.scoring,
            metric=identity.metric,
            diagnostics=[],
            error=scoring_error,
        )
        write_json(directory / 'scoring.json', score)
        score_ref = reference(run_dir, (relative / 'scoring.json').as_posix())
        fatal = (
            ReportError(code='auth_required', message='Renew provider credentials and resume.')
            if kind == 'auth'
            else None
        )
        outcome = Outcome(
            schema_family='pr_similarity_os.outcome',
            schema_version=2,
            run_id=identity.run_id,
            basket_sha256=identity.basket_sha256,
            config_identity_sha256=identity.config_identity_sha256,
            metric=identity.metric,
            runtime_binary_sha256=identity.runtime_binary_sha256,
            python_runtime=identity.python_runtime,
            agent_input=Run.model_validate(read_json(run_dir / 'run.json')).agent_input,
            task=row,
            seal=seal_ref,
            scoring_result=score_ref,
            scoring_error=scoring_error,
            fatal=fatal,
            archive_complete=True,
            artifact_receipts=receipts + [seal_ref, score_ref],
        )
        write_json(directory / 'outcome.json', outcome)
        return outcome

    return attempt


class _CancelledInvocation(Invocation):
    """Call the real cancellation branch before Popen and retain its empty owned log."""

    agent_started = False
    _environment = None

    def __init__(self, config: RunConfig, logs: Path, cwd: Path) -> None:
        """Bind an owned log directory and fixture working directory."""
        super().__init__(
            config,
            Sandbox(
                '/usr/bin/true',
                cwd / 'sandbox-source',
                cwd / 'sandbox-source',
                cwd / 'home',
                cwd / 'runtime',
                Path('/usr/bin/true'),
            ),
            logs,
            logs,
        )
        self.cwd = cwd

    def run(self, _prompt: bytes, stop: Stop) -> tuple[ProcessResult, EventTracker]:
        """Exercise real cancellation without spawning an unavailable runtime."""
        return (
            run_process(
                ['/definitely/not-a-native-runtime'],
                env={},
                cwd=self.cwd,
                timeout=1,
                grace=0.1,
                cancel=stop,
                log=self.logs / 'agent.log',
            ),
            EventTracker(),
        )

    def secrets(self) -> list[str]:
        """Supply the empty redaction boundary for this offline fixture."""
        return []


def _empty_triplet_fixture(
    directory: Path,
    _cache: object,
    before: str,
    after: str,
    _changes: object,
    task_id: str,
    attempt_id: str,
    *,
    run_id: str,
) -> dict:
    """Save a valid empty immutable triplet index for the no-op cancelled workspace."""
    index = {
        'version': 1,
        'run_id': run_id,
        'task_id': task_id,
        'attempt_id': attempt_id,
        'source': {'before': before, 'after': after},
        'entries': [],
    }
    write_json(directory / 'index.json', index)
    return index


def test_infrastructure_always_retries_and_report_is_last_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify infrastructure always retries and report is last attempt."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=1)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['infra', 'success']))
    assert code == 0
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert report.tasks[0].final_attempt is not None
    assert report.tasks[0].final_attempt.sequence == 2
    assert report.tasks[0].infrastructure_error is None
    assert len(list((directory / 'tasks').glob('*/attempts/*/outcome.json'))) == 2


def test_timeout_category_preserves_last_result_and_exhaustion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify timeout category preserves last result and exhaustion."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=1, categories=['timeout_no_changes'])
    code, directory = run(
        config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['timeout', 'timeout'])
    )
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert code == 0 and report.run.status == 'completed'
    assert report.tasks[0].execution.status == 'timeout'
    assert report.tasks[0].final_attempt is not None
    assert report.tasks[0].final_attempt.retry_index == 1


def test_resume_resets_retry_budget_and_preserves_attempt_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify resume resets retry budget and preserves attempt history."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=1, categories=['no_changes'])
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['interrupt']))
    assert code == 130
    code, _ = run(config, basket, resume=directory, attempt_function=fake_attempt(['timeout', 'success']))
    assert code == 0
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert report.tasks[0].final_attempt is not None
    assert report.tasks[0].final_attempt.sequence == 3
    starts = [
        Start.model_validate(read_json(path)).attempt.retry_index
        for path in (directory / 'tasks').glob('*/attempts/*/start.json')
    ]
    assert sorted(starts) == [0, 0, 1]


def test_cancel_before_native_spawn_preserves_not_started_telemetry_for_resume_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled pre-spawn attempt is complete historical evidence, not a missing usage capture."""
    config, basket = config_and_basket(tmp_path)
    runtime = Runtime(
        Path('/usr/bin/true'),
        ProvisioningReceipt(
            selector='opencode:v1.18.33',
            actual_version='1.18.33',
            asset_name='fixture.tar.gz',
            platform='fixture',
            archive_sha256='a' * 64,
            binary_sha256='b' * 64,
        ),
    )

    def prepare_agents(session: object) -> None:
        """Supply stable host receipts without provisioning a runtime or sandbox."""
        assert isinstance(session, coordinator.Coordinator)
        session.runtime = runtime
        session.bwrap = Path('/usr/bin/true')
        session.report = session.store.snapshot()
        session.report.agent.resolved_runtime = ReportAgentResolvedRuntime(
            version='1.18.33',
            asset_name='fixture.tar.gz',
            platform='fixture',
            archive_sha256='a' * 64,
            binary_sha256='b' * 64,
            receipt='_meta/runtime.json',
        )
        write_json(session.run_dir / '_meta/runtime.json', runtime.receipt)
        session.report.agent.python_runtime = freeze_python(session.run_dir)
        session.persist()
        write_json(
            session.run_dir / '_meta/sandbox.json',
            SandboxReceipt(backend='fixture', version='fixture', binary_sha256='c' * 64, network='host'),
        )

    def cancelled_prepare(state: attempt_module._Attempt) -> None:
        """Reach the real agent/capture/finalization path with cancellation before native spawn."""
        state.cache = GitCache(tmp_path / 'fixture-cache', state.basket_row.source.fetch_url)
        state.workspace = Workspace(tmp_path, tmp_path, '.', 'fixture', state.cache, state.row.before)
        state.prompt = b'fixture'
        assert state.report_identity.python_runtime is not None
        write_python_probe(state.run_dir, state.relative, state.report_identity.python_runtime)
        state.invocation = _CancelledInvocation(state.config, state.logs, tmp_path)
        state.stop.signal(signal.SIGINT, None)

    monkeypatch.setattr(coordinator.Coordinator, 'prepare_agents', prepare_agents)
    monkeypatch.setattr(attempt_module._Attempt, 'prepare', cancelled_prepare)
    monkeypatch.setattr(attempt_module, 'capture_changes', lambda *_args: CapturedChanges({}, b'', {}))
    monkeypatch.setattr(attempt_module, 'save_triplets', _empty_triplet_fixture)

    successful = fake_attempt(['success'])
    calls = 0

    def attempts(
        config: RunConfig,
        basket_row: BasketRecord,
        run_dir: Path,
        identity: NativeIdentity,
        runtime: Runtime,
        bwrap: Path,
        command: Command,
        metadata: ReportAttempt,
        stop: Stop,
        *,
        registration: TaskRegistration,
    ) -> Outcome | None:
        """Use real native execution first, then a closed fixture after resume."""
        nonlocal calls
        calls += 1
        target = runner.run_attempt if calls == 1 else successful
        return target(
            config, basket_row, run_dir, identity, runtime, bwrap, command, metadata, stop, registration=registration
        )

    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=attempts)
    assert code == 130
    first = Outcome.model_validate(read_json(next((directory / 'tasks').glob('*/attempts/*/outcome.json'))))
    assert first.archive_complete
    receipt = TelemetryReceipt.model_validate(
        read_json(next((directory / 'tasks').glob('*/attempts/*/telemetry/receipt.json')))
    )
    assert receipt.status == 'not_started' and receipt.archive_complete

    assert run(config, basket, resume=directory, attempt_function=attempts)[0] == 0
    assert calls == 2
    archive = create_archive(directory, tmp_path / 'resumed.tar.gz')
    assert verify_archive(archive.path).complete


def test_completed_resume_calls_no_agent_and_preserves_final_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify completed resume calls no agent and preserves final attempt."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    before = Report.model_validate(read_json(directory / 'report.json')).tasks[0]

    def forbidden(*_args: object, **_kwargs: object) -> None:
        """Fail if the tested path unexpectedly starts native work or HTTP."""
        raise AssertionError('completed task reran')

    code, _ = run(config, basket, resume=directory, attempt_function=forbidden)
    assert code == 0 and Report.model_validate(read_json(directory / 'report.json')).tasks[0] == before


def test_resume_identity_changes_fail_before_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify resume identity changes fail before agent."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    _, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    data = yaml.safe_load((tmp_path / 'config.yaml').read_text())
    data['agent']['timeout_seconds'] = 2
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(data))
    with pytest.raises(BenchmarkError) as caught:
        run(load_config(tmp_path / 'config.yaml'), basket, resume=directory)
    assert caught.value.code == 'resume_identity_mismatch'


def test_automatic_pricing_gap_is_distinct_from_agent_run_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify automatic pricing gap is distinct from agent run failure."""
    runner = provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    monkeypatch.setattr(runner, 'load_policy', lambda path: object())
    monkeypatch.setattr(runner, 'freeze_pricing', lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runner, 'apply_pricing', lambda *_args, **_kwargs: 1)
    code, directory = run(
        config,
        basket,
        output=tmp_path / 'runs',
        pricing=tmp_path / 'pricing.yaml',
        attempt_function=fake_attempt(['success']),
        level='ERROR',
    )
    assert code == 1
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert report.run.status == 'completed'
    assert report.summary.scored_tasks == 1
    assert report.summary.infrastructure_error_tasks == 0
    console = capsys.readouterr().err
    assert 'Agent run status: completed' in console
    assert 'nonzero exit code for pricing gaps' in console


def test_resume_records_abandoned_attempt_and_starts_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsealed partial evidence stays diagnostic and explicit resume reserves a fresh sequence."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)

    def partial(*args: object, **kwargs: object) -> None:
        """Leave only an owned runtime log, without pretending a native outcome exists."""
        run_dir, metadata = args[2], args[7]
        assert isinstance(run_dir, Path) and isinstance(metadata, ReportAttempt)
        registration = kwargs['registration']
        assert isinstance(registration, TaskRegistration)
        (run_dir / attempt_path(registration, metadata.sequence) / 'runtime.log').write_text(
            'partial fixture evidence\n'
        )
        raise RuntimeError('worker ended before native seal')

    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=partial)
    assert code == 1
    before = State.model_validate(read_json(directory / '_meta/state.json')).tasks[0]
    first = directory / RunStore.open(directory).attempt_directory(before.id, 1)
    start = (first / 'start.json').read_bytes()
    assert before.seal is None and before.outcome is None
    code, _ = run(config, basket, resume=directory, attempt_function=fake_attempt(['success']))
    assert code == 0
    current = Report.model_validate(read_json(directory / 'report.json'))
    assert current.tasks[0].final_attempt is not None
    assert current.tasks[0].final_attempt.sequence == 2
    assert (first / 'start.json').read_bytes() == start
    assert (first / 'runtime.log').read_text() == 'partial fixture evidence\n'
    assert not (first / 'outcome.json').exists()
    history = State.model_validate(read_json(directory / '_meta/state.json')).tasks[0].history
    assert len(history) == 1 and history[0].seal is None and history[0].outcome is None


def test_corrupt_closed_outcome_blocks_resume_without_native_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing accepted immutable evidence is corruption, never permission to fabricate a new outcome."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    _, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    outcome = next((directory / 'tasks').glob('*/attempts/*/outcome.json'))
    outcome.unlink()
    state = (directory / '_meta/state.json').read_bytes()
    invocations = list((directory / '_meta/invocations').iterdir())
    with pytest.raises(BenchmarkError):
        run(config, basket, resume=directory, attempt_function=fake_attempt([]))
    assert (directory / '_meta/state.json').read_bytes() == state
    assert list((directory / '_meta/invocations').iterdir()) == invocations
    assert not (outcome.parent.parent / '002').exists()


def test_resume_rejects_damaged_saved_inputs_before_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify resume rejects damaged saved inputs before agent."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    _, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    (directory / 'basket.json').write_bytes((directory / 'basket.json').read_bytes() + b'\n')
    with pytest.raises(BenchmarkError) as caught:
        run(config, basket, resume=directory, attempt_function=fake_attempt([]))
    assert caught.value.code == 'run_input_mismatch'


def test_unexpected_worker_error_is_friendly_and_saved_at_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify unexpected worker error is friendly and saved at debug."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)

    def broken(*args: object, **kwargs: object) -> None:
        """Simulate a log stream that raises an I/O error on every write."""
        raise RuntimeError('fixture internal failure details')

    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=broken, level='ERROR')
    assert code == 1
    assert Report.model_validate(read_json(directory / 'report.json')).run.status == 'failed'
    console = capsys.readouterr().err
    assert 'Traceback' not in console and 'fixture internal failure details' not in console
    logs = ''.join(path.read_text() for path in directory.glob('_meta/invocations/*/benchmark.log'))
    assert 'fixture internal failure details' in logs and 'Traceback' in logs


def test_missing_metric_dependency_saves_failed_preflight_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify missing metric dependency saves failed preflight report."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)

    def absent() -> None:
        """Reject metric preflight as if a required pinned dependency were absent."""
        raise BenchmarkError('metric_dependencies_missing', 'Install the pinned metric dependencies.', exit_code=1)

    monkeypatch.setattr(scoring, 'require_dependencies', absent)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt([]))
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert code == 1 and report.run.status == 'failed'
    assert report.run.error is not None
    assert report.run.error.code == 'metric_dependencies_missing'
    assert report.summary.pending_tasks == report.basket.total_tasks
    assert not list((directory / 'tasks').glob('*/attempts/*/start.json'))


def test_retry_history_keeps_all_usage_but_report_uses_final_800_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep 1000/1500/800-token attempts in history without summing them into final telemetry."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=2)
    totals = iter([1000, 1500, 800])

    def measured(row: TaskDraft, attemptdir: Path, directory: Path, identity: NativeIdentity) -> None:
        """Capture one native database before sealing its immutable usage receipts."""
        assert row.final_attempt is not None
        source = tmp_path / (row.final_attempt.id + '.db')
        db = native_db(source)
        total = next(totals)
        try:
            db.execute('delete from part where session_id != ?', ('root',))
            db.execute('delete from message where session_id != ?', ('root',))
            db.execute('delete from session where id != ?', ('root',))
            data = json.loads(db.execute('select data from part').fetchone()[0])
            data['tokens'] = {'input': total - 100, 'output': 100, 'reasoning': 0, 'total': total}
            db.execute('update part set data = ?', (json.dumps(data),))
            db.commit()
            captured = capture_telemetry(
                source,
                attemptdir / 'telemetry',
                run_id=identity.run_id,
                task_id=row.id,
                attempt_id=row.final_attempt.id,
                runtime='opencode:v1.18.33',
                root_id='root',
                opencode_started=True,
                path_prefix=(attemptdir / 'telemetry').relative_to(directory).as_posix(),
            )
        finally:
            db.close()
        row.telemetry = captured.data

    code, directory = run(
        config,
        basket,
        output=tmp_path / 'runs',
        attempt_function=fake_attempt(['infra', 'infra', 'success'], before_seal=measured),
    )
    assert code == 0
    final = Report.model_validate(read_json(directory / 'report.json')).tasks[0].telemetry
    assert final.tokens.total == 800
    assert final.tokens.cache_read is None and final.tokens.cache_write is None
    histories = [
        Outcome.model_validate(read_json(path)) for path in (directory / 'tasks').glob('*/attempts/*/outcome.json')
    ]
    saved_totals = [row.task.telemetry.tokens.total for row in histories]
    assert all(value is not None for value in saved_totals)
    assert sorted(value for value in saved_totals if value is not None) == [800, 1000, 1500]


@pytest.mark.parametrize('max_retries', [0, 2], ids=['no_automatic_retry', 'automatic_retry_budget'])
def test_provider_auth_failure_remains_pending_for_explicit_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_retries: int
) -> None:
    """Keep fatal authentication failures distinct from interrupt and retry them after explicit resume."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=max_retries)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['auth']))
    first = Report.model_validate(read_json(directory / 'report.json'))
    assert code == 1 and first.run.status == 'failed'
    assert first.tasks[0].processing_status == 'retry_pending'
    assert first.tasks[0].infrastructure_error is not None
    assert first.tasks[0].infrastructure_error.code == 'auth_required'
    assert run(config, basket, resume=directory, attempt_function=fake_attempt(['success']))[0] == 0
    last = Report.model_validate(read_json(directory / 'report.json')).tasks[0]
    assert last.final_attempt is not None
    assert last.final_attempt.sequence == 2 and last.final_attempt.retry_index == 0
    assert last.infrastructure_error is None


def test_preflight_interrupt_stays_interrupted_with_pending_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep user cancellation during runtime preparation distinct from infrastructure failure."""
    config, basket = config_and_basket(tmp_path)

    def interrupted(selector: str, cache_dir: Path, *, timeout: int, cancel: threading.Event) -> NoReturn:
        """Raise a real cancellation-shaped outcome after recording the user signal."""
        assert isinstance(cancel, Stop)
        cancel.signal(2, None)
        raise BenchmarkError('operation_cancelled', 'Preparation interrupted.', exit_code=130)

    monkeypatch.setattr(coordinator, 'provision_opencode', interrupted)
    code, directory = run(config, basket, output=tmp_path / 'runs')
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert code == 130 and report.run.status == 'interrupted'
    assert report.run.error is None
    assert all(row.infrastructure_error is None and row.final_attempt is None for row in report.tasks)


@pytest.mark.parametrize('boundary', ['accept_fact', 'accept'])
def test_scheduler_failure_cancels_sibling_process_before_pool_unwinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Cancel a real sibling child when main-thread validation or persistence fails."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    data = BasketV3.model_validate(read_json(basket))
    second = data.tasks[0].model_copy(deep=True)
    second.id = task_identity(data.meta.repository, 'pull_request', 2)
    second.change_request.number, second.change_request.url = 2, 'https://github.com/owner/repo/pull/2'
    data.tasks.append(second)
    data.meta.counts.input = data.meta.counts.succeeded = 2
    for counts in (data.meta.coverage.collect, data.meta.coverage.filter, data.meta.coverage.generate):
        counts.input = counts.succeeded = 2
    data.meta.coverage.discovery.matched = data.meta.coverage.discovery.selected = data.meta.coverage.selected = 2
    selection = data.meta.selection
    selection.selected_ids.append(second.id)
    assert isinstance(second.provenance.generation, Generation)
    selection.generation_keys[second.id] = second.provenance.generation.input_sha256
    selection.fingerprint = sha256(
        {'tasks': [[identifier, selection.generation_keys[identifier]] for identifier in selection.selected_ids]}
    )
    write_json(basket, data)
    started = threading.Event()
    results: list[ProcessResult] = []
    failure_time: list[float] = []
    success = fake_attempt(['success'])
    original_fact = RunStore.accept_fact
    original_accept = coordinator.Coordinator.accept

    def maybe_fail() -> None:
        """Inject one main-thread failure after both workers have begun execution."""
        ready = threading.current_thread() is threading.main_thread() and started.is_set()
        if ready and not failure_time:
            failure_time.append(time.monotonic())
            raise BenchmarkError('artifact_write_failed', 'Fixture persistence failure.', exit_code=1)

    def fail_fact(
        self: RunStore,
        task_id: str,
        kind: str,
        receipt: ArtifactReference,
        *,
        processing_status: Literal['pending', 'running', 'completed', 'retry_pending', 'interrupted'] | None = None,
    ) -> Report:
        """Fail one fact acceptance while leaving subsequent drain commits intact."""
        maybe_fail()
        return original_fact(self, task_id, kind, receipt, processing_status=processing_status)

    def fail_accept(self: coordinator.Coordinator, identifier: str, returned: Outcome | None) -> str:
        """Fail one coordinator acceptance before the owned sibling is drained."""
        maybe_fail()
        return original_accept(self, identifier, returned)

    def execute(
        config: RunConfig,
        row: BasketRecord,
        run_dir: Path,
        identity: NativeIdentity,
        runtime: Runtime,
        bwrap: Path,
        command: Command,
        metadata: ReportAttempt,
        stop: Stop,
        *,
        registration: TaskRegistration,
    ) -> Outcome | None:
        """Keep the second worker inside an owned native process until cancellation."""
        if row.id != second.id:
            assert started.wait(3)
            return success(
                config, row, run_dir, identity, runtime, bwrap, command, metadata, stop, registration=registration
            )
        results.append(
            run_process(
                ['/bin/sh', '-c', 'printf "ready\\n"; exec sleep 5'],
                env=os.environ.copy(),
                cwd=tmp_path,
                timeout=float('inf'),
                grace=0.1,
                cancel=stop,
                log=tmp_path / 'sibling.log',
                on_stdout=lambda _line: started.set(),
            )
        )
        return None

    if boundary == 'accept_fact':
        monkeypatch.setattr(RunStore, boundary, fail_fact)
    else:
        monkeypatch.setattr(coordinator.Coordinator, boundary, fail_accept)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=execute)
    assert code == 1 and Report.model_validate(read_json(directory / 'report.json')).run.status == 'failed'
    assert failure_time and time.monotonic() - failure_time[0] < 3
    assert len(results) == 1 and results[0].status == 'cancelled'
    assert results[0].exit_code is not None


def test_yaml_labels_and_frozen_native_inference_survive_resume_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Display edits cannot replace frozen inference facts or replay a completed agent."""
    provision_fixture(monkeypatch)
    _config, basket = config_and_basket(tmp_path)
    path = tmp_path / 'config.yaml'
    data = yaml.safe_load(path.read_text())
    data['tags'] = ['model:manual-label', 'Effort:Label', 'duplicate', 'duplicate']
    data['agent']['name'] = 'Original display'
    data['agent']['config']['opencode_json'] = {
        'model': 'openai/gpt-6.1-sol',
        'provider': {'openai': {'models': {'gpt-6.1-sol': {'options': {'reasoningEffort': 'medium'}}}}},
    }
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    assert code == 0
    before = Report.model_validate(read_json(directory / 'report.json'))
    assert before.run.tags == data['tags']
    configured = before.inference.configured
    assert configured.model.value == 'openai/gpt-6.1-sol'
    assert configured.effort.value == 'medium'
    assert configured.effort.source is not None
    assert configured.effort.source.ref == 'config_identity.json'
    data['tags'] = ['new label']
    data['agent']['name'] = 'New display'
    path.write_text(yaml.safe_dump(data))

    def no_replay(*_args: object, **_kwargs: object) -> None:
        """Any native call would violate completed-run resume semantics."""
        raise AssertionError('A completed task must not be replayed.')

    changed = load_config(path)
    assert changed.identity() == config.identity()
    assert run(changed, basket, resume=directory, attempt_function=no_replay)[0] == 0
    after = Report.model_validate(read_json(directory / 'report.json'))
    assert after.run.tags == before.run.tags
    assert after.agent.name == before.agent.name
    assert after.inference == before.inference
    assert after.tasks[0].final_attempt == before.tasks[0].final_attempt


def test_coordinator_freezes_registry_and_reserves_before_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New execution has one state authority and readable registered artifact paths."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    assert code == 0
    assert directory.name.startswith('20') and '-fixture-' in directory.name
    assert not (directory / 'checkpoint.json').exists()
    report = Report.model_validate(read_json(directory / 'report.json'))
    state = State.model_validate(read_json(directory / '_meta/state.json'))
    row = report.tasks[0]
    assert row.directory.startswith('tasks/001-')
    assert row.artifacts.outcome is not None
    assert row.artifacts.outcome.startswith(row.directory + '/attempts/001/')
    assert state.tasks[0].outcome == reference(directory, row.artifacts.outcome)


def test_host_scoring_recovery_reuses_seal_without_native_runtime_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed host score resumes against the same immutable attempt without any native setup."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=2, categories=['agent_error'])
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['score_error']))
    assert code == 1
    original = Report.model_validate(read_json(directory / 'report.json')).tasks[0]
    assert original.artifacts.outcome is not None
    outcome_path = directory / original.artifacts.outcome
    outcome = outcome_path.read_bytes()
    metadata = yaml.safe_load((tmp_path / 'config.yaml').read_text())
    metadata['agent']['credentials'] = {'TEST_API_KEY': 'UNSET_FIXTURE_ACCESS'}
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump(metadata))
    changed = load_config(tmp_path / 'config.yaml')
    assert changed.identity() == config.identity()
    monkeypatch.delenv('UNSET_FIXTURE_ACCESS', raising=False)

    def forbidden(*args: object, **kwargs: object) -> None:
        """Host recovery must never provision, authenticate, sandbox or run native generation."""
        raise AssertionError('Host recovery attempted native preparation.')

    for name in ('provision_opencode', 'resolve_bubblewrap', 'auth_snapshot'):
        monkeypatch.setattr(coordinator, name, forbidden)
    code, _ = run(changed, basket, resume=directory, attempt_function=forbidden)
    assert code == 0
    final = Report.model_validate(read_json(directory / 'report.json')).tasks[0]
    assert final.final_attempt == original.final_attempt
    assert final.scoring.status == 'available'
    assert outcome_path.read_bytes() == outcome
    state = State.model_validate(read_json(directory / '_meta/state.json')).tasks[0]
    assert state.scoring is not None and state.next_sequence == 2
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1


def test_relocated_completed_run_rebuilds_missing_report_without_native_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived presentation can be regenerated after moving all relative owned artifacts."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    _, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    before = Report.model_validate(read_json(directory / 'report.json'))
    moved = directory.with_name('relocated-run')
    directory.rename(moved)
    (moved / 'report.json').unlink()

    def forbidden(*args: object, **kwargs: object) -> None:
        """Completed state is sufficient for rebuilding report without runtime or model work."""
        raise AssertionError('A closed native attempt was replayed.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    assert run(config, basket, resume=moved, attempt_function=forbidden)[0] == 0
    after = Report.model_validate(read_json(moved / 'report.json'))
    assert after.run.id == before.run.id
    assert after.tasks == before.tasks
    assert (moved / 'run.json').exists() and not (moved / 'checkpoint.json').exists()
    assert not (moved / 'report.html').exists() and not list(moved.glob('*.md'))


def test_terminal_native_error_is_saved_without_unconfigured_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exhausted native error remains an explicit failed execution even with a valid score."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path, retries=2)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['agent_error']))
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert code == 1 and report.run.status == 'completed_with_errors'
    assert report.tasks[0].execution.status == 'error'
    assert report.tasks[0].scoring.status == 'available'
    assert report.tasks[0].infrastructure_error is None
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1


def test_disposable_invocation_metadata_cannot_block_completed_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed presentation metadata does not invalidate owned native facts or state."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    _, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    metadata = next(directory.glob('_meta/invocations/*/invocation.json'))
    metadata.write_text('{broken')

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        """Fail if a completed logical task is submitted to native execution again."""
        raise AssertionError('completed run must reuse owned facts')

    code, _ = run(config, basket, resume=directory, attempt_function=forbidden)
    assert code == 0
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1


def test_worker_phase_updates_are_coalesced_and_committed_by_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker phase callbacks become one bounded main-thread state observation."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    release = threading.Event()
    observed = []
    original = RunStore.observe
    complete = fake_attempt(['success'])

    def observe(store: RunStore, task_id: str, phase: str, stamp: str) -> Report:
        """Accept the coarse phase only on the coordinator's writer thread."""
        assert threading.current_thread() is threading.main_thread()
        observed.append(phase)
        result = original(store, task_id, phase, stamp)
        release.set()
        return result

    def attempt(
        config: RunConfig,
        row: BasketRecord,
        run_dir: Path,
        identity: NativeIdentity,
        runtime: Runtime,
        bwrap: Path,
        command: Command,
        metadata: ReportAttempt,
        stop: Stop,
        *,
        registration: TaskRegistration,
    ) -> Outcome | None:
        """Emit repeated presentation phases while waiting for a durable observation."""
        task_id = row.id
        for _ in range(100):
            command.task_phase(task_id, 'running')
        assert release.wait(2), 'coordinator must flush coarse observations while the worker runs'
        return complete(
            config, row, run_dir, identity, runtime, bwrap, command, metadata, stop, registration=registration
        )

    monkeypatch.setattr(RunStore, 'observe', observe)
    code, directory = run(config, basket, output=tmp_path / 'runs', attempt_function=attempt)
    assert code == 0 and observed == ['running']
    selected = State.model_validate(read_json(directory / '_meta/state.json')).tasks[0]
    assert selected.processing_status == 'completed' and selected.outcome is not None


@pytest.mark.parametrize('partial_evidence', [False, True])
def test_real_lock_initialization_failure_cleans_only_empty_owned_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial_evidence: bool
) -> None:
    """Actual lock files do not create ghost runs, while any partial evidence is retained."""
    config, basket = config_and_basket(tmp_path)
    output = tmp_path / 'runs'

    def fail_create(_cls: type[RunStore], directory: Path, *_args: object, **kwargs: object) -> NoReturn:
        """Fail after acquiring the real lease and optionally retain one already-written input."""
        assert (directory / '.lock').is_file() and getattr(kwargs['lease'], 'active')
        if partial_evidence:
            (directory / 'operator-note.txt').write_text('Retain partial evidence.\n')
        raise BenchmarkError('creation_failed', 'Fixture initialization failure.')

    monkeypatch.setattr(RunStore, 'create', classmethod(fail_create))
    with pytest.raises(BenchmarkError, match='Fixture initialization failure'):
        run(config, basket, output=output)
    retained = list(output.iterdir())
    if partial_evidence:
        assert len(retained) == 1
        assert (retained[0] / 'operator-note.txt').read_text() == 'Retain partial evidence.\n'
        assert (retained[0] / '.lock').is_file()
    else:
        assert retained == []


def test_empty_allocation_cleanup_preserves_another_active_writer(tmp_path: Path) -> None:
    """Cleanup cannot unlink a sole lock that another writer acquired after the original lease."""
    directory = tmp_path / 'new-allocation'
    directory.mkdir()
    allocation = directory.lstat()
    with run_lock(directory):
        original = (directory / '.lock').lstat()
    acquired, release = threading.Event(), threading.Event()

    def writer() -> None:
        """Hold a second real lease until the cleanup check has observed ownership."""
        with file_lock(directory / '.lock'):
            acquired.set()
            assert release.wait(3)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert acquired.wait(2)
        coordinator._cleanup_empty_allocation(directory, allocation, original)
        assert directory.is_dir() and (directory / '.lock').is_file()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()


def test_empty_allocation_cleanup_preserves_replaced_lock_inode(tmp_path: Path) -> None:
    """A replacement even empty regular lock is not the file created by the original lease."""
    directory = tmp_path / 'new-allocation'
    directory.mkdir()
    allocation = directory.lstat()
    with run_lock(directory):
        original = (directory / '.lock').lstat()
    descriptor = os.open(directory / '.lock', os.O_RDONLY)
    try:
        (directory / '.lock').unlink()
        (directory / '.lock').write_bytes(b'')
        replacement = (directory / '.lock').lstat()
        assert replacement.st_ino != original.st_ino
        coordinator._cleanup_empty_allocation(directory, allocation, original)
        assert (directory / '.lock').lstat().st_ino == replacement.st_ino
    finally:
        os.close(descriptor)
