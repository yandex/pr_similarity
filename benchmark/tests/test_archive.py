"""Archive only state-selected v1 evidence and verify it through RunStore."""

import io
import tarfile
from pathlib import Path
from typing import NoReturn

import pytest
from benchmark import coordinator, recovery
from benchmark.archive import build_manifest, create_archive, create_archive_locked, verify_archive
from benchmark.errors import BenchmarkError
from benchmark.run_store import RunStore
from benchmark.storage import read_json, run_lock, utc_now, write_json
from test_artifact_workflow import _crash, _inputs
from test_operation_workflows import stopped_run


def complete_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, attempts: int = 1) -> Path:
    """Create a real offline native executor/store run, without a model or network."""
    directory, _config, _dataset = stopped_run(tmp_path, monkeypatch, attempts=attempts)
    write_json(directory / '_meta/sandbox.json', {'schema_version': 1, 'executable': 'fixture'})
    return directory


def recovered_seal_only_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, score_error: bool = False
) -> tuple[Path, int]:
    """Crash after native seal and resume through only the saved host-scoring boundary."""
    workflow = _inputs(tmp_path)
    directory, _notice = _crash(tmp_path, 'seal')

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        """Prove sealed recovery never provisions or executes another native agent."""
        raise AssertionError('Seal-only host recovery must not provision native runtime.')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    if score_error:

        def fail_score(*_args: object, **_kwargs: object) -> dict:
            """Produce an explicit unavailable host result without a native replay."""
            raise BenchmarkError('scoring_failed', 'Fixture recovered host scoring failed.')

        monkeypatch.setattr(recovery, 'score_saved', fail_score)
    status, restored = coordinator.run(workflow.config, workflow.dataset, resume=directory, progress='off')
    assert restored == directory
    return directory, status


def test_archive_roundtrip_uses_actual_store_executor_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve real sealed attempts and an unselected operation without adopting it."""
    directory = complete_run(tmp_path, monkeypatch, attempts=2)
    unselected = directory / 'operations/rescore/diagnostic/result.json'
    unselected.parent.mkdir(parents=True)
    unselected.write_text('{"diagnostic":"not selected"}\n')
    archive = create_archive(directory, tmp_path / 'archive.tar.gz')
    manifest = verify_archive(archive.path)
    paths = [entry['path'] for entry in manifest['files']]
    assert manifest['complete']
    assert paths == sorted(set(paths))
    assert 'run.json' in paths and '_meta/state.json' in paths and 'report.json' in paths
    assert 'operations/rescore/diagnostic/result.json' in paths
    assert sum(path.endswith('/outcome.json') for path in paths) == 2
    assert 'checkpoint.json' not in paths


def test_archive_accepts_available_host_score_without_fabricating_native_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal-only crash remains publishable after an exact selected host recovery result."""
    directory, status = recovered_seal_only_run(tmp_path, monkeypatch)
    selected = RunStore.open(directory).state['tasks'][0]
    assert status == 0 and selected['seal'] is not None and selected['outcome'] is None
    assert selected['scoring'] is not None
    archive = create_archive(directory, tmp_path / 'recovered.tar.gz')
    manifest = verify_archive(archive.path)
    paths = {entry['path'] for entry in manifest['files']}
    attempt = directory / RunStore.open(directory).attempt_directory(selected['id'], selected['attempt']['sequence'])
    assert manifest['complete']
    assert (attempt / 'seal.json').relative_to(directory).as_posix() in paths
    assert (attempt / 'outcome.json').relative_to(directory).as_posix() not in paths
    assert selected['scoring']['result']['path'] in paths and selected['scoring']['receipt']['path'] in paths


def test_archive_refuses_seal_without_selected_host_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A native seal alone does not authorize treating unfinished host scoring as complete."""
    workflow = _inputs(tmp_path)
    directory, _notice = _crash(tmp_path, 'seal')
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        recovery.recover_native_facts(store, workflow.config)
        report = store.snapshot()
        report['tasks'][0]['processing_status'] = 'interrupted'
        report['run'].update(status='interrupted', finished_at=utc_now())
        store.commit(report)
    manifest = build_manifest(directory)
    assert not manifest['complete']
    assert any(item['path'].endswith('/outcome.json') for item in manifest['missing_artifacts'])
    with pytest.raises(BenchmarkError) as caught:
        create_archive(directory, tmp_path / 'unscored.tar.gz')
    assert caught.value.code == 'archive_incomplete'


def test_archive_refuses_unavailable_host_score_without_native_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected host failure remains explicit and cannot stand in for a completed score."""
    directory, status = recovered_seal_only_run(tmp_path, monkeypatch, score_error=True)
    report = RunStore.open(directory).snapshot()
    assert status == 1 and report['tasks'][0]['scoring']['status'] == 'unavailable'
    manifest = build_manifest(directory)
    assert not manifest['complete']
    assert any(item['path'].endswith('/outcome.json') for item in manifest['missing_artifacts'])
    with pytest.raises(BenchmarkError) as caught:
        create_archive(directory, tmp_path / 'unavailable.tar.gz')
    assert caught.value.code == 'archive_incomplete'


def test_archive_repairs_only_missing_derived_report_under_writer_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost report cache is rebuilt from state before tar creation, without new execution."""
    directory = complete_run(tmp_path, monkeypatch)
    (directory / 'report.json').unlink()
    archive = create_archive(directory, tmp_path / 'archive.tar.gz')
    assert archive.manifest['complete'] and (directory / 'report.json').is_file()
    assert verify_archive(archive.path)['report_sha256'] == archive.manifest['report_sha256']


def test_locked_archive_reuses_exact_active_store_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publisher callers do not acquire a second incompatible lock around RunStore."""
    directory = complete_run(tmp_path, monkeypatch)
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
        archive = create_archive_locked(directory, tmp_path / 'archive.tar.gz', store=store, lease=lease)
    assert archive.path.is_file() and verify_archive(archive.path)['complete']


def test_locked_archive_rejects_foreign_or_expired_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller cannot pair another run's store and lease to bypass writer ownership."""
    directory = complete_run(tmp_path, monkeypatch)
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    with run_lock(directory) as lease:
        store = RunStore.open(directory, lease=lease)
    with pytest.raises(BenchmarkError) as caught:
        create_archive_locked(directory, foreign / 'archive.tar.gz', store=store, lease=lease)
    assert caught.value.code == 'writer_lease_expired'


def test_archive_refuses_legacy_layout_and_stale_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema version one alone cannot turn a legacy report into a current archive."""
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    write_json(legacy / 'report.json', {'schema_version': 1})
    with pytest.raises(BenchmarkError) as caught:
        create_archive(legacy, tmp_path / 'legacy.tar.gz')
    assert caught.value.code == 'unsupported_run_format'

    directory = complete_run(tmp_path, monkeypatch)
    report = read_json(directory / 'report.json')
    report['run']['status'] = 'failed'
    write_json(directory / 'report.json', report)
    with pytest.raises(BenchmarkError) as caught:
        build_manifest(directory)
    assert caught.value.code == 'stale_report'


def test_inventory_rejects_private_host_names_but_permits_role_owned_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository auth.json is source evidence; a loose host-like copy is not."""
    directory = complete_run(tmp_path, monkeypatch)
    role_source = next((directory / 'tasks').glob('*/attempts/*/triplets')) / '001/config/auth_base.json'
    role_source.parent.mkdir(parents=True, exist_ok=True)
    role_source.write_text('{"fixture":true}\n')
    assert any(
        entry['path'].endswith('/triplets/001/config/auth_base.json') for entry in build_manifest(directory)['files']
    )
    (directory / 'auth.json').write_text('{"private":true}\n')
    with pytest.raises(BenchmarkError) as caught:
        build_manifest(directory)
    assert caught.value.code == 'archive_private_state'


def test_tar_traversal_and_nonregular_members_never_extract(tmp_path: Path) -> None:
    """Archive verification validates names and member kinds before its temporary extraction."""
    path = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(path, 'w:gz') as output:
        link = tarfile.TarInfo('../escape')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        output.addfile(link)
        manifest = tarfile.TarInfo('manifest.json')
        manifest.size = 2
        output.addfile(manifest, io.BytesIO(b'{}'))
    with pytest.raises(BenchmarkError):
        verify_archive(path)
    assert not (tmp_path / 'escape').exists()
