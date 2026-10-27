"""One authoritative run state selects immutable attempts and offline results."""

import copy
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

from benchmark.config import RunConfig
from benchmark.errors import BenchmarkError
from benchmark.execution.agent_input import freeze_agent_input, validate_agent_input
from benchmark.execution.usage import validate_usage
from benchmark.models import Report, ReportDraft, Run, State, Usage
from benchmark.models.base import WireModel
from benchmark.models.evidence import (
    ConfigIdentity,
    EffectiveConfig,
    OperationDetails,
    OperationReceipt,
    OperationResult,
    OperationSelection,
    Outcome,
    PriceItem,
    PricingRequest,
    PublishFingerprint,
    PublishReceipt,
    PublishRequest,
    ReservationNotice,
    RuntimeReceipt,
    ScoreItem,
    ScoringEvidence,
    Seal,
    Start,
)
from benchmark.models.report import (
    ReportAgentResolvedRuntime,
    ReportAttempt,
    ReportError,
    ReportLastReprice,
    ReportLastRescore,
    ReportMetric,
    ReportState,
    ReportTask,
    ReportTaskProgress,
    ReportTaskScoring,
    TaskDraft,
)
from benchmark.models.run import RunBaseline
from benchmark.models.runtime import PythonReceipt
from benchmark.models.state import (
    StateLastPublish,
    StatePendingPublish,
    StateRun,
    StateSelectedOperations,
    StateTaskItem,
    StateTaskItemHistoryItem,
)
from benchmark.persistence.artifact_refs import ArtifactReference, read_reference_json
from benchmark.persistence.artifact_refs import read_reference as read_reference
from benchmark.persistence.artifact_refs import reference as reference
from benchmark.persistence.layout import (
    TaskRegistration,
    artifact_path,
    attempt_path,
    task_registry,
)
from benchmark.persistence.operations import operation_identifier, validate_operation
from benchmark.persistence.python_evidence import validate_python_fact
from benchmark.persistence.storage import (
    RunLease,
    atomic_write,
    bytes_sha256,
    document_bytes,
    parse_json,
    read_json,
    run_lock,
    sha256,
    utc_now,
    write_json,
)
from benchmark.presentation.inference import build_inference, generated_groups
from benchmark.presentation.result import (
    pending_task,
    project_task,
    summary,
    unavailable_scoring,
    validate_report,
)
from benchmark.pricing.summary import CostEvidenceError, build_cost_summary
from benchmark.validation.basket import Basket, load_basket
from benchmark.validation.contracts import validate_document


def _selection(registration: TaskRegistration) -> StateTaskItem:
    """Initialize scheduling without copying mutable native result state."""
    return StateTaskItem(
        id=registration.id,
        ordinal=registration.ordinal,
        processing_status='pending',
        attempt=None,
        next_sequence=1,
        retry_index=0,
        retry_epoch=0,
        outcome=None,
        start=None,
        seal=None,
        scoring=None,
        pricing=None,
        progress=ReportTaskProgress(phase='pending', updated_at=None),
        history=[],
    )


def _parse_model[T: WireModel](model: type[T], value: object, code: str) -> T:
    """Reject malformed producer objects without exposing submitted field values."""
    try:
        return model.model_validate(value)
    except ValidationError:
        raise BenchmarkError(code, 'Artifact does not match its expected object contract.') from None


@dataclass(frozen=True)
class AttemptSelection:
    """Pair a task identity with one current or historical native selection."""

    id: str
    attempt: ReportAttempt
    start: ArtifactReference | None
    seal: ArtifactReference | None
    outcome: ArtifactReference | None
    scoring: OperationSelection | None
    pricing: OperationSelection | None


def _selected(task_id: str, row: StateTaskItem | StateTaskItemHistoryItem) -> AttemptSelection:
    """Associate a native owner with its task without altering persisted history."""
    assert row.attempt is not None
    return AttemptSelection(task_id, row.attempt, row.start, row.seal, row.outcome, row.scoring, row.pricing)


def _attempts(task: StateTaskItem) -> list[AttemptSelection]:
    """List historical and current native owners in their original sequence order."""
    result = [_selected(task.id, item) for item in task.history]
    if task.attempt is not None:
        result.append(_selected(task.id, task))
    return result


def _attempt_identity(attempt: ReportAttempt) -> tuple[str, str, int, int, str]:
    """Compare reserved ownership independently of mutable finish time."""
    return attempt.id, attempt.invocation_id, attempt.sequence, attempt.retry_index, attempt.started_at


def _same_task_identity(row: TaskDraft, frozen: TaskDraft) -> bool:
    """Compare immutable task inputs without serializing mutable execution state."""
    return (
        row.repository == frozen.repository
        and row.number == frozen.number
        and row.url == frozen.url
        and row.title == frozen.title
        and row.kind == frozen.kind
        and row.source == frozen.source
        and row.diff_policy == frozen.diff_policy
        and row.before == frozen.before
        and row.after == frozen.after
        and row.before_tree == frozen.before_tree
        and row.after_tree == frozen.after_tree
        and row.workspace == frozen.workspace
        and row.agent_task == frozen.agent_task
    )


class RunStore:
    """Own the immutable registry, atomic selections and deterministic public snapshot."""

    def __init__(
        self,
        directory: Path,
        registry: Run,
        state: State,
        *,
        lease: RunLease | None = None,
        state_digest: str | None = None,
        allow_omitted_publish_payloads: bool = False,
    ) -> None:
        """Bind validated records without writing during read-only construction."""
        validate_document('run', registry)
        validate_document('state', state)
        self.directory = directory
        self.registry = registry
        self.state = state
        self.lease = lease
        self.allow_omitted_publish_payloads = allow_omitted_publish_payloads
        self._disk_digest = state_digest or bytes_sha256(document_bytes(state))
        self._tasks = {row.id: row for row in registry.tasks}
        self._facts_cache: dict[tuple[str, str, str, str, str, int], TaskDraft] = {}

    @classmethod
    def create(
        cls, directory: Path, initial: ReportDraft, basket: Basket, config: RunConfig, *, lease: RunLease | None = None
    ) -> 'RunStore':
        """Serialize initialization and never overwrite an already allocated registry."""
        if lease is not None:
            if not lease.active or lease.directory != directory.resolve():
                raise BenchmarkError('writer_lease_expired', 'Run creation requires its active directory lease.')
            return cls._create(directory, initial, basket, config, lease)
        with run_lock(directory) as acquired:
            store = cls._create(directory, initial, basket, config, acquired)
            store.lease = None
            return store

    @classmethod
    def _create(
        cls, directory: Path, initial: ReportDraft, basket: Basket, config: RunConfig, lease: RunLease
    ) -> 'RunStore':
        """Freeze creation inputs and reserve authoritative state before any native work."""
        if any(path.name != '.lock' for path in directory.iterdir()):
            raise BenchmarkError(
                'run_exists', 'Run directory already contains artifacts; preserve it and use RESUME or a new run.'
            )
        identity = config.identity()
        agent_input = freeze_agent_input(directory, config)
        atomic_write(directory / 'basket.json', basket.content)
        write_json(directory / 'config_identity.json', identity)
        baseline = RunBaseline(
            schema_family=initial.schema_family,
            schema_version=initial.schema_version,
            run=copy.deepcopy(initial.run),
            basket=copy.deepcopy(initial.basket),
            agent=copy.deepcopy(initial.agent),
            config=copy.deepcopy(initial.config),
            metric=copy.deepcopy(initial.metric),
            last_rescore=copy.deepcopy(initial.last_rescore),
            last_reprice=copy.deepcopy(initial.last_reprice),
            inference=build_inference(identity),
        )
        registry = Run(
            schema_family='pr_similarity_os.run',
            schema_version=2,
            id=initial.run.id,
            agent_input=agent_input,
            baseline=baseline,
            tasks=task_registry(basket.rows, basket.data.meta.repository),
        )
        write_json(directory / 'run.json', registry)
        state = State(
            schema_family='pr_similarity_os.state',
            schema_version=2,
            run_id=registry.id,
            registry_sha256=bytes_sha256(document_bytes(registry)),
            revision=1,
            updated_at=initial.run.updated_at,
            run=StateRun(
                status=initial.run.status,
                finished_at=initial.run.finished_at,
                error=initial.run.error,
                current_invocation_id=initial.run.current_invocation_id,
            ),
            resolved_runtime=initial.agent.resolved_runtime,
            python_runtime=None,
            metric=initial.metric,
            selected_operations=StateSelectedOperations(rescore=None, reprice=None),
            pending_publish=None,
            last_publish=None,
            tasks=[_selection(entry) for entry in registry.tasks],
        )
        write_json(directory / '_meta/state.json', state)
        store = cls(directory, registry, state, lease=lease)
        store.validate()
        store.rebuild()
        return store

    @classmethod
    def open(
        cls, directory: Path, *, lease: RunLease | None = None, allow_omitted_publish_payloads: bool = False
    ) -> 'RunStore':
        """Reject incompatible pre-release layouts before any mutation or execution."""
        if (directory / 'run.json').is_file() != (directory / '_meta/state.json').is_file():
            raise BenchmarkError(
                'incomplete_run_state',
                'Run registry/state is incomplete; preserve this directory and use a new run or restore its backup.',
            )
        if not (directory / 'run.json').is_file() or not (directory / '_meta/state.json').is_file():
            raise BenchmarkError(
                'unsupported_run_format',
                'Run lacks the current registry/state layout; use the tool version that created it.',
            )
        raw_registry = read_json(artifact_path(directory, 'run.json', must_exist=True))
        if 'agent_input' not in raw_registry:
            raise BenchmarkError(
                'unsupported_run_format', 'Run predates shared native inputs; use its creating tool version.'
            )
        content = artifact_path(directory, '_meta/state.json', must_exist=True).read_bytes()
        raw_state = parse_json(content)
        validate_document('run', raw_registry)
        validate_document('state', raw_state)
        registry, state = _parse_model(Run, raw_registry, 'registry_task_mismatch'), _parse_model(
            State, raw_state, 'invalid_state'
        )
        store = cls(
            directory,
            registry,
            state,
            lease=lease,
            state_digest=bytes_sha256(content),
            allow_omitted_publish_payloads=allow_omitted_publish_payloads,
        )
        store.validate()
        return store

    def task(self, task_id: str) -> TaskRegistration:
        """Resolve ownership by the frozen task registry rather than display guesses."""
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise BenchmarkError(
                'unknown_task_identity', 'Artifact belongs to a task outside the frozen run.'
            ) from error

    def attempt_directory(self, task_id: str, sequence: int) -> Path:
        """Return the registered numbered location for a native attempt."""
        return attempt_path(self.task(task_id), sequence)

    def validate(self) -> None:
        """Validate frozen input bytes, registry mapping and each state selection."""
        for value, family in ((self.registry, 'run'), (self.state, 'state')):
            if value.schema_family != 'pr_similarity_os.' + family or type(value.schema_version) is not int:
                raise BenchmarkError('unsupported_run_format', 'Run registry/state has an incompatible schema.')
            if value.schema_version != 2:
                raise BenchmarkError('unsupported_run_format', 'Run registry/state has an unsupported schema version.')
        if self.state.run_id != self.registry.id:
            raise BenchmarkError('state_owner_mismatch', 'State and immutable registry belong to different runs.')
        if bytes_sha256((self.directory / 'run.json').read_bytes()) != self.state.registry_sha256:
            raise BenchmarkError('registry_digest_mismatch', 'Immutable run registry is damaged.')
        basket = load_basket(artifact_path(self.directory, 'basket.json', must_exist=True))
        baseline = self.registry.baseline
        if (
            baseline.run.id != self.registry.id
            or baseline.basket.path != 'basket.json'
            or baseline.config.identity_path != 'config_identity.json'
        ):
            raise BenchmarkError('run_input_mismatch', 'Run baseline has foreign ownership or input locations.')
        identity = _parse_model(
            ConfigIdentity,
            read_json(artifact_path(self.directory, 'config_identity.json', must_exist=True)),
            'run_input_mismatch',
        )
        if basket.sha256 != baseline.basket.sha256 or sha256(identity) != baseline.config.identity_sha256:
            raise BenchmarkError(
                'run_input_mismatch', 'Frozen basket/config inputs are damaged or belong to another run.'
            )
        validate_agent_input(self.directory, self.registry.agent_input, identity)
        if baseline.inference != build_inference(identity):
            raise BenchmarkError(
                'inference_owner_mismatch', 'Configured inference differs from its frozen native config.'
            )
        expected = task_registry(basket.rows, basket.data.meta.repository)
        if self.registry.tasks != expected:
            raise BenchmarkError('registry_task_mismatch', 'Registry differs from the complete frozen basket.')
        if type(self.state.revision) is not int or self.state.revision < 1:
            raise BenchmarkError('invalid_state', 'State revision must be a positive integer.')
        self._validate_runtime_projection()
        if self.state.selected_operations.rescore is None and self.state.metric != baseline.metric:
            raise BenchmarkError('metric_owner_mismatch', 'State metric differs from the frozen initial metric.')
        for kind, selected in (
            ('rescore', self.state.selected_operations.rescore),
            ('reprice', self.state.selected_operations.reprice),
        ):
            if selected is not None:
                operation = validate_operation(self.directory, self.registry, selected, kind)
                if kind == 'rescore' and operation.result.metric != self.state.metric:
                    raise BenchmarkError(
                        'metric_owner_mismatch', 'State metric differs from its selected score operation.'
                    )
                if selected.accepted_revision > self.state.revision:
                    raise BenchmarkError('operation_commit_mismatch', 'Root operation selection is newer than state.')
        for selected, closed in ((self.state.pending_publish, False), (self.state.last_publish, True)):
            if selected is not None:
                self._validate_publication(selected, closed=closed)
        self._validate_selections()

    def _validate_runtime_projection(self) -> None:
        """Bind the runtime projection to its root receipt and frozen selector."""
        python = self.state.python_runtime
        if python is not None:
            if python.path != '_meta/python.json':
                raise BenchmarkError('python_receipt_mismatch', 'Python receipt has an unexpected location.')
            python_receipt = read_reference(self.directory, python, PythonReceipt)
            if python_receipt.preflight != 'passed':
                raise BenchmarkError('python_preflight_missing', 'Run Python has no successful sandbox preflight.')
        resolved = self.state.resolved_runtime
        if resolved is None:
            return
        if resolved.receipt != '_meta/runtime.json':
            raise BenchmarkError('runtime_receipt_mismatch', 'Resolved runtime has a foreign receipt location.')
        receipt = _parse_model(
            RuntimeReceipt,
            read_json(artifact_path(self.directory, '_meta/runtime.json', must_exist=True)),
            'runtime_receipt_mismatch',
        )
        expected = ReportAgentResolvedRuntime(
            version=receipt.actual_version,
            asset_name=receipt.asset_name,
            platform=receipt.platform,
            archive_sha256=receipt.archive_sha256,
            binary_sha256=receipt.binary_sha256,
            receipt='_meta/runtime.json',
        )
        selector = self.registry.baseline.agent.runtime
        if (
            resolved != expected
            or not selector.startswith('opencode:v')
            or resolved.version != selector.removeprefix('opencode:v')
        ):
            raise BenchmarkError(
                'runtime_receipt_mismatch', 'Resolved runtime differs from its root receipt or frozen version.'
            )

    def _validate_selections(self) -> None:
        """Reject duplicate owners, foreign sequences and contradictory selected facts."""
        if [row.id for row in self.state.tasks] != list(self._tasks):
            raise BenchmarkError('invalid_state', 'State task queue differs from the immutable registry.')
        seen: set[str] = set()
        for row in self.state.tasks:
            registration = self.task(row.id)
            if row.ordinal != registration.ordinal or row.processing_status not in {
                'pending',
                'running',
                'completed',
                'retry_pending',
                'interrupted',
            }:
                raise BenchmarkError('invalid_state', 'Task ordinal or processing state is invalid.')
            for value, minimum in ((row.next_sequence, 1), (row.retry_index, 0), (row.retry_epoch, 0)):
                if type(value) is not int or value < minimum:
                    raise BenchmarkError('invalid_state', 'Attempt scheduling sequence or retry budget is invalid.')
            if row.attempt is None and row.processing_status != 'pending':
                raise BenchmarkError('invalid_state', 'Only pending tasks can have no reserved native attempt.')
            if row.attempt is None and any(
                value is not None for value in (row.start, row.seal, row.outcome, row.scoring, row.pricing)
            ):
                raise BenchmarkError('attempt_owner_mismatch', 'Selected facts require a registered native attempt.')
            attempts = _attempts(row)
            sequences = [item.attempt.sequence for item in attempts]
            if len(sequences) != row.next_sequence - 1 or any(
                sequence != ordinal for ordinal, sequence in enumerate(sequences, 1)
            ):
                raise BenchmarkError('invalid_state', 'Attempt history must contain every reserved sequence in order.')
            if row.attempt is None and attempts:
                raise BenchmarkError('attempt_owner_mismatch', 'Attempt history requires a current native selection.')
            for item in attempts:
                self._validate_attempt(item, seen)

    def _validate_attempt(self, selection: AttemptSelection, seen: set[str]) -> None:
        """Validate selected and historical native owners with their immutable references."""
        attempt = selection.attempt
        if attempt.id in seen:
            raise BenchmarkError(
                'attempt_owner_mismatch', 'Attempt UUID is reused across current or historical owners.'
            )
        seen.add(attempt.id)
        self._validate_start(selection)
        if selection.outcome is not None:
            if selection.seal is None:
                raise BenchmarkError('attempt_seal_missing', 'Final outcome requires its explicitly selected seal.')
            outcome = read_reference(self.directory, selection.outcome, Outcome, error_code='scoring_result_mismatch')
            if outcome.seal != selection.seal:
                raise BenchmarkError('attempt_seal_mismatch', 'Outcome references a different native seal.')
            native_seal = read_reference(self.directory, selection.seal, Seal)
            self._initial_score(selection, outcome, native_seal)
            left, right = outcome.task, native_seal.task
            if (
                left.final_attempt != right.final_attempt
                or left.setup != right.setup
                or left.execution != right.execution
                or left.changes != right.changes
                or left.telemetry != right.telemetry
                or left.infrastructure_error != right.infrastructure_error
            ):
                raise BenchmarkError('native_fact_mismatch', 'Outcome changes immutable native execution evidence.')
        for key, chosen in (('scoring', selection.scoring), ('pricing', selection.pricing)):
            if chosen is not None:
                self._selected_result(selection, key)
        for kind, chosen in (('outcome', selection.outcome), ('seal', selection.seal)):
            if chosen is not None:
                self._facts(selection, kind)

    def _initial_score(self, selection: AttemptSelection, outcome: Outcome, native_seal: Seal) -> ReportTaskScoring:
        """Bind initial host scoring to its exact sealed owner and result bytes."""
        receipt = outcome.scoring_result
        expected = (self.attempt_directory(selection.id, selection.attempt.sequence) / 'scoring.json').as_posix()
        if receipt.path != expected:
            raise BenchmarkError('scoring_result_missing', 'Outcome requires its owned initial scoring.json receipt.')
        if receipt not in outcome.artifact_receipts:
            raise BenchmarkError('scoring_receipt_missing', 'Outcome inventory does not own its initial host score.')
        result = read_reference(self.directory, receipt, ScoringEvidence, error_code='scoring_result_mismatch')
        if (
            result.schema_family != 'pr_similarity_os.scoring'
            or result.schema_version != 2
            or result.run_id != self.registry.id
            or result.task_id != selection.id
            or result.attempt_id != selection.attempt.id
            or result.seal != selection.seal
            or result.metric != native_seal.metric
        ):
            raise BenchmarkError('scoring_owner_mismatch', 'Initial scoring result differs from its sealed owner.')
        scoring = result.scoring
        if (
            scoring.status not in {'available', 'unavailable'}
            or scoring != outcome.task.scoring
            or result.error != outcome.scoring_error
        ):
            raise BenchmarkError('scoring_result_mismatch', 'Outcome scoring differs from its immutable host result.')
        return scoring

    def _facts(self, selection: AttemptSelection, kind: str) -> TaskDraft:
        """Bind native facts and every signed artifact to the selected task owner."""
        attempt = selection.attempt
        receipt = selection.seal if kind == 'seal' else selection.outcome
        assert receipt is not None
        cache_key = (selection.id, attempt.id, kind, receipt.path, receipt.sha256, receipt.size_bytes)
        if cache_key in self._facts_cache:
            return self._facts_cache[cache_key]
        value = (
            read_reference(self.directory, receipt, Seal)
            if kind == 'seal'
            else read_reference(self.directory, receipt, Outcome, error_code='scoring_result_mismatch')
        )
        if value.schema_family != 'pr_similarity_os.' + kind or value.schema_version != 2:
            raise BenchmarkError(
                'attempt_owner_mismatch', 'Execution fact has an unsupported schema family or version.'
            )
        expected = (self.attempt_directory(selection.id, attempt.sequence) / (kind + '.json')).as_posix()
        if receipt.path != expected or value.run_id != self.registry.id:
            raise BenchmarkError('attempt_owner_mismatch', 'Selected facts have an unexpected run or owner path.')
        if value.basket_sha256 != self.registry.baseline.basket.sha256:
            raise BenchmarkError('attempt_owner_mismatch', 'Execution facts belong to another frozen basket.')
        if value.config_identity_sha256 != self.registry.baseline.config.identity_sha256:
            raise BenchmarkError('attempt_owner_mismatch', 'Execution facts belong to another agent configuration.')
        if value.agent_input != self.registry.agent_input:
            raise BenchmarkError('attempt_owner_mismatch', 'Native fact has a foreign shared input.')
        validate_python_fact(
            self.directory,
            value,
            self.state.python_runtime,
            self.attempt_directory(selection.id, attempt.sequence).as_posix() + '/',
        )
        row = value.task
        if (
            row.id != selection.id
            or row.final_attempt is None
            or _attempt_identity(row.final_attempt) != _attempt_identity(attempt)
        ):
            raise BenchmarkError('attempt_owner_mismatch', 'Selected fact task/attempt identity differs.')
        if row.setup.status == 'running' or row.execution.status == 'running':
            raise BenchmarkError('attempt_not_terminal', 'Native evidence cannot be sealed while execution is active.')
        frozen = pending_task(self.task(row.id).basket_row)
        if not _same_task_identity(row, frozen):
            raise BenchmarkError('attempt_owner_mismatch', 'Execution fact differs from its frozen task.')
        paths = [item.path for item in value.artifact_receipts]
        if len(paths) != len(set(paths)):
            raise BenchmarkError('invalid_artifact_reference', 'Native fact duplicates an evidence receipt.')
        for item in value.artifact_receipts:
            prefix = self.attempt_directory(row.id, attempt.sequence).as_posix() + '/'
            shared = self.registry.agent_input
            allowed = [shared.config, *shared.resources]
            if self.state.python_runtime is not None:
                allowed.append(self.state.python_runtime)
            effective = row.artifacts.effective_config
            if effective.status == 'saved' and item.path == effective.path:
                if not re.fullmatch(r'agent/effective/[0-9a-f]{64}\.json', item.path):
                    raise BenchmarkError('attempt_owner_mismatch', 'Invalid shared effective config path.')
                if item.sha256 + '.json' != item.path.split('/')[-1]:
                    raise BenchmarkError('artifact_digest_mismatch', 'Effective filename differs from its digest.')
                allowed.append(item)
            if not item.path.startswith(prefix) and item not in allowed:
                raise BenchmarkError('attempt_owner_mismatch', 'Evidence receipt escapes its native attempt.')
            content = artifact_path(self.directory, item.path, must_exist=True).read_bytes()
            if len(content) != item.size_bytes or bytes_sha256(content) != item.sha256:
                raise BenchmarkError('artifact_digest_mismatch', 'Sealed attempt evidence is damaged.')
        self._facts_cache[cache_key] = row
        return row

    def _live_row(self, selection: StateTaskItem) -> TaskDraft:
        """Expose future locations and actual availability without inventing terminal results."""
        row = pending_task(self.task(selection.id).basket_row)
        attempt = selection.attempt
        if attempt is None:
            return row
        directory = self.attempt_directory(selection.id, attempt.sequence)
        row.final_attempt = copy.deepcopy(attempt)
        row.locations = {
            key: (directory / name).as_posix()
            for key, name in (
                ('agent_log', 'agent.log'),
                ('agent_events', 'events.jsonl'),
                ('runtime_log', 'runtime.log'),
                ('setup_log', 'setup.log'),
                ('outcome', 'outcome.json'),
                ('triplets_manifest', 'triplets/index.json'),
            )
        }
        available = {
            key: location
            for key, location in row.locations.items()
            if key != 'runtime_log' and artifact_path(self.directory, location).is_file()
        }
        row.artifacts.outcome = available.get('outcome')
        row.artifacts.triplets_manifest = available.get('triplets_manifest')
        row.artifacts.setup_log = available.get('setup_log')
        row.artifacts.agent_events = available.get('agent_events')
        row.artifacts.agent_log = available.get('agent_log')
        phase = selection.progress.phase
        if phase == 'running':
            row.execution.status = 'running'
        if phase == 'setup':
            row.setup.status = 'running'
        if selection.processing_status == 'interrupted':
            row.execution.status = 'unknown'
            row.setup.status = 'unknown'
            row.scoring = unavailable_scoring(
                'unavailable',
                ReportError(code='native_seal_missing', message='Native work stopped before evidence was sealed.'),
            )
        return row

    def _selected_result(self, selection: StateTaskItem | AttemptSelection, kind: str) -> ScoreItem | PriceItem | None:
        """Choose an exact operation item rather than a newest directory heuristic."""
        receipt = selection.scoring if kind == 'scoring' else selection.pricing
        if receipt is None:
            return None
        operation = validate_operation(
            self.directory, self.registry, receipt, 'rescore' if kind == 'scoring' else 'reprice'
        )
        if receipt.accepted_revision > self.state.revision:
            raise BenchmarkError('operation_commit_mismatch', 'Selected result is newer than committed state.')
        items: Sequence[ScoreItem | PriceItem] = (
            (operation.result.tasks or []) if kind == 'scoring' else (operation.result.attempts or [])
        )
        attempt = selection.attempt
        assert attempt is not None
        matches = [row for row in items if row.task_id == selection.id and row.attempt_id == attempt.id]
        if len(matches) != 1:
            raise BenchmarkError(
                'operation_owner_mismatch', 'Selected operation result has no unique task/attempt owner.'
            )
        return matches[0]

    def _effective_configs(self) -> list[EffectiveConfig]:
        """Project only configurations explicitly owned by selected native seals."""
        configs: list[EffectiveConfig] = []
        for selected in self.state.tasks:
            for attempt in _attempts(selected):
                if attempt.seal is None:
                    continue
                native = read_reference(self.directory, attempt.seal, Seal)
                effective = native.task.artifacts.effective_config
                if effective.status != 'saved':
                    continue
                expected = effective.path
                if not isinstance(expected, str) or not re.fullmatch(r'agent/effective/[0-9a-f]{64}\.json', expected):
                    raise BenchmarkError('inference_owner_mismatch', 'Effective config has an unexpected shared path.')
                receipts = {item.path: item for item in native.artifact_receipts}
                if expected not in receipts:
                    raise BenchmarkError(
                        'inference_owner_mismatch', 'Effective config is not in its selected native seal.'
                    )
                configs.append(
                    EffectiveConfig(
                        attempt_id=attempt.attempt.id,
                        source_ref=expected,
                        config=read_reference_json(self.directory, receipts[expected]),
                    )
                )
        return configs

    def _publication_fingerprint(self, request: PublishRequest) -> PublishFingerprint:
        """Validate the immutable HTTPS fingerprint even when its old payload is omitted."""
        value = request.fingerprint
        if value.model_extra:
            raise BenchmarkError('publication_fingerprint_invalid', 'Publication fingerprint fields are invalid.')
        parsed = urlsplit(value.server)
        if (
            parsed.scheme != 'https'
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ('', '/')
            or parsed.query
            or parsed.fragment
            or value.run_id != self.registry.id
            or sha256(value) != request.fingerprint_sha256
        ):
            raise BenchmarkError(
                'publication_fingerprint_invalid', 'Publication fingerprint has inconsistent ownership.'
            )
        for digest in (value.archive_sha256, value.report_sha256):
            if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
                raise BenchmarkError('publication_fingerprint_invalid', 'Publication content digest is invalid.')
        return value

    def _publication_confirmation(self, request: PublishRequest, receipt: PublishReceipt) -> None:
        """Require confirmed public metadata and server identity to match the frozen request."""
        if receipt.status != 'completed':
            if receipt.server_receipt is not None or receipt.public_metadata is not None:
                raise BenchmarkError(
                    'publication_receipt_invalid', 'Rejected publication cannot claim server confirmation.'
                )
            return
        confirmed = receipt.server_receipt
        fingerprint = self._publication_fingerprint(request)
        if (
            confirmed is None
            or confirmed.model_extra
            or confirmed.run_id != self.registry.id
            or confirmed.archive_sha256 != fingerprint.archive_sha256
            or confirmed.report_sha256 != fingerprint.report_sha256
            or confirmed.idempotency_key != request.id
            or not confirmed.author_login
        ):
            raise BenchmarkError(
                'publication_receipt_invalid', 'Server confirmation differs from the immutable request.'
            )
        metadata = receipt.public_metadata
        if metadata is None or metadata.inference != request.metadata.inference:
            raise BenchmarkError('publication_receipt_invalid', 'Public inference differs from the frozen config.')
        if (
            metadata.config_source != request.metadata.agent_input.config
            or type(metadata.config_supported) is not bool
            or metadata.benchmark != request.metadata.benchmark
        ):
            raise BenchmarkError('publication_receipt_invalid', 'Public native input or harness metadata differs.')
        tags, groups = metadata.tags, metadata.tag_groups
        if groups.get('setup') != tags:
            raise BenchmarkError('publication_receipt_invalid', 'Public Setup metadata is inconsistent.')
        generated = generated_groups(request.metadata.inference)
        if set(groups) != set(generated) | {'setup'} or any(
            groups.get(key) != value for key, value in generated.items()
        ):
            raise BenchmarkError('publication_receipt_invalid', 'Public generated model/effort group differs.')

    def _validate_publication(
        self, selected: StatePendingPublish | StateLastPublish, *, closed: bool
    ) -> PublishRequest:
        """Bind a publication journal to one immutable request, payload and optional receipt."""
        identifier = operation_identifier(selected.id)
        prefix = 'operations/publish/' + identifier + '/'
        if selected.request.path != prefix + 'request.json':
            raise BenchmarkError('publication_owner_mismatch', 'Publication request has a foreign operation path.')
        request = read_reference(self.directory, selected.request, PublishRequest)
        baseline = self.registry.baseline
        if (
            request.schema_family != 'pr_similarity_os.publish_request'
            or request.schema_version != 2
            or request.id != identifier
            or request.run_id != self.registry.id
            or request.basket_sha256 != baseline.basket.sha256
            or request.config_identity_sha256 != baseline.config.identity_sha256
            or request.idempotency_key != identifier
        ):
            raise BenchmarkError('publication_owner_mismatch', 'Publication belongs to different frozen inputs.')
        self._publication_fingerprint(request)
        payload = request.payload
        path = artifact_path(self.directory, payload.path)
        available = path.is_file()
        if (
            payload.model_extra
            or payload.size_bytes < 0
            or payload.path != prefix + 'payload.tar.gz'
            or payload.sha256 != request.fingerprint.archive_sha256
            or (available and reference(self.directory, payload.path) != payload)
            or (not available and not self.allow_omitted_publish_payloads)
        ):
            raise BenchmarkError('publication_payload_mismatch', 'Frozen publication payload differs from its receipt.')
        if closed:
            assert isinstance(selected, StateLastPublish)
            if selected.receipt.path != prefix + 'receipt.json':
                raise BenchmarkError('publication_owner_mismatch', 'Publication receipt has a foreign operation path.')
            receipt = read_reference(self.directory, selected.receipt, PublishReceipt)
            if (
                receipt.schema_family != 'pr_similarity_os.publish_receipt'
                or receipt.schema_version != 2
                or receipt.id != identifier
                or receipt.run_id != self.registry.id
                or receipt.request != selected.request
                or receipt.status not in ('completed', 'conflict', 'rejected')
            ):
                raise BenchmarkError(
                    'publication_owner_mismatch', 'Closed publication receipt has inconsistent ownership.'
                )
            self._publication_confirmation(request, receipt)
        return request

    def select_publication(self, identifier: str, receipt: ArtifactReference) -> Report:
        """Commit the unresolved exact upload before any authentication or network call."""
        with self._writer():
            selected = StatePendingPublish(id=identifier, request=copy.deepcopy(receipt))
            self._validate_publication(selected, closed=False)
            existing = self.state.pending_publish
            if existing is not None:
                if existing != selected:
                    raise BenchmarkError(
                        'publication_pending', 'Recover the unresolved publication before starting another.'
                    )
                return self.snapshot()
            state = copy.deepcopy(self.state)
            state.pending_publish = selected
            return self._publish_selection(state)

    def finish_publication(self, receipt: ArtifactReference) -> Report:
        """Select immutable confirmation and clear only its matching pending request."""
        with self._writer():
            pending = self.state.pending_publish
            if pending is None:
                raise BenchmarkError('publication_not_pending', 'No unresolved publication can accept this receipt.')
            selected = StateLastPublish(
                id=pending.id, request=copy.deepcopy(pending.request), receipt=copy.deepcopy(receipt)
            )
            self._validate_publication(selected, closed=True)
            state = copy.deepcopy(self.state)
            state.last_publish, state.pending_publish = selected, None
            return self._publish_selection(state)

    def last_operation(self, kind: str) -> OperationSelection | None:
        """Return the exact selected root operation, including empty result populations."""
        if kind not in ('rescore', 'reprice'):
            raise BenchmarkError('invalid_operation_kind', 'Only score and price operations have root selections.')
        selected = (
            self.state.selected_operations.rescore if kind == 'rescore' else self.state.selected_operations.reprice
        )
        return copy.deepcopy(selected)

    def read_artifact[T: WireModel](self, receipt: ArtifactReference, model: type[T]) -> T:
        """Read typed cost evidence after its exact byte identity has been checked."""
        return read_reference(self.directory, receipt, model)

    def price_result(self, selection: OperationSelection) -> OperationResult:
        """Expose only a verified selected pricing result to billing projection."""
        return validate_operation(self.directory, self.registry, selection, 'reprice').result

    def validate_usage(self, ledger: Usage) -> None:
        """Apply usage schema and owner checks without introducing alternate rules."""
        validate_usage(ledger)

    def _operation_metadata(self, kind: str) -> ReportLastRescore | ReportLastReprice | None:
        """Derive display provenance from the immutable selected operation receipt."""
        selected = self.last_operation(kind)
        if selected is None:
            return None
        receipt = read_reference(self.directory, selected.receipt, OperationReceipt)
        if kind == 'reprice':
            return ReportLastReprice(
                at=receipt.finished_at,
                invocation_id=receipt.invocation_id,
                calculation_id=selected.id,
                previous_report_sha256=receipt.input_report.sha256,
                backup_path=receipt.input_report.path,
                receipt_path=selected.receipt.path,
            )
        return ReportLastRescore(
            at=receipt.finished_at,
            invocation_id=receipt.invocation_id,
            previous_report_sha256=receipt.input_report.sha256,
            backup_path=receipt.input_report.path,
            receipt_path=selected.receipt.path,
        )

    def snapshot(self) -> Report:
        """Project saved timestamps and accepted immutable facts deterministically."""
        baseline = copy.deepcopy(self.registry.baseline)
        run = baseline.run
        run.status, run.finished_at, run.error = self.state.run.status, self.state.run.finished_at, self.state.run.error
        run.current_invocation_id = self.state.run.current_invocation_id
        run.revision, run.updated_at = self.state.revision, self.state.updated_at
        baseline.agent.resolved_runtime = copy.deepcopy(self.state.resolved_runtime)
        baseline.agent.python_runtime = copy.deepcopy(self.state.python_runtime)
        identity = _parse_model(
            ConfigIdentity,
            read_json(artifact_path(self.directory, 'config_identity.json', must_exist=True)),
            'run_input_mismatch',
        )
        rows: list[ReportTask] = []
        for selection in self.state.tasks:
            row = self._live_row(selection)
            locations = row.locations
            for kind, chosen in (('seal', selection.seal), ('outcome', selection.outcome)):
                if chosen is not None:
                    row = copy.deepcopy(self._facts(_selected(selection.id, selection), kind))
            row.processing_status = selection.processing_status
            row.final_attempt = copy.deepcopy(selection.attempt)
            row.locations = locations
            row.artifacts.outcome = None if selection.outcome is None else selection.outcome.path
            score = self._selected_result(selection, 'scoring')
            if score is not None:
                assert isinstance(score, ScoreItem)
                row.scoring = copy.deepcopy(score.scoring)
                if row.infrastructure_error is not None and row.infrastructure_error.stage == 'score':
                    row.infrastructure_error = None
            price = self._selected_result(selection, 'pricing')
            if price is not None:
                assert isinstance(price, PriceItem)
                row.telemetry.api_equivalent = copy.deepcopy(price.api_equivalent)
            rows.append(
                project_task(row, selection.ordinal, self.task(selection.id).path, copy.deepcopy(selection.progress))
            )
        last_rescore, last_reprice = self._operation_metadata('rescore'), self._operation_metadata('reprice')
        assert last_rescore is None or isinstance(last_rescore, ReportLastRescore)
        assert last_reprice is None or isinstance(last_reprice, ReportLastReprice)
        try:
            costs = build_cost_summary(self)
        except CostEvidenceError as error:
            raise BenchmarkError(error.code, error.message) from error
        return Report(
            schema_family=baseline.schema_family,
            schema_version=baseline.schema_version,
            run=run,
            basket=baseline.basket,
            agent=baseline.agent,
            config=baseline.config,
            metric=copy.deepcopy(self.state.metric),
            tasks=rows,
            summary=summary(rows),
            last_rescore=last_rescore,
            last_reprice=last_reprice,
            state=ReportState(path='_meta/state.json', sha256=self._disk_digest),
            costs=costs,
            agent_input=copy.deepcopy(self.registry.agent_input),
            inference=build_inference(identity, self._effective_configs()),
        )

    def _rebuild(self) -> Report:
        """Restore the derived snapshot without changing selection, revision or timestamps."""
        report = self.snapshot()
        validate_report(report)
        write_json(self.directory / 'report.json', report)
        return report

    def _commit(self, report: Report, *, selected_state: State | None = None) -> Report:
        """Commit scheduling references first, then publish their validated report view."""
        candidate = copy.deepcopy(self.state if selected_state is None else selected_state)
        candidate.revision += 1
        candidate.updated_at = utc_now()
        candidate.run = StateRun(
            status=report.run.status,
            finished_at=report.run.finished_at,
            error=report.run.error,
            current_invocation_id=report.run.current_invocation_id,
        )
        candidate.resolved_runtime = copy.deepcopy(report.agent.resolved_runtime)
        candidate.python_runtime = copy.deepcopy(report.agent.python_runtime)
        candidate.metric = copy.deepcopy(report.metric)
        for record, row in zip(candidate.tasks, report.tasks, strict=True):
            if record.id != row.id:
                raise BenchmarkError('state_owner_mismatch', 'Cannot reorder or substitute a frozen task queue.')
            if record.processing_status == 'completed' and row.processing_status != 'completed':
                raise BenchmarkError(
                    'terminal_task_closed', 'Completed native tasks cannot be reopened by report edits.'
                )
            status = row.processing_status
            if status not in ('pending', 'running', 'completed', 'retry_pending', 'interrupted'):
                raise BenchmarkError(
                    'invalid_state', 'Task processing state is invalid for the authoritative scheduler.'
                )
            record.processing_status = status
            if status in {'completed', 'interrupted', 'retry_pending'}:
                phase = status.replace('_', ' ')
                if record.progress.phase != phase:
                    record.progress = ReportTaskProgress(phase=phase, updated_at=candidate.updated_at)
            previous, current = record.attempt, row.final_attempt
            if (previous is None) != (current is None) or (
                previous is not None
                and current is not None
                and _attempt_identity(previous) != _attempt_identity(current)
            ):
                raise BenchmarkError('attempt_owner_mismatch', 'Only reservation may change native attempt ownership.')
            record.attempt = copy.deepcopy(current)
        proposed = RunStore(self.directory, self.registry, candidate)
        proposed._facts_cache = self._facts_cache
        proposed.validate()
        projected = proposed.snapshot()
        validate_report(projected)
        write_json(self.directory / '_meta/state.json', candidate)
        self.state = candidate
        self._disk_digest = bytes_sha256(document_bytes(candidate))
        write_json(self.directory / 'report.json', projected)
        return projected

    @contextmanager
    def _writer(self) -> Iterator[None]:
        """Reuse an active coordinator lease or acquire a standalone operation lock."""
        if self.lease is not None:
            if not self.lease.active or self.lease.directory != self.directory.resolve():
                raise BenchmarkError(
                    'writer_lease_expired', 'Run writer lease is expired or belongs to another directory.'
                )
            self._check_revision()
            yield
        else:
            with run_lock(self.directory):
                self._check_revision()
                yield

    def _check_revision(self) -> None:
        """Prevent a stale independently opened store from losing another writer's update."""
        content = artifact_path(self.directory, '_meta/state.json', must_exist=True).read_bytes()
        if bytes_sha256(content) != self._disk_digest:
            raise BenchmarkError('stale_run_state', 'Run state changed since this writer opened it; reopen the run.')

    def commit(self, report: Report) -> Report:
        """Commit under one writer lock, rejecting independently stale state snapshots."""
        with self._writer():
            return self._commit(report)

    def rebuild(self) -> Report:
        """Write the deterministic report under the same state-owner boundary."""
        with self._writer():
            return self._rebuild()

    def reserve(self, task_id: str, attempt: ReportAttempt) -> Report:
        """Persist unique start ownership and sequence before an executor can spawn."""
        with self._writer():
            state = copy.deepcopy(self.state)
            selected = next(row for row in state.tasks if row.id == task_id)
            if selected.processing_status not in ('pending', 'retry_pending', 'interrupted'):
                raise BenchmarkError(
                    'attempt_not_reservable', 'Active or completed tasks cannot start a new native attempt.'
                )
            if selected.seal is not None and selected.outcome is None:
                raise BenchmarkError(
                    'host_recovery_required', 'Sealed evidence requires host recovery, not native replay.'
                )
            if attempt.sequence != selected.next_sequence:
                raise BenchmarkError('attempt_sequence_conflict', 'Native attempt sequence was already reserved.')
            if any(row.attempt is not None and row.attempt.id == attempt.id for row in self.state.tasks):
                raise BenchmarkError('attempt_owner_mismatch', 'Native attempt UUID was already registered.')
            directory = self.attempt_directory(task_id, attempt.sequence)
            target = self.directory / directory
            artifact_path(self.directory, (directory / 'start.json').as_posix())
            if target.exists():
                raise BenchmarkError(
                    'uncommitted_reservation', 'Resume must recover the existing reservation before new work.'
                )
            baseline = self.registry.baseline
            start = Start(
                schema_family='pr_similarity_os.start',
                schema_version=2,
                run_id=self.registry.id,
                task_id=task_id,
                attempt=copy.deepcopy(attempt),
                basket_sha256=baseline.basket.sha256,
                config_identity_sha256=baseline.config.identity_sha256,
            )
            self._write_start(target, start)
            self._reserve_selection(
                selected, attempt, reference(self.directory, (directory / 'start.json').as_posix()), 'running'
            )
            return self._publish_selection(state)

    def _write_start(self, target: Path, start: Start) -> None:
        """Publish a complete start directory so a crash cannot strand an empty reservation."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.reservation-', dir=target.parent) as temporary:
            staged = Path(temporary)
            write_json(staged / 'start.json', start)
            os.rename(staged, target)
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _reserve_selection(
        self,
        selected: StateTaskItem,
        attempt: ReportAttempt,
        start: ArtifactReference,
        status: Literal['running', 'interrupted'],
    ) -> None:
        """Consume a reserved sequence while preserving all earlier native owners."""
        if selected.attempt is not None:
            selected.history.append(
                StateTaskItemHistoryItem(
                    attempt=copy.deepcopy(selected.attempt),
                    start=copy.deepcopy(selected.start),
                    seal=copy.deepcopy(selected.seal),
                    outcome=copy.deepcopy(selected.outcome),
                    scoring=copy.deepcopy(selected.scoring),
                    pricing=copy.deepcopy(selected.pricing),
                )
            )
        selected.attempt = copy.deepcopy(attempt)
        selected.next_sequence, selected.retry_index = attempt.sequence + 1, attempt.retry_index
        selected.processing_status = status
        selected.progress = ReportTaskProgress(phase='preparing', updated_at=utc_now())
        selected.start, selected.seal, selected.outcome = start, None, None
        selected.scoring, selected.pricing = None, None

    def _publish_selection(self, state: State) -> Report:
        """Validate a copied selection before replacing the authoritative state."""
        proposed = RunStore(self.directory, self.registry, state)
        proposed._facts_cache = self._facts_cache
        proposed.validate()
        return self._commit(proposed.snapshot(), selected_state=state)

    def prepare_invocation(self, invocation_id: str, *, resumed: bool) -> Report:
        """Record a new command and reset only unfinished native budgets in a new epoch."""
        with self._writer():
            state = copy.deepcopy(self.state)
            state.run.current_invocation_id = operation_identifier(invocation_id)
            if resumed:
                if state.run.status in ('running', 'stopping'):
                    state.run.status = 'interrupted'
                for row in state.tasks:
                    if row.processing_status != 'completed':
                        row.retry_epoch += 1
                        row.retry_index = 0
            return self._publish_selection(state)

    def recover_reservations(self) -> list[ReservationNotice]:
        """Consume intact uncommitted starts without issuing native work or guessing results.

        Only the next registered sequence may be adopted. A missing or corrupt
        authoritative state must be restored separately; this is not state reconstruction.

        Returns:
            Native owners recovered as interrupted reservations, for coordinator notices.

        Raises:
            BenchmarkError: If start ownership is corrupt or its state cannot commit.

        """
        with self._writer():
            state = copy.deepcopy(self.state)
            recovered: list[ReservationNotice] = []
            for selected in state.tasks:
                relative = self.attempt_directory(selected.id, selected.next_sequence) / 'start.json'
                path = artifact_path(self.directory, relative.as_posix())
                if not path.is_file():
                    continue
                if selected.processing_status not in ('pending', 'retry_pending', 'interrupted'):
                    raise BenchmarkError(
                        'unexpected_reservation', 'An active or completed task has an uncommitted start.'
                    )
                start = _parse_model(Start, read_json(path), 'attempt_start_mismatch')
                attempt = start.attempt
                if attempt.sequence != selected.next_sequence or attempt.finished_at is not None:
                    raise BenchmarkError(
                        'attempt_start_mismatch', 'Uncommitted start has an unexpected native sequence.'
                    )
                self._reserve_selection(
                    selected, attempt, reference(self.directory, relative.as_posix()), 'interrupted'
                )
                recovered.append(
                    ReservationNotice(task_id=selected.id, attempt_id=attempt.id, sequence=attempt.sequence)
                )
            if recovered:
                self._publish_selection(state)
            return recovered

    def accept_fact(
        self,
        task_id: str,
        kind: str,
        receipt: ArtifactReference,
        *,
        processing_status: Literal['pending', 'running', 'completed', 'retry_pending', 'interrupted'] | None = None,
    ) -> Report:
        """Select an explicitly closed native fact without discovering results from filenames."""
        with self._writer():
            if kind not in ('seal', 'outcome'):
                raise BenchmarkError('invalid_fact_kind', 'Only native seals and outcomes can be accepted.')
            state = copy.deepcopy(self.state)
            selected = next(row for row in state.tasks if row.id == task_id)
            if selected.attempt is None:
                raise BenchmarkError('attempt_start_missing', 'Cannot select evidence before native reservation.')
            existing = selected.seal if kind == 'seal' else selected.outcome
            if existing is not None and existing != receipt:
                raise BenchmarkError('immutable_fact_conflict', 'An accepted native fact cannot be replaced.')
            if kind == 'outcome' and selected.seal is None:
                raise BenchmarkError('attempt_seal_missing', 'Final outcome requires an accepted native seal.')
            if selected.processing_status == 'completed' and processing_status not in (None, 'completed'):
                raise BenchmarkError('terminal_task_closed', 'Accepted native facts cannot reopen a completed task.')
            if kind == 'seal':
                selected.seal = copy.deepcopy(receipt)
                fact: Seal | Outcome = read_reference(self.directory, receipt, Seal)
            else:
                selected.outcome = copy.deepcopy(receipt)
                fact = read_reference(self.directory, receipt, Outcome, error_code='scoring_result_mismatch')
                if fact.seal != selected.seal:
                    raise BenchmarkError('attempt_seal_mismatch', 'Final outcome references a different native seal.')
            proposed = RunStore(self.directory, self.registry, state)
            proposed._facts_cache = self._facts_cache
            selected.attempt = copy.deepcopy(fact.task.final_attempt)
            if processing_status is not None:
                selected.processing_status = processing_status
            proposed.validate()
            return self._commit(proposed.snapshot(), selected_state=state)

    def observe(self, task_id: str, phase: str, updated_at: str) -> Report:
        """Publish a bounded phase observation without treating it as an execution result."""
        with self._writer():
            state = copy.deepcopy(self.state)
            selected = next(row for row in state.tasks if row.id == task_id)
            if selected.attempt is None:
                raise BenchmarkError('phase_owner_mismatch', 'Cannot observe a task before its attempt is reserved.')
            selected.progress = ReportTaskProgress(phase=phase, updated_at=updated_at)
            proposed = RunStore(self.directory, self.registry, state)
            proposed._facts_cache = self._facts_cache
            return self._commit(proposed.snapshot(), selected_state=state)

    def _validate_start(self, selection: AttemptSelection) -> None:
        """Require a persisted start receipt for every reserved native owner."""
        if selection.start is None:
            raise BenchmarkError('attempt_start_missing', 'Reserved attempt has no durable start receipt.')
        start = read_reference(self.directory, selection.start, Start)
        attempt = selection.attempt
        expected = (self.attempt_directory(selection.id, attempt.sequence) / 'start.json').as_posix()
        baseline = self.registry.baseline
        if (
            selection.start.path != expected
            or start.schema_family != 'pr_similarity_os.start'
            or start.schema_version != 2
            or start.run_id != self.registry.id
            or start.task_id != selection.id
            or start.basket_sha256 != baseline.basket.sha256
            or start.config_identity_sha256 != baseline.config.identity_sha256
        ):
            raise BenchmarkError('attempt_start_mismatch', 'Start receipt has incompatible task/run inputs.')
        if _attempt_identity(start.attempt) != _attempt_identity(attempt):
            raise BenchmarkError('attempt_start_mismatch', 'Start receipt differs from the registered native attempt.')

    def prepare_operation(
        self,
        kind: str,
        items: Sequence[ScoreItem | PriceItem],
        *,
        invocation_id: str,
        metric: ReportMetric | None = None,
        operation_id: str | None = None,
        extra: OperationDetails | None = None,
        artifacts: dict[str, bytes] | None = None,
        preallocated: bool = False,
        status: str = 'completed',
    ) -> OperationSelection:
        """Stage typed immutable offline results without changing accepted selections."""
        with self._writer():
            if kind not in ('rescore', 'reprice') or self.state.run.status in ('running', 'stopping'):
                raise BenchmarkError(
                    'operation_not_allowed', 'Offline results require a stopped run and a supported kind.'
                )
            if status not in ('completed', 'completed_with_gaps', 'no_op', 'failed'):
                raise BenchmarkError(
                    'invalid_operation_status', 'Operation receipt has an unsupported completion status.'
                )
            identifier = operation_identifier(str(uuid.uuid4()) if operation_id is None else operation_id)
            relative = Path('operations') / kind / identifier
            artifact_path(self.directory, (relative / 'result.json').as_posix())
            directory = self.directory / relative
            self._allocate_operation(directory, identifier, kind, preallocated=preallocated)
            source = artifact_path(self.directory, '_meta/state.json', must_exist=True).read_bytes()
            atomic_write(directory / 'input_state.json', source)
            write_json(directory / 'input_report.json', self.snapshot())
            baseline = self.registry.baseline
            if kind == 'rescore':
                scores = [item for item in items if isinstance(item, ScoreItem)]
                if len(scores) != len(items):
                    raise BenchmarkError('invalid_operation_result', 'Rescore requires typed scoring items.')
                result = OperationResult(
                    schema_family='pr_similarity_os.operation_result',
                    schema_version=2,
                    id=identifier,
                    kind=kind,
                    run_id=self.registry.id,
                    basket_sha256=baseline.basket.sha256,
                    config_identity_sha256=baseline.config.identity_sha256,
                    metric=metric,
                    tasks=scores,
                )
            else:
                prices = [item for item in items if isinstance(item, PriceItem)]
                if len(prices) != len(items):
                    raise BenchmarkError('invalid_operation_result', 'Reprice requires typed pricing items.')
                result = OperationResult(
                    schema_family='pr_similarity_os.operation_result',
                    schema_version=2,
                    id=identifier,
                    kind=kind,
                    run_id=self.registry.id,
                    basket_sha256=baseline.basket.sha256,
                    config_identity_sha256=baseline.config.identity_sha256,
                    metric=metric,
                    attempts=prices,
                )
            for name, content in (artifacts or {}).items():
                if name in ('input_state.json', 'input_report.json', 'result.json', 'receipt.json'):
                    raise BenchmarkError(
                        'operation_artifact_conflict', 'Additional evidence cannot replace operation facts.'
                    )
                atomic_write(artifact_path(directory, name), content)
            write_json(directory / 'result.json', result)
            result_ref = reference(self.directory, (relative / 'result.json').as_posix())
            receipt = OperationReceipt(
                schema_family='pr_similarity_os.operation_receipt',
                schema_version=2,
                id=identifier,
                kind=kind,
                run_id=self.registry.id,
                basket_sha256=baseline.basket.sha256,
                config_identity_sha256=baseline.config.identity_sha256,
                status=status,
                invocation_id=invocation_id,
                finished_at=utc_now(),
                result=result_ref,
                input_state=reference(self.directory, (relative / 'input_state.json').as_posix()),
                input_report=reference(self.directory, (relative / 'input_report.json').as_posix()),
                details=OperationDetails() if extra is None else extra,
            )
            write_json(directory / 'receipt.json', receipt)
            selection = OperationSelection(
                id=identifier,
                kind=kind,
                accepted_revision=self.state.revision + 1,
                result=result_ref,
                receipt=reference(self.directory, (relative / 'receipt.json').as_posix()),
            )
            if status in ('completed', 'completed_with_gaps'):
                validate_operation(self.directory, self.registry, selection, kind)
            return selection

    def _allocate_operation(self, directory: Path, identifier: str, kind: str, *, preallocated: bool) -> None:
        """Complete only a declared owned pricing input directory, never reuse finished facts."""
        if not preallocated:
            directory.mkdir(parents=True, exist_ok=False)
            return
        if kind != 'reprice':
            raise BenchmarkError('invalid_operation_kind', 'Only frozen pricing inputs may preallocate an operation.')
        request = _parse_model(
            PricingRequest,
            read_json(artifact_path(directory, 'request.json', must_exist=True)),
            'operation_owner_mismatch',
        )
        baseline = self.registry.baseline
        if (
            request.schema_family != 'pr_similarity_os.pricing_request'
            or type(request.schema_version) is not int
            or request.schema_version != 2
            or request.id != identifier
            or request.kind != kind
            or request.run_id != self.registry.id
            or request.basket_sha256 != baseline.basket.sha256
            or request.config_identity_sha256 != baseline.config.identity_sha256
        ):
            raise BenchmarkError('operation_owner_mismatch', 'Preallocated pricing inputs have a foreign owner.')
        for name in ('input_state.json', 'input_report.json', 'result.json', 'receipt.json'):
            path = artifact_path(directory, name)
            if path.exists():
                raise BenchmarkError('immutable_operation_conflict', 'Operation facts already exist; preserve them.')

    def accept_operation(self, selection: OperationSelection) -> Report:
        """Commit only a validated result computed from the exact current state."""
        with self._writer():
            kind = selection.kind
            operation = validate_operation(self.directory, self.registry, selection, kind)
            if operation.receipt.input_state.sha256 != self._disk_digest:
                raise BenchmarkError(
                    'operation_inputs_stale', 'Offline result was computed from a different state revision.'
                )
            original = self.snapshot()
            candidate = RunStore(
                self.directory,
                self.registry,
                copy.deepcopy(self.state),
                lease=self.lease,
                state_digest=self._disk_digest,
            )
            candidate._facts_cache = self._facts_cache
            items: Sequence[ScoreItem | PriceItem] = (
                (operation.result.tasks or []) if kind == 'rescore' else (operation.result.attempts or [])
            )
            keyed = {(row.task_id, row.attempt_id) for row in items}
            for row in candidate.state.tasks:
                attempts: Sequence[StateTaskItemHistoryItem | StateTaskItem] = (
                    [*row.history, row] if kind == 'reprice' else [row]
                )
                for item in attempts:
                    attempt = item.attempt
                    if attempt is not None and (row.id, attempt.id) in keyed:
                        if kind == 'rescore':
                            item.scoring = copy.deepcopy(selection)
                        else:
                            item.pricing = copy.deepcopy(selection)
            if kind == 'rescore':
                candidate.state.selected_operations.rescore = copy.deepcopy(selection)
                assert operation.result.metric is not None
                original.metric = copy.deepcopy(operation.result.metric)
            else:
                candidate.state.selected_operations.reprice = copy.deepcopy(selection)
            original.run.current_invocation_id = operation.receipt.invocation_id
            previous_digest = candidate._disk_digest
            try:
                return candidate._commit(original)
            finally:
                if candidate._disk_digest != previous_digest:
                    self.state, self._disk_digest = candidate.state, candidate._disk_digest
