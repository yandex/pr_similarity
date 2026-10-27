"""Embed validated, selected run evidence for report interactions without a server."""

import difflib
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.execution.agent_input import benchmark_settings
from benchmark.json_types import JsonObject
from benchmark.models.evidence import ConfigIdentity, Seal
from benchmark.models.report import Report, ReportTask
from benchmark.models.triplets import TripletIndex
from benchmark.persistence.artifact_refs import read_reference, read_reference_json
from benchmark.persistence.layout import artifact_path
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json, sha256
from benchmark.persistence.triplets import TripletBundle, read_triplets
from benchmark.presentation.native_config import native_projection
from benchmark.vcs.workspace import FileState


def source_diff(base: FileState, changed: FileState, path: str, role: str) -> str:
    """Keep full unified text, missing final newlines, and empty-file existence changes."""
    chunks = []
    for line in difflib.unified_diff(
        base.text.splitlines(keepends=True),
        changed.text.splitlines(keepends=True),
        fromfile='base/' + path,
        tofile=role + '/' + path,
    ):
        chunks.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    if not chunks and base.exists != changed.exists:
        chunks = ['[File ' + ('added' if changed.exists else 'deleted') + '; empty content]\n']
    return ''.join(chunks)


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


def _task_diffs(store: RunStore, report: Report) -> JsonObject:
    """Generate source-role diffs for the exact scored files in the selected attempts."""
    result: JsonObject = {}
    for index, (selected, row) in enumerate(zip(store.state.tasks, report.tasks, strict=True)):
        if selected.id != row.id:
            raise BenchmarkError('report_owner_mismatch', 'Task display differs from the selected run state.')
        if not row.scoring.files:
            continue
        if selected.seal is None:
            raise BenchmarkError('triplet_receipt_missing', 'Scored files require a native seal.')
        seal = read_reference(store.directory, selected.seal, Seal)
        bundle = _sealed_bundle(store, row, seal)
        files: JsonObject = {}
        for number, file in enumerate(row.scoring.files):
            if file.path not in bundle.states:
                raise BenchmarkError('invalid_triplets', 'Scored file is absent from the source manifest.')
            state = bundle.states[file.path]
            files[str(number)] = {
                'human_diff': source_diff(state.base, state.human, file.path, 'human'),
                'agent_diff': source_diff(state.base, state.ai, file.path, 'ai'),
            }
        result[str(index)] = files
    return result


def embedded_data(run_dir: Path, report: Report) -> JsonObject:
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
        config.update(supported=True, config=native_projection(raw))
    except ValueError as error:
        config['reason'] = str(error)
    return {'config': config, 'benchmark': benchmark_settings(identity).to_wire(), 'diffs': _task_diffs(store, report)}
