"""Offline scoring selects immutable operation results without rewriting native outcomes."""

import uuid
from pathlib import Path

from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.metrics.scoring import metric_receipt, score_pr
from benchmark.models.evidence import OperationDetails, ScoreItem, Seal
from benchmark.models.report import ReportError
from benchmark.models.scoring import Diagnostic
from benchmark.models.triplets import TripletIndex
from benchmark.persistence.artifact_refs import read_reference, reference
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import run_lock
from benchmark.persistence.triplets import read_triplets
from benchmark.runtime.commands import Command


def _score_current(store: RunStore) -> list[ScoreItem]:
    """Score only current manifests whose complete role files are owned by the native seal."""
    items: list[ScoreItem] = []
    rows = store.snapshot().tasks
    for selected, row in zip(store.state.tasks, rows, strict=True):
        manifest = row.artifacts.triplets_manifest
        if manifest is None or selected.seal is None:
            continue
        assert selected.attempt is not None
        native = read_reference(store.directory, selected.seal, Seal)
        owned = {item.path: item for item in native.artifact_receipts}
        if manifest not in owned:
            raise BenchmarkError('triplet_receipt_missing', 'Native seal does not own the triplet manifest.')
        path = artifact_path(store.directory, manifest, must_exist=True)
        signed_index = read_reference(store.directory, owned[manifest], TripletIndex)
        bundle = read_triplets(path.parent, owner=row, run_id=store.registry.id)
        if bundle.index != signed_index:
            raise BenchmarkError('artifact_digest_mismatch', 'Triplet index changed while reading selected inputs.')
        required = {manifest}
        for entry in bundle.index.entries:
            required.update(
                (path.parent / item.path).relative_to(store.directory).as_posix()
                for item in (entry.files.base, entry.files.human, entry.files.ai)
                if item is not None
            )
        if not required <= owned.keys():
            raise BenchmarkError('triplet_receipt_missing', 'Native seal does not bind every original source role.')
        if any(reference(store.directory, name) != owned[name] for name in required):
            raise BenchmarkError('artifact_digest_mismatch', 'Selected source roles differ from native capture.')
        diagnostics: list[Diagnostic] = []
        scoring = score_pr(
            bundle.states,
            artifact_prefix=path.parent.relative_to(store.directory).as_posix(),
            diagnostics=diagnostics,
            artifact_refs=bundle.references,
        )
        items.append(
            ScoreItem(task_id=row.id, attempt_id=selected.attempt.id, scoring=scoring, diagnostics=diagnostics)
        )
    return items


def rescore(run_dir: Path, *, level: str = 'INFO') -> int:
    """Recompute current sealed source scores under one authoritative writer lease."""
    with run_lock(run_dir) as lease:
        store = RunStore.open(run_dir, lease=lease)
        report = store.rebuild()
        if report.run.status in ('running', 'stopping'):
            raise BenchmarkError('run_active', 'Rescore requires a stopped run.')
        command = Command(run_dir, 'rescore', level=level, previous=report.run.current_invocation_id)
        identifier = str(uuid.uuid4())
        metric = None
        try:
            metric = metric_receipt()
            items = _score_current(store)
            if metric_receipt() != metric:
                raise BenchmarkError(
                    'metric_changed', 'Metric changed during rescore; preserve the previous selection.'
                )
            selection = store.prepare_operation(
                'rescore', items, invocation_id=command.id, metric=metric, operation_id=identifier
            )
            store.accept_operation(selection)
            fallbacks = sum(item.code == 'codebleu_fallback' for row in items for item in row.diagnostics)
            command.message(f'Selected scores for {len(items)} sealed task(s); CodeBLEU fallback files: {fallbacks}.')
            command.finish('completed')
            return 0
        except OPERATION_ERRORS as error:
            command.diagnostic(error)
            operation = run_dir / 'operations/rescore' / identifier
            if not operation.exists():
                try:
                    store.prepare_operation(
                        'rescore',
                        [],
                        invocation_id=command.id,
                        metric=metric,
                        operation_id=identifier,
                        status='failed',
                        extra=OperationDetails(
                            error=ReportError(
                                code=error.code if isinstance(error, BenchmarkError) else 'rescore_failed',
                                message='Host scoring failed; previous selections remain available.',
                            )
                        ),
                    )
                except OPERATION_ERRORS as secondary:
                    command.diagnostic(secondary)
            command.finish('failed')
            raise
        finally:
            command.close()
