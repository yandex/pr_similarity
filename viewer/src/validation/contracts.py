"""Package-local consumer schemas and arithmetic/reference invariants."""

import math
import re
from collections.abc import Callable, Mapping

from viewer.errors import ViewerError
from viewer.models import (
    ArchiveManifest,
    Basket,
    JsonObject,
    Report,
    Run,
    State,
    WireModel,
)
from viewer.models.evidence import (
    ArtifactReceipt,
    ConfigIdentity,
    InferenceCaptureSeal,
    InferenceSeal,
    InferenceStart,
    InferenceState,
    InferenceTaskFacts,
    NativeTask,
    OperationReceipt,
    Outcome,
    PublishReceipt,
    PublishRequest,
    Receipt,
    RuntimeReceipt,
    ScoringEvidence,
    Seal,
    Start,
    TripletEntry,
    TripletIndex,
    parse_evidence,
)
from viewer.models.python_runtime import PythonReceipt
from viewer.models.report import (
    ReportAgentInput,
    ReportAttempt,
    ReportInferenceEffectiveAttemptItem,
    ReportTask,
    ReportTaskScoring,
)
from viewer.models.run import RunTaskItem
from viewer.models.state import (
    StateAttempt,
    StateLastPublish,
    StatePendingPublish,
    StateTaskItem,
    StateTaskItemHistoryItem,
)
from viewer.presentation.cost_summary import CostEvidenceError, build_cost_summary
from viewer.presentation.inference import build_inference, public_inference
from viewer.util import digest, json_bytes, parse_json, require_object, safe_path
from viewer.validation.agent_input import validate_agent_input
from viewer.validation.basket import validate_basket, validate_basket_report
from viewer.validation.operations import OperationStore
from viewer.validation.python_evidence import validate_python_attempt
from viewer.validation.schema import validate_document as validate_document
from viewer.validation.schema import validator as validator
from viewer.validation.triplet_paths import triplet_path

__all__ = ['require', 'validate_document', 'validate_identity', 'validate_report', 'validator']


def require(condition: object, code: str = 'invalid_contract') -> None:
    """Reject inconsistent public artifacts with a safe stable error code."""
    if not condition:
        raise ViewerError(code, 'Uploaded documents or artifacts are inconsistent.')


def validate_report(report: object) -> Report:
    """Validate a supported report contract, public paths and aggregate arithmetic."""
    validate_document('report', report)
    report = Report.model_validate(report.to_wire() if isinstance(report, Report) else report)
    tasks = report.tasks
    require(report.run.status not in ('running', 'stopping'))
    require(len(tasks) == report.basket.total_tasks == report.summary.total_tasks)
    require(len({t.id for t in tasks}) == len(tasks))
    counts = {'total_tasks': len(tasks)}
    for state, name in [
        ('completed', 'completed_tasks'),
        ('pending', 'pending_tasks'),
        ('running', 'active_tasks'),
        ('retry_pending', 'retry_pending_tasks'),
        ('interrupted', 'interrupted_tasks'),
    ]:
        counts[name] = sum(t.processing_status == state for t in tasks)
    counts['infrastructure_error_tasks'] = sum(t.infrastructure_error is not None for t in tasks)
    for state in ('timeout', 'error', 'cancelled'):
        counts[state + '_tasks'] = sum(t.execution.status == state for t in tasks)
    for state in ('nonempty', 'empty', 'unknown'):
        counts[state + '_diff_tasks'] = sum(t.changes.status == state for t in tasks)
    counts['scored_tasks'] = sum(t.scoring.status == 'available' for t in tasks)
    require(counts == report.summary.to_wire())
    for task in tasks:
        _validate_scores(task)
    return report


def _validate_scores(task: ReportTask) -> None:
    """Check role references and recompute reported score counts and aggregates."""
    scoring = task.scoring
    if scoring.status != 'available':
        return
    assert scoring.counts is not None and scoring.Mf_mlp is not None and scoring.LCf_mlp is not None
    rows = scoring.files
    if rows:
        prefix = _triplet_prefix(task)
        for row in rows:
            original = _source_path(row.path)
            ordinal = None
            for role, reference, exists in (
                ('base', row.base, row.exists.base),
                ('human', row.human, row.exists.human),
                ('ai', row.ai, row.exists.ai),
            ):
                if not exists:
                    require(reference is None, 'invalid_public_artifact')
                    continue
                require(isinstance(reference, str) and reference.startswith(prefix), 'invalid_public_artifact')
                assert isinstance(reference, str)
                relative = reference[len(prefix) :]
                number = relative.split('/', 1)[0]
                require(re.fullmatch(r'[0-9]{3,}', number) is not None, 'invalid_public_artifact')
                try:
                    expected = prefix + triplet_path(int(number), original, role)
                except ValueError as error:
                    raise ViewerError('invalid_public_artifact', 'Invalid triplet variant reference.') from error
                require(reference == expected and (ordinal is None or ordinal == number), 'invalid_public_artifact')
                ordinal = number
    paths = [safe_path(f.path) for f in rows]
    require(paths == sorted(set(paths)))
    totals: dict[str, int | float] = dict.fromkeys(
        [
            'human_files',
            'agent_files',
            'matched_files',
            'missed_files',
            'extra_files',
            'human_loc',
            'agent_loc',
            'loc_weight',
        ],
        0,
    )
    for row in rows:
        h, a = row.changed_human, row.changed_agent
        require(h or a)
        require(row.loc_weight == (max(1, row.human_loc) if h else 0))
        for k, v in [
            ('human_files', h),
            ('agent_files', a),
            ('matched_files', h and a),
            ('missed_files', h and not a),
            ('extra_files', a and not h),
        ]:
            totals[k] += int(v)
        for k, value in (('human_loc', row.human_loc), ('agent_loc', row.agent_loc), ('loc_weight', row.loc_weight)):
            totals[k] += value
    require(totals == scoring.counts.to_wire())
    mean = sum(f.mlp for f in rows) / len(rows) if rows else 1
    weighted = sum(f.mlp * f.loc_weight for f in rows) / totals['loc_weight'] if totals['loc_weight'] else mean
    require(
        math.isclose(mean, scoring.Mf_mlp, abs_tol=1e-12) and math.isclose(weighted, scoring.LCf_mlp, abs_tol=1e-12)
    )


def validate_identity(
    report: object,
    basket: object,
    basket_bytes: bytes,
    manifest: object,
    inventory: Mapping[str, Receipt],
    read_file: Callable[[str], bytes],
) -> tuple[Report, Basket, ArchiveManifest]:
    """Bind untrusted archive documents to frozen input, native evidence and exact role receipts."""
    report = validate_report(report)
    basket = validate_basket(basket)
    validate_basket_report(basket, report)
    validate_document('archive', manifest)
    manifest = ArchiveManifest.model_validate(manifest.to_wire() if isinstance(manifest, ArchiveManifest) else manifest)
    require(manifest.schema_version == report.schema_version, 'invalid_state')
    require(manifest.complete and not any(x.required for x in manifest.missing_artifacts))
    require(manifest.run_id == report.run.id and manifest.report_revision == report.run.revision)
    require(manifest.report_sha256 == inventory['report.json'].sha256)
    require(report.basket.sha256 == digest(basket_bytes))
    for name in (
        'run.json',
        '_meta/state.json',
        '_meta/runtime.json',
        '_meta/sandbox.json',
        'basket.json',
        'config_identity.json',
    ):
        _reference(name, inventory)
    _validate_registry_state(report, basket, inventory, read_file)
    identity_raw = require_object(parse_json(read_file('config_identity.json')), 'invalid_inference')
    require(digest(json_bytes(identity_raw)) == report.config.identity_sha256)
    validate_agent_input(report.agent_input, identity_raw, inventory, read_file)
    identity = parse_evidence(ConfigIdentity, identity_raw, 'invalid_agent_input')
    validate_inference_evidence(report, identity, inventory, read_file)
    _validate_runtime_receipt(report, inventory, read_file)
    _reference(report.config.identity_path, inventory)
    for task in report.tasks:
        _validate_archive_task(task, report, inventory, read_file)
    for entry in (report.last_rescore, report.last_reprice):
        if entry is not None:
            _reference(entry.receipt_path, inventory)
            _reference(entry.backup_path, inventory)
    return report, basket, manifest


def _validate_runtime_receipt(
    report: Report, inventory: Mapping[str, Receipt], read_file: Callable[[str], bytes]
) -> None:
    """Bind the projected pinned runtime to its explicit host provisioning receipt."""
    runtime = report.agent.resolved_runtime
    require(runtime is not None and runtime.receipt == '_meta/runtime.json', 'invalid_state')
    assert runtime is not None
    receipt = _document('_meta/runtime.json', inventory, read_file, RuntimeReceipt)
    require(
        (runtime.asset_name, runtime.platform, runtime.archive_sha256, runtime.binary_sha256)
        == (receipt.asset_name, receipt.platform, receipt.archive_sha256, receipt.binary_sha256),
        'invalid_state',
    )
    require(runtime.version == receipt.actual_version, 'invalid_state')
    require(report.agent.runtime == 'opencode:v' + runtime.version, 'invalid_state')
    if report.schema_version == 2:
        python = report.agent.python_runtime
        require(python is not None, 'invalid_state')
        assert python is not None
        _receipt(python, inventory, '_meta/python.json')
        python_receipt = _document('_meta/python.json', inventory, read_file, PythonReceipt)
        require(python_receipt.preflight == 'passed', 'invalid_state')


def _receipt(value: WireModel | None, inventory: Mapping[str, Receipt], path: str) -> None:
    """Require one exact immutable receipt instead of selecting a newest directory."""
    require(value is not None, 'invalid_state')
    assert value is not None
    require(set(value.to_wire()) == {'path', 'size_bytes', 'sha256'}, 'invalid_state')
    receipt = parse_evidence(ArtifactReceipt, value.to_wire(), 'invalid_state')
    require(type(receipt.size_bytes) is int, 'invalid_state')
    require(
        receipt.path == path and path in inventory and inventory[path].to_wire() == receipt.to_wire(), 'invalid_state'
    )


def _document[T: WireModel](
    path: str,
    inventory: Mapping[str, Receipt],
    read_file: Callable[[str], bytes],
    model: type[T],
    code: str = 'invalid_state',
) -> T:
    """Read typed facts after checking exact bytes and authoritative schemas."""
    return parse_evidence(model, _document_json(path, inventory, read_file, code), code)


def _document_json(
    path: str, inventory: Mapping[str, Receipt], read_file: Callable[[str], bytes], code: str = 'invalid_state'
) -> JsonObject:
    """Verify bytes before entering a typed document parsing boundary."""
    if code == 'invalid_inference':
        require(path in inventory, code)
    else:
        _reference(path, inventory)
    content = read_file(path)
    require(len(content) == inventory[path].size_bytes and digest(content) == inventory[path].sha256, code)
    return require_object(parse_json(content), code)


def _same(left: WireModel | None, right: WireModel | None) -> bool:
    """Compare equivalent wire contracts whose declared model classes differ."""
    return (left.to_wire() if left is not None else None) == (right.to_wire() if right is not None else None)


def _validate_registry_state(
    report: Report, basket: Basket, inventory: Mapping[str, Receipt], read_file: Callable[[str], bytes]
) -> None:
    """Bind report rows to the direct v1 registry/state selection, including history."""
    registry_raw = _document_json('run.json', inventory, read_file)
    state_raw = _document_json('_meta/state.json', inventory, read_file)
    validate_document('run', registry_raw)
    validate_document('state', state_raw)
    registry = Run.model_validate(registry_raw)
    state = State.model_validate(state_raw)
    require(
        registry.schema_family == 'pr_similarity_os.run'
        and registry.schema_version == report.schema_version
        and registry.id == report.run.id
        and isinstance(registry.tasks, list),
        'invalid_state',
    )
    require(
        state.schema_family == 'pr_similarity_os.state'
        and state.schema_version == report.schema_version
        and state.run_id == registry.id
        and state.registry_sha256 == inventory['run.json'].sha256
        and state.revision == report.run.revision
        and isinstance(state.tasks, list),
        'invalid_state',
    )
    require(
        report.state.to_wire()
        == {
            'path': '_meta/state.json',
            'sha256': inventory['_meta/state.json'].sha256,
        },
        'invalid_state',
    )
    require(_same(registry.agent_input, report.agent_input), 'invalid_agent_input')
    baseline = registry.baseline
    require(baseline.schema_version == report.schema_version, 'invalid_state')
    require(
        baseline.basket.sha256 == report.basket.sha256
        and baseline.basket.path == 'basket.json'
        and baseline.config.identity_sha256 == report.config.identity_sha256
        and baseline.config.identity_path == 'config_identity.json',
        'invalid_state',
    )
    entries = registry.tasks
    require(
        [entry.id for entry in entries] == [task.id for task in basket.tasks],
        'invalid_state',
    )
    require(
        [entry.id for entry in entries] == [task.id for task in report.tasks],
        'invalid_state',
    )
    require(
        [entry.id for entry in state.tasks] == [entry.id for entry in entries],
        'invalid_state',
    )
    _validate_projection_header(report, registry, state)
    store = OperationStore(registry, state, inventory, read_file, validate_report)
    for kind, selection, projected in (
        ('rescore', state.selected_operations.rescore, report.last_rescore),
        ('reprice', state.selected_operations.reprice, report.last_reprice),
    ):
        if selection is None:
            require(projected is None, 'invalid_state')
            if kind == 'rescore':
                require(_same(state.metric, registry.baseline.metric), 'invalid_state')
        else:
            result = store.operation(selection, kind)
            if kind == 'rescore':
                require(_same(result.metric, state.metric), 'invalid_state')
            receipt = store.read_artifact(selection.receipt, OperationReceipt)
            metadata = {
                'at': receipt.finished_at,
                'invocation_id': receipt.invocation_id,
                'previous_report_sha256': receipt.input_report.sha256,
                'backup_path': receipt.input_report.path,
                'receipt_path': selection.receipt.path,
            }
            if kind == 'reprice':
                metadata['calculation_id'] = selection.id
            require(projected is not None and projected.to_wire() == metadata, 'invalid_state')
    for registration, selected, task in zip(entries, state.tasks, report.tasks, strict=True):
        _validate_registered_task(registration, selected, task, basket, store)
    try:
        costs = build_cost_summary(store)
    except CostEvidenceError as error:
        raise ViewerError(error.code, error.message) from error
    require(report.costs == costs, 'invalid_costs')
    for selected, closed in ((state.pending_publish, False), (state.last_publish, True)):
        if selected is not None:
            _validate_publication_selection(selected, closed, report, inventory, read_file)


def _validate_projection_header(report: Report, registry: Run, state: State) -> None:
    """Keep public control fields and immutable display metadata tied to their one source."""
    baseline = registry.baseline
    for left, right in (
        (report.run.id, baseline.run.id),
        (report.run.created_at, baseline.run.created_at),
        (report.run.tags, baseline.run.tags),
    ):
        require(
            _same(left, right) if isinstance(left, WireModel) and isinstance(right, WireModel) else left == right,
            'invalid_state',
        )
    for left, right in (
        (report.run.status, state.run.status),
        (report.run.finished_at, state.run.finished_at),
        (report.run.error, state.run.error),
        (report.run.current_invocation_id, state.run.current_invocation_id),
    ):
        require(
            _same(left, right) if isinstance(left, WireModel) and isinstance(right, WireModel) else left == right,
            'invalid_state',
        )
    for left, right in (
        (report.agent.name, baseline.agent.name),
        (report.agent.runtime, baseline.agent.runtime),
        (report.agent.auth_mode, baseline.agent.auth_mode),
    ):
        require(
            _same(left, right) if isinstance(left, WireModel) and isinstance(right, WireModel) else left == right,
            'invalid_state',
        )
    require(report.run.updated_at == state.updated_at, 'invalid_state')
    require(_same(report.agent.resolved_runtime, state.resolved_runtime), 'invalid_state')
    require(_same(report.agent.python_runtime, state.python_runtime), 'invalid_state')
    require(_same(report.metric, state.metric), 'invalid_state')
    require(_same(report.basket, baseline.basket) and _same(report.config, baseline.config), 'invalid_state')


def _validate_registered_task(
    registration: RunTaskItem, selected: StateTaskItem, task: ReportTask, basket: Basket, store: OperationStore
) -> None:
    """Validate native history and derive the public row from its selected immutable facts."""
    require(
        registration.ordinal == task.ordinal == selected.ordinal
        and registration.path == task.directory
        and registration.basket_row == basket.tasks[int(task.ordinal) - 1].to_wire(),
        'invalid_state',
    )
    require(task.processing_status == selected.processing_status, 'invalid_state')
    require(_same(task.progress, selected.progress), 'invalid_state')
    owners = [*selected.history, selected] if selected.attempt is not None else []
    sequences = []
    for owner in owners:
        native, initial = _validate_registered_attempt(registration, owner, task, store)
        assert owner.attempt is not None
        sequences.append(owner.attempt.sequence)
        if owner is selected:
            _validate_selected_row(task, selected, native, initial, store)
    require(
        sequences == list(range(1, len(sequences) + 1)) and selected.next_sequence == len(sequences) + 1,
        'invalid_state',
    )


def _validate_registered_attempt(
    registration: RunTaskItem, owner: StateTaskItem | StateTaskItemHistoryItem, task: ReportTask, store: OperationStore
) -> tuple[NativeTask, ReportTaskScoring | None]:
    """Require a closed native seal and either its outcome or a selected available host score."""
    attempt = owner.attempt
    require(attempt is not None and type(attempt.sequence) is int and attempt.sequence >= 1, 'invalid_state')
    assert attempt is not None
    prefix = registration.path + f'/attempts/{attempt.sequence:03d}/'
    for name, selected_receipt, model in (('start', owner.start, Start), ('seal', owner.seal, Seal)):
        _receipt(selected_receipt, store.inventory, prefix + name + '.json')
        fact = store.document(prefix + name + '.json', model)
        require(
            fact.schema_family == 'pr_similarity_os.' + name
            and type(fact.schema_version) is int
            and fact.schema_version == store.registry.schema_version
            and fact.run_id == store.registry.id
            and fact.basket_sha256 == store.registry.baseline.basket.sha256
            and fact.config_identity_sha256 == store.registry.baseline.config.identity_sha256,
            'invalid_state',
        )
    start, seal = store.document(prefix + 'start.json', Start), store.document(prefix + 'seal.json', Seal)
    require(seal.matches_agent_input(store.registry.agent_input), 'invalid_agent_input')
    validate_python_attempt(seal, store, prefix)
    if store.registry.schema_version == 2:
        require(seal.python_runtime is not None, 'invalid_state')
        _receipt(seal.python_runtime, store.inventory, '_meta/python.json')
    require(start.task_id == task.id, 'invalid_state')
    initial_attempt = _attempt_identity(start.attempt)
    selected_attempt = attempt.to_wire()
    require(
        all(key in selected_attempt and selected_attempt[key] == value for key, value in initial_attempt.items()),
        'invalid_state',
    )
    require(seal.task.id == task.id and _same(seal.task.final_attempt, attempt), 'invalid_state')
    for selection, kind in ((owner.scoring, 'rescore'), (owner.pricing, 'reprice')):
        if selection is not None:
            store.item(selection, kind, task.id, attempt.id)
    if owner.outcome is None:
        require(owner.scoring is not None, 'invalid_state')
        assert owner.scoring is not None
        score = store.score_item(owner.scoring, task.id, attempt.id)
        require(score.scoring.status == 'available', 'invalid_state')
        return seal.task, None
    _receipt(owner.outcome, store.inventory, prefix + 'outcome.json')
    outcome = store.document(prefix + 'outcome.json', Outcome)
    require('scoring' in outcome.task.model_fields_set, 'invalid_state')
    require(_same(outcome.python_runtime, seal.python_runtime), 'invalid_state')
    require(
        outcome.schema_family == 'pr_similarity_os.outcome'
        and type(outcome.schema_version) is int
        and outcome.schema_version == store.registry.schema_version
        and outcome.run_id == store.registry.id
        and _same(outcome.seal, owner.seal),
        'invalid_state',
    )
    for left, right in (
        (outcome.task.id, seal.task.id),
        (outcome.task.final_attempt, seal.task.final_attempt),
        (outcome.task.setup, seal.task.setup),
        (outcome.task.execution, seal.task.execution),
        (outcome.task.changes, seal.task.changes),
        (outcome.task.telemetry, seal.task.telemetry),
        (outcome.task.infrastructure_error, seal.task.infrastructure_error),
    ):
        require(
            _same(left, right) if isinstance(left, WireModel) and isinstance(right, WireModel) else left == right,
            'invalid_state',
        )
    _validate_initial_scoring(prefix, attempt, task, seal, outcome, store.inventory, store.read_file)
    return outcome.task, outcome.task.scoring


def _validate_selected_row(
    task: ReportTask,
    selected: StateTaskItem,
    native: NativeTask,
    initial: ReportTaskScoring | None,
    store: OperationStore,
) -> None:
    """Bind rendered execution, score and price to state-selected native and host results."""
    for left, right in (
        (task.final_attempt, native.final_attempt),
        (task.setup, native.setup),
        (task.execution, native.execution),
        (task.changes, native.changes),
    ):
        require(
            _same(left, right) if isinstance(left, WireModel) and isinstance(right, WireModel) else left == right,
            'invalid_state',
        )
    expected_outcome = None if selected.outcome is None else selected.outcome.path
    require(task.artifacts.outcome == expected_outcome, 'invalid_state')
    scoring = initial
    if selected.scoring is not None:
        assert selected.attempt is not None
        scoring = store.score_item(selected.scoring, task.id, selected.attempt.id).scoring
    require(_same(task.scoring, scoring), 'invalid_state')
    projected_telemetry = task.telemetry.to_wire()
    require(
        all(
            key == 'api_equivalent' or projected_telemetry.get(key) == value
            for key, value in native.telemetry.to_wire().items()
        ),
        'invalid_state',
    )
    pricing = native.telemetry.api_equivalent
    if selected.pricing is not None:
        assert selected.attempt is not None
        pricing = store.price_item(selected.pricing, task.id, selected.attempt.id).api_equivalent
    require(task.telemetry.api_equivalent == pricing, 'invalid_state')


def _validate_initial_scoring(
    prefix: str,
    attempt: ReportAttempt | StateAttempt,
    report_task: ReportTask,
    seal: Seal,
    outcome: Outcome,
    inventory: Mapping[str, Receipt],
    read_file: Callable[[str], bytes],
) -> None:
    """Bind initial inline scoring to its immutable receipt and native metric."""
    receipt = outcome.scoring_result
    _receipt(receipt, inventory, prefix + 'scoring.json')
    scoring = _document(prefix + 'scoring.json', inventory, read_file, ScoringEvidence)
    require(
        scoring.schema_family == 'pr_similarity_os.scoring'
        and type(scoring.schema_version) is int
        and scoring.schema_version == outcome.schema_version
        and scoring.run_id == outcome.run_id
        and scoring.task_id == report_task.id
        and scoring.attempt_id == attempt.id
        and scoring.seal == outcome.seal
        and scoring.metric == seal.metric == outcome.metric
        and scoring.scoring == outcome.task.scoring,
        'invalid_state',
    )
    require(
        scoring.matches_error(outcome)
        and scoring.scoring.status in {'available', 'unavailable'}
        and outcome.has_scoring_receipt(receipt),
        'invalid_state',
    )


def _validate_publication_selection(
    selected: StatePendingPublish | StateLastPublish,
    closed: bool,
    report: Report,
    inventory: Mapping[str, Receipt],
    read_file: Callable[[str], bytes],
) -> None:
    """Validate publication provenance while allowing only its deliberately omitted nested payload."""
    identifier = selected.id
    prefix = 'operations/publish/' + identifier + '/'
    _receipt(selected.request, inventory, prefix + 'request.json')
    request = _document(prefix + 'request.json', inventory, read_file, PublishRequest)
    require(
        request.schema_family == 'pr_similarity_os.publish_request'
        and request.schema_version == report.schema_version
        and request.id == identifier
        and request.run_id == report.run.id
        and request.basket_sha256 == report.basket.sha256
        and request.config_identity_sha256 == report.config.identity_sha256,
        'invalid_state',
    )
    payload = request.payload
    require(
        payload is not None and payload.path == prefix + 'payload.tar.gz',
        'invalid_state',
    )
    assert payload is not None
    if payload.path in inventory:
        require(inventory[payload.path].to_wire() == payload.to_wire(), 'invalid_state')
    if closed:
        require(isinstance(selected, StateLastPublish), 'invalid_state')
        assert isinstance(selected, StateLastPublish)
        _receipt(selected.receipt, inventory, prefix + 'receipt.json')
        receipt = _document(prefix + 'receipt.json', inventory, read_file, PublishReceipt)
        require(
            receipt.schema_family == 'pr_similarity_os.publish_receipt'
            and receipt.schema_version == report.schema_version
            and receipt.id == identifier
            and receipt.run_id == report.run.id
            and _same(receipt.request, selected.request),
            'invalid_state',
        )


def _reference(path: str | None, inventory: Mapping[str, Receipt]) -> None:
    """Require each present report artifact to exist in the verified archive inventory."""
    if path is not None:
        safe_path(path)
        require(path in inventory, 'missing_artifact')


def _validate_archive_task(
    task: ReportTask, report: Report, inventory: Mapping[str, Receipt], read_file: Callable[[str], bytes]
) -> None:
    """Check frozen task ownership and all selected native/public artifact references."""
    _reference(task.changes.diff, inventory)
    for path in (
        task.artifacts.outcome,
        task.artifacts.triplets_manifest,
        task.artifacts.setup_log,
        task.artifacts.agent_events,
        task.artifacts.agent_log,
    ):
        _reference(path, inventory)
    _reference(task.artifacts.effective_config.path, inventory)
    for entry in (task.telemetry.session_db, task.telemetry.usage_ledger):
        if entry:
            _reference(entry.path, inventory)
            require(inventory[entry.path].sha256 == entry.sha256)
    for row in task.scoring.files:
        for reference in (row.base, row.human, row.ai):
            _reference(reference, inventory)
    if task.artifacts.triplets_manifest is not None:
        validate_triplet_index(
            parse_evidence(TripletIndex, parse_json(read_file(task.artifacts.triplets_manifest)), 'invalid_triplets'),
            task,
            inventory,
            run_id=report.run.id,
        )


def _source_path(value: str) -> str:
    """Require a normalized source path without repository metadata components."""
    path = safe_path(value)
    require('.git' not in path.split('/'), 'invalid_triplets')
    return path


def _triplet_prefix(task: ReportTask) -> str:
    """Bind public role paths to the registered directory and selected attempt sequence."""
    directory, attempt = task.directory, task.final_attempt
    require(isinstance(directory, str) and attempt is not None, 'invalid_public_artifact')
    assert isinstance(directory, str) and attempt is not None
    require(
        re.fullmatch(r'tasks/[0-9]{3,}-[a-z0-9-]+', safe_path(directory)) is not None,
        'invalid_public_artifact',
    )
    sequence = attempt.sequence
    require(type(sequence) is int and sequence >= 1, 'invalid_public_artifact')
    prefix = f'{directory}/attempts/{sequence:03d}/triplets/'
    require(
        task.artifacts.triplets_manifest == prefix + 'index.json',
        'invalid_public_artifact',
    )
    return prefix


def _triplet_entry(entry: TripletEntry, number: int, prefix: str, inventory: Mapping[str, Receipt]) -> str:
    """Validate every role receipt and explicit absence of bytes and metadata."""
    require(set(entry.to_wire()) == {'path', 'number', 'exists', 'modes', 'files'}, 'invalid_triplets')
    path = _source_path(entry.path)
    require(type(entry.number) is int and entry.number == number, 'invalid_triplets')
    for group in (entry.exists, entry.modes, entry.files):
        require(set(group.to_wire()) == {'base', 'human', 'ai'}, 'invalid_triplets')
    for role, exists, mode, receipt in (
        ('base', entry.exists.base, entry.modes.base, entry.files.base),
        ('human', entry.exists.human, entry.modes.human, entry.files.human),
        ('ai', entry.exists.ai, entry.modes.ai, entry.files.ai),
    ):
        expected = triplet_path(number, path, role)
        if not exists:
            require(mode is None and receipt is None and prefix + expected not in inventory, 'invalid_triplets')
            continue
        require(mode in ('100644', '100755') and receipt is not None, 'invalid_triplets')
        assert receipt is not None
        require(set(receipt.to_wire()) == {'path', 'size_bytes', 'sha256'}, 'invalid_triplets')
        require(receipt.path == expected and prefix + expected in inventory, 'invalid_triplets')
        actual = inventory[prefix + expected]
        require(
            receipt.size_bytes >= 0 and receipt.size_bytes == actual.size_bytes and receipt.sha256 == actual.sha256,
            'invalid_triplets',
        )
    return path


def validate_triplet_index(index: object, task: ReportTask, inventory: Mapping[str, Receipt], *, run_id: str) -> None:
    """Validate the exact schema-1 adjacent variants and its run/task/attempt/revision owner.

    Args:
        index: Decoded immutable triplet index.
        task: Selected report task with registered directory and final attempt.
        inventory: Archive members whose size and digest were verified from bytes.
        run_id: Owning report run identity.

    Raises:
        ViewerError: If ownership, exact paths, nullable states or receipts disagree.

    """
    index = parse_evidence(
        TripletIndex, index.to_wire() if isinstance(index, TripletIndex) else index, 'invalid_triplets'
    )
    require(
        set(index.to_wire()) == {'version', 'run_id', 'task_id', 'attempt_id', 'source', 'entries'}, 'invalid_triplets'
    )
    require(type(index.version) is int and index.version == 1, 'invalid_triplets')
    attempt = task.final_attempt
    require(attempt is not None, 'invalid_triplets')
    assert attempt is not None
    require(
        index.run_id == run_id and index.task_id == task.id and index.attempt_id == attempt.id,
        'invalid_triplets',
    )
    require(
        index.source.to_wire() == {'before': task.before, 'after': task.after},
        'invalid_triplets',
    )
    entries = index.entries
    require(isinstance(entries, list), 'invalid_triplets')
    prefix = _triplet_prefix(task)
    rows = {}
    paths = []
    for number, entry in enumerate(entries, 1):
        path = _triplet_entry(entry, number, prefix, inventory)
        paths.append(path)
        rows[path] = entry
    require(paths == sorted(set(paths)), 'invalid_triplets')
    for scored in task.scoring.files:
        require(scored.path in rows, 'invalid_triplets')
        row = rows[scored.path]
        require(scored.exists == row.exists, 'invalid_triplets')
        for reference, receipt in (
            (scored.base, row.files.base),
            (scored.human, row.files.human),
            (scored.ai, row.files.ai),
        ):
            expected = prefix + receipt.path if receipt is not None else None
            require(reference == expected, 'invalid_triplets')


def _evidence_document(path: str, inventory: Mapping[str, Receipt], read_file: Callable[[str], bytes]) -> JsonObject:
    """Read native ownership facts only after matching exact archived bytes and hashes."""
    require(path in inventory, 'invalid_inference')
    content = read_file(path)
    receipt = inventory[path]
    require(
        len(content) == receipt.size_bytes and digest(content) == receipt.sha256,
        'invalid_inference',
    )
    return require_object(parse_json(content), 'invalid_inference')


def _native_owner(value: Start | Seal | InferenceStart | InferenceSeal, report: Report, family: str) -> None:
    """Bind a native start/seal to the same frozen run, basket and agent configuration."""
    require(
        value.schema_family == 'pr_similarity_os.' + family
        and type(value.schema_version) is int
        and value.schema_version == report.schema_version
        and value.run_id == report.run.id
        and value.basket_sha256 == report.basket.sha256
        and value.config_identity_sha256 == report.config.identity_sha256,
        'invalid_inference',
    )


def _sealed_config(
    claim: ReportInferenceEffectiveAttemptItem,
    task: ReportTask,
    report: Report,
    inventory: Mapping[str, Receipt],
    read_file: Callable[[str], bytes],
) -> JsonObject:
    """Validate one selected or historical effective config against explicit native owners."""
    ref = safe_path(claim.source_ref)
    require(re.fullmatch(r'agent/effective/[0-9a-f]{64}\.json', ref) is not None, 'invalid_inference')
    require(ref in inventory and ref.split('/')[-1] == inventory[ref].sha256 + '.json', 'invalid_inference')
    state = _document('_meta/state.json', inventory, read_file, InferenceState, 'invalid_inference')
    matches = [
        item
        for row in state.tasks
        if row.id == task.id
        for item in [*row.history, row]
        if item.attempt is not None and item.attempt.id == claim.attempt_id
    ]
    require(len(matches) == 1, 'invalid_inference')
    selected = matches[0]
    assert selected.attempt is not None and selected.start is not None and selected.seal is not None
    sequence = selected.attempt.sequence
    prefix = task.directory + f'/attempts/{sequence:03d}/'
    require(
        selected.start.path == prefix + 'start.json' and selected.seal.path == prefix + 'seal.json',
        'invalid_inference',
    )
    final = task.final_attempt
    require(final is not None and sequence <= final.sequence, 'invalid_inference')
    assert final is not None
    if sequence == final.sequence:
        require(claim.attempt_id == final.id, 'invalid_inference')
        require(task.artifacts.effective_config.path == ref, 'invalid_inference')
    start = _document(prefix + 'start.json', inventory, read_file, InferenceStart, 'invalid_inference')
    _native_owner(start, report, 'start')
    attempt = start.attempt
    require(attempt is not None, 'invalid_inference')
    assert attempt is not None
    require(start.task_id == task.id, 'invalid_inference')
    require(
        attempt.id == claim.attempt_id and type(attempt.sequence) is int and attempt.sequence == sequence,
        'invalid_inference',
    )
    seal = _document(prefix + 'seal.json', inventory, read_file, InferenceSeal, 'invalid_inference')
    _native_owner(seal, report, 'seal')
    sealed_task = seal.task
    require(sealed_task is not None, 'invalid_inference')
    assert sealed_task is not None
    require(sealed_task.id == task.id, 'invalid_inference')
    sealed_attempt = sealed_task.final_attempt
    require(sealed_attempt is not None, 'invalid_inference')
    assert sealed_attempt is not None
    require(
        sealed_attempt.id == claim.attempt_id
        and type(sealed_attempt.sequence) is int
        and sealed_attempt.sequence == sequence,
        'invalid_inference',
    )
    require(set(attempt.to_wire()) == set(sealed_attempt.to_wire()), 'invalid_inference')
    require(_attempt_identity(attempt) == _attempt_identity(sealed_attempt), 'invalid_inference')
    if sequence == final.sequence:
        require(_attempt_identity(attempt) == _attempt_identity(final), 'invalid_inference')
    _validate_inference_task_identity(sealed_task, task)
    artifacts = sealed_task.artifacts
    require(artifacts is not None, 'invalid_inference')
    assert artifacts is not None
    effective = artifacts.effective_config
    require(
        effective is not None and effective.path == ref,
        'invalid_inference',
    )
    _sealed_receipts(seal, prefix, ref, inventory, report.agent_input)
    return _evidence_document(ref, inventory, read_file)


def _validate_inference_task_identity(sealed_task: InferenceTaskFacts, task: ReportTask) -> None:
    """Bind sealed source facts to the public task before exposing its effective config."""
    require(
        (
            sealed_task.repository,
            sealed_task.number,
            sealed_task.kind,
            sealed_task.source.to_wire(),
            sealed_task.diff_policy,
            sealed_task.before,
            sealed_task.after,
            sealed_task.before_tree,
            sealed_task.after_tree,
            sealed_task.workspace,
            sealed_task.agent_task,
        )
        == (
            task.repository,
            task.number,
            task.kind,
            task.source.to_wire(),
            task.diff_policy,
            task.before,
            task.after,
            task.before_tree,
            task.after_tree,
            task.workspace,
            task.agent_task,
        ),
        'invalid_inference',
    )


def _sealed_receipts(
    seal: InferenceSeal, prefix: str, ref: str, inventory: Mapping[str, Receipt], agent_input: ReportAgentInput
) -> None:
    """Require unique attempt-scoped seal receipts matching actual archive bytes."""
    receipts = seal.artifact_receipts
    require(isinstance(receipts, list), 'invalid_inference')
    assert isinstance(receipts, list)
    require(seal.matches_agent_input(agent_input), 'invalid_inference')
    allowed = [
        agent_input.config.to_wire(),
        *(item.to_wire() for item in agent_input.resources),
        inventory[ref].to_wire(),
    ]
    if seal.schema_version == 2:
        require(seal.python_runtime is not None, 'invalid_inference')
        assert seal.python_runtime is not None
        _receipt(seal.python_runtime, inventory, '_meta/python.json')
        allowed.append(seal.python_runtime.to_wire())
        require(seal.python_runtime.to_wire() in [item.to_wire() for item in receipts], 'invalid_inference')
    paths = []
    for receipt in receipts:
        require(
            set(receipt.to_wire()) == {'path', 'size_bytes', 'sha256'},
            'invalid_inference',
        )
        path = safe_path(receipt.path)
        require((path.startswith(prefix) or receipt.to_wire() in allowed) and path in inventory, 'invalid_inference')
        require(
            type(receipt.size_bytes) is int and receipt.size_bytes >= 0,
            'invalid_inference',
        )
        require(receipt.to_wire() == inventory[path].to_wire(), 'invalid_inference')
        paths.append(path)
    require(len(paths) == len(set(paths)) and ref in paths, 'invalid_inference')


def validate_inference_evidence(
    report: Report, identity: ConfigIdentity, inventory: Mapping[str, Receipt], read_file: Callable[[str], bytes]
) -> None:
    """Compare exact configured facts and each declared effective origin with owned sealed evidence.

    Committed run/state selection coverage is a separate validation gate. This
    function never adopts unreferenced attempt directories as inference facts.
    """
    require('inference' in report.model_fields_set, 'invalid_inference')
    declared = report.inference
    try:
        require(public_inference(declared.to_wire()) == declared.to_wire(), 'invalid_inference')
    except ValueError:
        raise ViewerError('invalid_inference', 'Unsupported inference evidence.') from None
    expected = build_inference(identity.to_wire())
    require(declared.configured.to_wire() == expected['configured'], 'invalid_inference')
    tasks = {task.directory: task for task in report.tasks}
    require(len(tasks) == len(report.tasks), 'invalid_inference')
    claims = declared.effective.attempts
    refs = [claim.source_ref for claim in claims]
    identifiers = [claim.attempt_id for claim in claims]
    require(
        len(identifiers) == len(set(identifiers)),
        'invalid_inference',
    )
    state = _document('_meta/state.json', inventory, read_file, InferenceState, 'invalid_inference')
    by_id = {task.id: task for task in report.tasks}
    owners = {}
    captured = set()
    for selected in state.tasks:
        for item in [*selected.history, selected]:
            attempt = item.attempt
            if attempt is None:
                continue
            require(attempt.id not in owners and selected.id in by_id, 'invalid_inference')
            owners[attempt.id] = by_id[selected.id]
            if item.seal is not None:
                seal = _document(item.seal.path, inventory, read_file, InferenceCaptureSeal, 'invalid_inference')
                sealed_task = seal.task
                require(sealed_task is not None, 'invalid_inference')
                artifacts = sealed_task.artifacts
                require(artifacts is not None, 'invalid_inference')
                effective = artifacts.effective_config
                require(effective is not None, 'invalid_inference')
                if effective.status == 'saved':
                    captured.add(attempt.id)
    require(set(identifiers) == captured, 'invalid_inference')
    configs = []
    for claim in claims:
        require(claim.attempt_id in owners, 'invalid_inference')
        native = _sealed_config(claim, owners[claim.attempt_id], report, inventory, read_file)
        configs.append({'attempt_id': claim.attempt_id, 'source_ref': claim.source_ref, 'config': native})
    for task in report.tasks:
        ref = task.artifacts.effective_config.path
        if ref is not None:
            require(ref in refs, 'invalid_inference')
    require(declared.to_wire() == build_inference(identity.to_wire(), configs), 'invalid_inference')


def _attempt_identity(attempt: WireModel) -> JsonObject:
    """Compare complete immutable attempt fields while excluding its closing time."""
    value = attempt.to_wire()
    value.pop('finished_at', None)
    return value
