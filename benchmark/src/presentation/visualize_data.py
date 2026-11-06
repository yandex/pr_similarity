"""Embed validated, selected run evidence for report interactions without a server."""

from decimal import Decimal
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any

from benchmark.errors import BenchmarkError
from benchmark.execution.agent_input import benchmark_settings
from benchmark.json_types import JsonObject
from benchmark.models.evidence import ConfigIdentity, Seal
from benchmark.models.report import Report, ReportTask
from benchmark.models.triplets import TripletIndex
from benchmark.persistence.artifact_refs import read_reference, read_reference_json
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import bytes_sha256, canonical_bytes, document_bytes, read_json, sha256
from benchmark.persistence.triplets import TripletBundle, read_triplets
from benchmark.presentation.display_limits import CONFIG_DISPLAY_BYTES, DIFF_BYTES, DIFF_LINES
from benchmark.presentation.file_views import change_counts, file_view
from benchmark.presentation.measurements import (
    TOKEN_CATEGORIES,
    Measurement,
    combine_measurements,
    measurement_dict,
)
from benchmark.presentation.native_config import native_projection
from benchmark.presentation.run_metrics import aggregate, aggregate_snapshot, task_status
from benchmark.presentation.task_measurements import (
    TaskMeasurements,
    build_task_measurements,
)
from benchmark.pricing.summary import CostEvidenceError


def _sealed_bundle(store: RunStore, row: ReportTask, seal: Seal) -> TripletBundle:
    """Bind the index and every source role to the selected native seal before display."""
    manifest = row.artifacts.triplets_manifest
    owned = {item.path: item for item in seal.artifact_receipts}
    if manifest is None or manifest not in owned:
        raise BenchmarkError('triplet_receipt_missing', 'Selected source manifest is not owned by its native seal.')
    path = artifact_path(store.directory, manifest, must_exist=True)
    index = read_reference(store.directory, owned[manifest], TripletIndex)
    bundle = read_triplets(path.parent, owner=row, run_id=store.registry.id)
    if bundle.index != index:
        raise BenchmarkError('artifact_digest_mismatch', 'Source index changed while preparing the report.')
    for entry in bundle.index.entries:
        for receipt in (entry.files.base, entry.files.human, entry.files.ai):
            if receipt is not None:
                relative = (path.parent / receipt.path).relative_to(store.directory).as_posix()
                if owned.get(relative) != receipt.model_copy(update={'path': relative}):
                    raise BenchmarkError('triplet_receipt_missing', 'Source role is not bound to the native seal.')
    return bundle


def embedded_data(run_dir: Path, report: Report) -> dict[str, Any]:
    """Load portable config, harness metadata and diffs while the caller holds the run lock."""
    store = RunStore.open(run_dir)
    if store.snapshot() != report:
        raise BenchmarkError('report_changed', 'Selected report changed while preparing local HTML.')
    identity = ConfigIdentity.model_validate(read_json(artifact_path(run_dir, report.config.identity_path)))
    if sha256(identity) != report.config.identity_sha256:
        raise BenchmarkError('artifact_digest_mismatch', 'Run configuration identity changed.')
    raw = read_reference_json(run_dir, report.agent_input.config)
    config: JsonObject = {'source': report.agent_input.config.to_wire(), 'supported': False, 'config': None}
    try:
        projected = native_projection(raw)
        if len(canonical_bytes(projected)) > CONFIG_DISPLAY_BYTES:
            config.update(reason='display_limit')
        else:
            config.update(supported=True, config=projected)
    except ValueError as error:
        config['reason'] = str(error)
    try:
        measurements = build_task_measurements(store, report)
    except CostEvidenceError as error:
        raise BenchmarkError(error.code, error.message) from error
    instructions = package_files('benchmark').joinpath('resources/task_instructions.txt').read_bytes()
    default = identity.instructions.sha256 == bytes_sha256(instructions)
    config['setup_summary'] = {
        'auth_mode': identity.auth_mode,
        'setup_script': identity.setup is not None,
        'instructions_default': default,
        'instructions_text': instructions.decode() if default else None,
        'env_count': len(identity.env),
    }
    file_views = {}
    file_diffs = {}
    changes = {}
    for row in report.tasks:
        # read_triplets verifies the original source receipts before any presentation read.
        selected = next(item for item in store.state.tasks if item.id == row.id)
        if row.scoring.files:
            if selected.seal is None:
                raise BenchmarkError('triplet_receipt_missing', 'Scored files require native evidence.')
            bundle = _sealed_bundle(store, row, read_reference(store.directory, selected.seal, Seal))
            if any(file.path not in bundle.states for file in row.scoring.files):
                raise BenchmarkError('invalid_triplets', 'Scored file is absent from the source manifest.')

        def read_source(path: str, limit: int) -> bytes:
            """Read a bounded prefix from an already verified source artifact."""
            with artifact_path(store.directory, path, must_exist=True).open('rb') as stream:
                return stream.read(limit)

        file_views[row.id] = {
            file.path: file_view(file, read_source, DIFF_BYTES, DIFF_LINES, full=True) for file in row.scoring.files
        }
        file_diffs[row.id] = {
            file.path: file_view(file, read_source, DIFF_BYTES, DIFF_LINES) for file in row.scoring.files
        }
        changes[row.id] = change_counts(row, read_source, DIFF_BYTES, DIFF_LINES)
    return {
        'config': config,
        'auth_mode': identity.auth_mode,
        'report_sha256': bytes_sha256(document_bytes(report)),
        'file_views': file_views,
        'file_diffs': file_diffs,
        'changes': changes,
        'aggregates': aggregate_snapshot(report.tasks, measurements),
        'task_metrics': {
            row.id: {kind: aggregate([row], [task_status(row)], 'Mpr', kind, measurements) for kind in ('Mf', 'LCf')}
            for row in report.tasks
        },
        'benchmark': benchmark_settings(identity).to_wire(),
        'measurements': display_metrics(report, measurements),
    }


def display_metrics(report: Report, measurements: dict[str, TaskMeasurements] | None = None) -> dict[str, Any]:
    """Normalize each task and all status masks before embedding autonomous HTML."""
    if measurements is None:
        measurements = {}
        for task in report.tasks:
            price = task.telemetry.api_equivalent
            amount = (price.amount if price.status == 'complete' else None) if price else task.telemetry.cost.amount
            measurements[task.id] = TaskMeasurements(
                tokens={
                    category: Measurement(
                        subtotal=getattr(task.telemetry.tokens, category) or 0,
                        known=getattr(task.telemetry.tokens, category) is not None,
                        complete=getattr(task.telemetry.tokens, category) is not None,
                    )
                    for category in TOKEN_CATEGORIES
                },
                cost=Measurement(
                    subtotal=Decimal(str(amount)) if amount is not None else 0,
                    known=amount is not None,
                    complete=amount is not None,
                ),
                cost_source='reprice' if price else 'opencode',
            )

    def projection(values: list[TaskMeasurements]) -> dict[str, Any]:
        """Sum currency in Python Decimal, never repeatedly in browser floats."""
        return {
            'cost': measurement_dict(combine_measurements([value.cost for value in values])),
            'tokens': {
                category: measurement_dict(combine_measurements([value.tokens[category] for value in values]))
                for category in TOKEN_CATEGORIES
            },
        }

    def status(task: ReportTask) -> str:
        """Keep the established final-attempt task selection semantics."""
        if task.infrastructure_error or task.processing_status != 'completed':
            return 'fail'
        if task.execution.status == 'timeout':
            return 'timeout'
        if task.execution.status == 'completed':
            return {'empty': 'no-op', 'nonempty': 'OK'}.get(task.changes.status, 'fail')
        return 'fail'

    statuses = ('OK', 'no-op', 'timeout', 'fail')
    return {
        'tasks': {task.id: projection([measurements[task.id]]) for task in report.tasks},
        'selections': {
            str(mask): projection(
                [measurements[task.id] for task in report.tasks if mask & (1 << statuses.index(status(task)))]
            )
            for mask in range(16)
        },
    }
