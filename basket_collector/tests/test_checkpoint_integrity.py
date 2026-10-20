"""Reject corrupted resume queues before any external operation or publication."""

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from basket_collector.checkpoints import validate_checkpoint
from basket_collector.config import CollectorConfig
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    TasksDocument,
)
from basket_collector.github import GitHubError
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.pipeline import Pipeline
from basket_collector.storage import (
    StageStore,
    StorageError,
    atomic_write,
    read_checkpoint,
    sha256,
)
from test_contracts import _basket, _filter, _tasks
from test_pipeline import FakeGitHub, FakeGitRepository, _config, _filter_one, _producer
from test_storage import _document


class _FailSecondPR(FakeGitHub):
    """Leave a genuine checkpoint after collecting the first queued PR."""

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Fail the second operation so the first record is durably saved."""
        if number == 2:
            raise GitHubError('temporary fixture failure', code='github_auth_error', retryable=True)
        return super().get_pull_request(repository, number)


def _interrupted_collection(tmp_path: Path) -> tuple[CollectorConfig, Path, dict[str, Any]]:
    """Create a genuine interrupted queue with its first PR already saved."""
    config = _config(tmp_path)
    config = config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [1, 2]})})
    with pytest.raises(GitHubError, match='temporary fixture failure'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=_FailSecondPR())).run(
            'collect'
        )
    work = Path(config.output_dir) / '.work' / 'collect.json'
    payload = json.loads(work.read_text(encoding='utf-8'))
    return config, work, payload


def test_shortened_collect_queue_fails_before_api_or_publication(tmp_path: Path) -> None:
    """A saved subset must not replace the configured two-PR population."""
    config, work, payload = _interrupted_collection(tmp_path)
    payload['checkpoint']['numbers'] = [1]
    work.write_text(json.dumps(payload), encoding='utf-8')
    github = FakeGitHub()
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect')
    assert github.calls == 0
    assert not (Path(config.output_dir) / 'collected.json').exists()


@pytest.mark.parametrize(
    'mutation',
    [
        lambda data: data['checkpoint'].update(numbers=[True, 2]),
        lambda data: data['checkpoint'].update(numbers=[1.0, 2]),
        lambda data: data['checkpoint'].update(numbers=[0, 2]),
        lambda data: data['checkpoint'].update(numbers=[1, 1]),
        lambda data: data['checkpoint'].update(numbers=[2, 1]),
        lambda data: data['checkpoint'].update(numbers=[1, 3]),
        lambda data: data['checkpoint'].update(numbers=[]),
        lambda data: data['checkpoint'].update(phase='unknown'),
        lambda data: data['checkpoint'].update(phase='ready'),
        lambda data: data['checkpoint'].update(extra='unrecognized'),
        lambda data: data['checkpoint'].update(next_page=True),
        lambda data: data['checkpoint'].update(next_page=1.0),
        lambda data: data['checkpoint'].update(matches=[['2026-01-01T00:00:00Z', 1]]),
        lambda data: data['meta']['discovery'].update(matched=3),
    ],
    ids=[
        'bool',
        'float',
        'zero',
        'duplicate',
        'order',
        'foreign',
        'empty',
        'phase',
        'ready',
        'extra',
        'bool-cursor',
        'float-cursor',
        'foreign-discovery',
        'matched-count',
    ],
)
def test_corrupt_collect_state_is_rejected_before_external_work(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    """Reject invalid queue fields and phase transitions before touching GitHub."""
    config, work, payload = _interrupted_collection(tmp_path)
    mutation(payload)
    atomic_write(work, payload)
    github = FakeGitHub()
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect')
    assert github.calls == 0
    assert not (Path(config.output_dir) / 'collected.json').exists()


def test_coherently_shortened_queue_still_must_match_frozen_config(tmp_path: Path) -> None:
    """Matching edited counters cannot legitimize omission of a configured PR."""
    config, work, payload = _interrupted_collection(tmp_path)
    payload['checkpoint']['numbers'] = [1]
    payload['meta']['counts'].update(input=1, pending=0)
    payload['meta']['discovery'].update(matched=1, selected=1)
    atomic_write(work, payload)
    with pytest.raises(StorageError, match='configured PR IDs'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )


def test_saved_records_must_follow_frozen_queue(tmp_path: Path) -> None:
    """Reject genuine PR snapshots persisted in a different queue order."""
    config, work, payload = _interrupted_collection(tmp_path)
    second = deepcopy(payload['tasks'][0])
    second.update(id='change:b76ae00445934fd9558d883e581fac684789154c92d1e1e58a8e6c068b26138f')
    second['change_request'].update(number=2, url='https://github.com/owner/repo/pull/2')
    payload['tasks'].insert(0, second)
    payload['meta']['counts'].update(succeeded=2, pending=0)
    atomic_write(work, payload)
    with pytest.raises(StorageError, match='frozen queue order|attempt journal'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )


def _dated_document() -> CollectDocument:
    """Build a capped date-range checkpoint with the selected record saved."""
    payload = _document(status='interrupted').model_dump(mode='json')
    payload['meta']['settings']['collect'] = {
        'selection': 'date_range',
        'merged_from': '2026-01-01T00:00:00Z',
        'merged_before': '2026-02-01T00:00:00Z',
        'max_changes': 1,
    }
    payload['meta']['settings_hash'] = sha256(payload['meta']['settings'])
    payload['meta']['discovery'].update(matched=2, selected=1, capped=True)
    return CollectDocument.model_validate(payload)


def _dated_state() -> dict[str, Any]:
    """Build the final sorted match population and its capped queue."""
    return {
        'phase': 'records',
        'next_page': 3,
        'numbers': [1],
        'matches': [['2026-01-01T00:00:00Z', 1], ['2026-01-02T00:00:00Z', 2]],
    }


@pytest.mark.parametrize(
    'mutation',
    [
        lambda data: data.update(numbers=[2]),
        lambda data: data.update(matches=[['2026-01-01T00:00:00Z', 1]]),
        lambda data: data['matches'].reverse(),
        lambda data: data['matches'][1].__setitem__(1, 1),
        lambda data: data['matches'][1].__setitem__(1, True),
        lambda data: data['matches'][1].__setitem__(1, 2.0),
        lambda data: data['matches'][1].__setitem__(0, '2026-02-01T00:00:00Z'),
        lambda data: data['matches'][1].__setitem__(0, 'malformed'),
    ],
    ids=['cap-order', 'short-matches', 'match-order', 'duplicate', 'bool', 'float', 'out-of-range', 'timestamp'],
)
def test_date_range_checkpoint_matches_and_cap_are_validated(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    """Reject tampered discovery matches before resuming a frozen date queue."""
    document, state = _dated_document(), _dated_state()
    mutation(state)
    store = StageStore(tmp_path, 'collect')
    atomic_write(store.work_path, {**document.model_dump(mode='json'), 'checkpoint': state})
    with pytest.raises(StorageError, match='checkpoint'):
        read_checkpoint(store.work_path, CollectDocument)


@pytest.mark.parametrize('phase', ['repository', 'discovery', 'records', 'ready'])
def test_valid_collect_phases_preserve_resumable_state(tmp_path: Path, phase: str) -> None:
    """Accept initial, partially discovered, frozen and publication checkpoints."""
    document, state = _dated_document(), _dated_state()
    state['phase'] = phase
    if phase in ('repository', 'discovery'):
        payload = document.model_dump(mode='json')
        payload['tasks'] = []
        payload['meta']['counts'].update(input=0, succeeded=0)
        payload['meta']['discovery'].update(matched=0, selected=0, capped=False)
        state['numbers'] = []
        if phase == 'repository':
            state.update(matches=[], next_page=1)
            payload['meta']['repository']['default_branch'] = None
        document = CollectDocument.model_validate(payload)
    store = StageStore(tmp_path, 'collect')
    store.checkpoint(document, state)
    restored, restored_state = read_checkpoint(store.work_path, CollectDocument)
    assert restored == document
    assert restored_state == {**state, 'attempts': {}, 'retry_ids': []}


@pytest.mark.parametrize(
    'fixture, model, state',
    [
        (_filter, FilterDocument, {'phase': 'unknown'}),
        (_filter, FilterDocument, {'phase': 'local', 'extra': 1}),
        (_tasks, TasksDocument, {'phase': 'unknown'}),
        (
            _tasks,
            TasksDocument,
            {'retry_attempts': {'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea': True}},
        ),
        (
            _tasks,
            TasksDocument,
            {'retry_attempts': {'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea': 1.0}},
        ),
        (
            _tasks,
            TasksDocument,
            {'retry_attempts': {'change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea': -1}},
        ),
        (_tasks, TasksDocument, {'retry_attempts': {'issue:' + 'a' * 64: 1}}),
        (_tasks, TasksDocument, {'retry_attempts': {'change:invalid': 1}}),
        (_tasks, TasksDocument, {'retry_attempts': {}, 'extra': 'unknown'}),
        (_basket, BasketDocument, {'phase': 'unknown'}),
        (_basket, BasketDocument, {'phase': 'ready', 'extra': 1}),
    ],
)
def test_all_stage_checkpoint_shapes_are_strict(
    tmp_path: Path,
    fixture: Callable[[], dict[str, Any]],
    model: type[FilterDocument] | type[TasksDocument] | type[BasketDocument],
    state: dict[str, Any],
) -> None:
    """Validate stages, phase values, retry types and extra state keys on read."""
    payload = fixture()
    payload['meta'].update(status='interrupted', completed_at=None)
    payload['checkpoint'] = state
    path = tmp_path / 'checkpoint.json'
    atomic_write(path, payload)
    with pytest.raises(StorageError, match='checkpoint'):
        read_checkpoint(path, model)


def test_writer_rejects_invalid_state_without_replacing_checkpoint(tmp_path: Path) -> None:
    """A failed validation must retain the last durable resumable state."""
    document, state = _dated_document(), _dated_state()
    store = StageStore(tmp_path, 'collect')
    store.checkpoint(document, state)
    original = store.work_path.read_bytes()
    with pytest.raises(StorageError, match='checkpoint'):
        store.checkpoint(document, {**state, 'numbers': []})
    assert store.work_path.read_bytes() == original


def test_generate_requires_v2_attempt_state() -> None:
    """Reject obsolete retry counters and retain explicit v2 phases."""
    document = TasksDocument.model_validate(_tasks())
    with pytest.raises(ValueError):
        validate_checkpoint(document, {'retry_attempts': {}})
    assert validate_checkpoint(document, {'phase': 'ready'}) == {'phase': 'ready'}


def _ready_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> tuple[CollectorConfig, Path]:
    """Convert a genuine completed stage into its interrupted ready state."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    config = config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [1, 2]})})
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
        ).run('basket')
        == 0
    )
    store = StageStore(Path(config.output_dir), stage)
    payload = json.loads(store.path.read_text(encoding='utf-8'))
    payload['meta'].update(status='interrupted', completed_at=None)
    payload['checkpoint'] = {'phase': 'ready'}
    atomic_write(store.work_path, payload)
    store.path.unlink()
    return config, store.work_path


@pytest.mark.parametrize('stage', ['filter', 'generate'])
def test_truncated_ready_stage_cannot_publish_empty_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """An internally coherent ready document must retain its predecessor population."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, stage)
    payload = json.loads(work.read_text(encoding='utf-8'))
    payload['tasks'] = []
    payload['meta']['counts'].update(input=0, succeeded=0)
    atomic_write(work, payload)
    github = FakeGitHub()
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run(stage)
    assert github.calls == 0
    assert not StageStore(Path(config.output_dir), stage).path.exists()


@pytest.mark.parametrize('stage', ['filter', 'generate', 'export'])
def test_ready_stage_source_edits_are_rejected_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Saved source fields must match verified records even when IDs are unchanged."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, stage)
    payload = json.loads(work.read_text(encoding='utf-8'))
    payload['tasks'][0]['change_request']['title'] = 'Edited source title'
    atomic_write(work, payload)
    github = FakeGitHub()
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run(stage)
    assert github.calls == 0
    assert not StageStore(Path(config.output_dir), stage).path.exists()


@pytest.mark.parametrize('identity', ['change:' + 'f' * 64, 'change:' + 'e' * 64])
def test_opaque_retry_ids_must_belong_to_verified_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
) -> None:
    """Validate opaque IDs against the full predecessor rather than parsing their text."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, 'generate')
    payload = json.loads(work.read_text(encoding='utf-8'))
    payload['checkpoint'] = {'phase': 'records', 'retry_attempts': {identity: 1}}
    atomic_write(work, payload)
    calls: list[bool] = []

    def forbidden_llm() -> None:
        """Reject any generation client creation before checkpoint validation."""
        calls.append(True)
        raise AssertionError('invalid retry ID reached generation')

    pipeline = Pipeline(config, producer=_producer())
    monkeypatch.setattr(pipeline, '_llm', forbidden_llm)
    with pytest.raises(StorageError, match='checkpoint'):
        pipeline.run('generate')
    assert calls == []


def test_truncated_ready_export_cannot_publish_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Export counts and records must cover every successful generated task."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, 'export')
    payload = json.loads(work.read_text(encoding='utf-8'))
    assert len(payload['tasks']) == 2
    payload['tasks'].pop()
    payload['meta']['counts'].update(input=1, succeeded=1)
    atomic_write(work, payload)
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(config, producer=_producer()).run('export')
    assert not StageStore(Path(config.output_dir), 'export').path.exists()


def test_ready_export_omissions_must_match_failed_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A basket cannot claim omitted failures absent from its task predecessor."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, 'export')
    payload = json.loads(work.read_text(encoding='utf-8'))
    payload['meta'].update(
        partial=True, omitted_failed_ids=['change:5cf8b21e64346c447df4cea7af341b3693b88f60562e5fb1cfb13923fbc708ac']
    )
    atomic_write(work, payload)
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(config, producer=_producer()).run('export')


def test_ready_export_dataset_metadata_matches_frozen_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resumed projection must retain the configured dataset identity."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, 'export')
    payload = json.loads(work.read_text(encoding='utf-8'))
    payload['meta']['name'] = 'Edited dataset name'
    atomic_write(work, payload)
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(config, producer=_producer()).run('export')


@pytest.mark.parametrize('stage', ['filter', 'generate', 'export'])
@pytest.mark.parametrize('existing_result', [False, True], ids=['absent-result', 'existing-result'])
def test_invalid_completed_work_is_not_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, existing_result: bool
) -> None:
    """Crash recovery must validate predecessor content before replacing a result."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, stage)
    payload = json.loads(work.read_text(encoding='utf-8'))
    payload.pop('checkpoint')
    payload['meta'].update(status='completed', completed_at=payload['meta']['updated_at'])
    store = StageStore(Path(config.output_dir), stage)
    original = None
    if existing_result:
        store.path.symlink_to(store.publication._alias_target(store.stage))
        original = store.path.read_bytes()
    payload['tasks'][0]['change_request']['title'] = 'Tampered completed work'
    atomic_write(work, payload)
    work_bytes = work.read_bytes()
    github = FakeGitHub()
    with pytest.raises(StorageError, match='checkpoint'):
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run(stage)
    assert github.calls == 0
    assert work.exists() and work.read_bytes() == work_bytes
    if original is None:
        assert not store.path.exists()
    else:
        assert store.path.read_bytes() == original


def test_ready_export_rejects_changed_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A safe but edited cwd must not replace the saved task projection on resume."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, 'export')
    payload = json.loads(work.read_text(encoding='utf-8'))
    assert payload['tasks'][0]['workspace'] == '.'
    payload['tasks'][0]['workspace'] = 'another/directory'
    # This is a valid standalone basket; only the source projection can expose the edit.
    BasketDocument.model_validate({key: value for key, value in payload.items() if key != 'checkpoint'})
    atomic_write(work, payload)
    github = FakeGitHub()
    with pytest.raises(StorageError, match='basket records differ from successful task projection'):
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run('export')
    assert github.calls == 0
    assert not StageStore(Path(config.output_dir), 'export').path.exists()


@pytest.mark.parametrize('stage', ['collect', 'filter'])
def test_ready_export_upstream_coverage_matches_frozen_predecessors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Reject arithmetically valid coverage edits before publishing a resumed export."""
    config, work = _ready_checkpoint(tmp_path, monkeypatch, 'export')
    payload = json.loads(work.read_text())
    coverage = payload['meta']['coverage']
    coverage['discovery']['matched'] += 1
    coverage['discovery']['selected'] += 1
    coverage['collect']['input'] += 1
    if stage == 'collect':
        coverage['collect']['rejected'] += 1
    else:
        coverage['collect']['succeeded'] += 1
        coverage['filter']['input'] += 1
        coverage['filter']['rejected'] += 1
    BasketDocument.model_validate({key: value for key, value in payload.items() if key != 'checkpoint'})
    atomic_write(work, payload)
    with pytest.raises(StorageError, match='coverage'):
        Pipeline(config, producer=_producer()).run('export')
    assert not StageStore(Path(config.output_dir), 'export').path.exists()
