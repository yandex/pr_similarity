"""Durable state and identity tests; no external service is involved."""

import json
import os
from pathlib import Path

import pytest
from basket_collector.contracts import (
    ChangeRequest,
    CollectDocument,
    CollectMeta,
    CollectRecord,
    Counts,
    Discovery,
    JsonValue,
    Producer,
    Repository,
    Revisions,
    SourceRevisions,
    StageError,
    StageStatus,
)
from basket_collector.storage import (
    StageStore,
    StorageError,
    atomic_write,
    read_document,
    read_json,
    sha256,
    single_writer,
    stage_sha256,
)
from fixture_helpers import descriptor


def _producer() -> Producer:
    """Build a stable producer identity for artifact tests."""
    return Producer(
        version='0.1.0',
        implementation_sha256='a' * 64,
        dependencies_sha256='b' * 64,
        python_version='3.12.3',
        git_version='git version 2',
    )


def _document(*, status: StageStatus = 'completed', title: str = 'Example') -> CollectDocument:
    """Build a collect document with configurable status and PR title."""
    settings: dict[str, JsonValue] = {'collect': {'selection': 'change_ids', 'change_ids': [1]}}
    row = CollectRecord(
        id='change:3130fa6dacfd279c7741cf27960c8e69b406d699499bdafcb4b96588664b07ea',
        change_request=ChangeRequest(
            repository='o/r',
            number=1,
            url='https://github.com/o/r/pull/1',
            title=title,
            body='',
            author='alice',
            merged_at='2026-01-01T00:00:00Z',
            kind='pull_request',
        ),
        revisions=Revisions(
            source=SourceRevisions(
                base_sha='a' * 40,
                head_sha='b' * 40,
                merge_commit_sha='c' * 40,
                head_repository='o/r',
                commits_count=1,
                expected_base_sha=None,
                squash_commit_sha=None,
                diff_version_id=None,
                target_branch='develop',
            )
        ),
        source=descriptor('o/r', 1),
    )
    return CollectDocument(
        meta=CollectMeta(
            schema_version=2,
            status=status,
            input=None,
            settings=settings,
            settings_hash=sha256(settings),
            producer=_producer(),
            lineage=[],
            repository=Repository(
                path='o/r',
                web_url='https://github.com/o/r',
                default_branch='develop',
                provider='github',
                server_url='https://github.com',
                project_id='123',
            ),
            started_at='2026-01-01T00:00:00Z',
            updated_at='2026-01-01T00:00:01Z',
            completed_at='2026-01-01T00:00:02Z' if status == 'completed' else None,
            counts=Counts(input=1, succeeded=1, rejected=0, failed=0, pending=0),
            error=None,
            discovery=Discovery(matched=1, selected=1, capped=False),
        ),
        tasks=[row],
    )


def test_stage_hash_ignores_only_execution_times() -> None:
    """Exclude execution timestamps but retain semantic content in stage hashes."""
    original = _document()
    changed_times = original.model_copy(
        update={
            'meta': original.meta.model_copy(
                update={
                    'started_at': '2026-02-01T00:00:00Z',
                    'updated_at': '2026-02-01T00:00:01Z',
                    'completed_at': '2026-02-01T00:00:02Z',
                }
            )
        }
    )
    assert stage_sha256(original) == stage_sha256(changed_times)
    assert stage_sha256(original) != stage_sha256(_document(title='Changed'))


def test_checkpoint_reuses_progress_and_keeps_previous_result_on_overwrite(tmp_path: Path) -> None:
    """Resume checkpoint progress while preserving a previous published result."""
    store = StageStore(tmp_path, 'collect')
    old = _document()
    store.publish(old)
    interrupted = _document(status='interrupted', title='New')
    state = {'phase': 'records', 'next_page': 1, 'matches': [], 'numbers': [1]}
    store.checkpoint(interrupted, state)
    loaded, loaded_state = store.load(
        CollectDocument,
        settings=old.meta.settings,
        producer=_producer(),
        input_receipt=None,
    )
    assert loaded == interrupted
    assert loaded_state == {**state, 'attempts': {}, 'retry_ids': []}
    assert read_document(store.path, CollectDocument) == old
    assert store.load(
        CollectDocument,
        settings=old.meta.settings,
        producer=_producer(),
        input_receipt=None,
        overwrite=True,
    ) == (None, None)
    assert read_document(store.path, CollectDocument) == old


def test_mismatch_requires_overwrite(tmp_path: Path) -> None:
    """Require overwrite when saved settings differ from current settings."""
    store = StageStore(tmp_path, 'collect')
    store.publish(_document())
    with pytest.raises(StorageError, match='OVERWRITE=1'):
        store.load(
            CollectDocument,
            settings={'collect': {}, 'source': {'provider': 'github', 'project': 'x/y'}},
            producer=_producer(),
            input_receipt=None,
        )


@pytest.mark.parametrize('location', ['result', 'checkpoint', 'completed-work'])
@pytest.mark.parametrize(
    'field, value',
    [
        ('schema_version', 1),
        ('schema_version', True),
        ('schema_version', 1.0),
        ('schema_family', 'unsupported.family'),
    ],
    ids=['unsupported-version', 'boolean-version', 'float-version', 'unknown-family'],
)
def test_unsupported_schema_identity_cannot_be_overwritten(
    tmp_path: Path, location: str, field: str, value: float | str
) -> None:
    """Reject unknown schemas in saved results, checkpoints, and completed work."""
    store = StageStore(tmp_path, 'collect')
    payload = _document(status='interrupted' if location == 'checkpoint' else 'completed').model_dump(mode='json')
    payload['meta'][field] = value
    if location == 'checkpoint':
        payload['checkpoint'] = {'phase': 'records'}
    atomic_write(store.path if location == 'result' else store.work_path, payload)
    with pytest.raises(StorageError):
        store.load(CollectDocument, settings=_document().meta.settings, producer=_producer(), input_receipt=None)
    if field == 'schema_version' or location == 'result':
        with pytest.raises(StorageError):
            store.load(
                CollectDocument,
                settings=_document().meta.settings,
                producer=_producer(),
                input_receipt=None,
                overwrite=True,
            )


@pytest.mark.parametrize('code', ['source_changed', 'llm_configuration', 'github_auth_error'])
def test_nonretryable_checkpoint_errors_require_overwrite(tmp_path: Path, code: str) -> None:
    """Require rebuilding every checkpoint explicitly marked nonretryable."""
    store = StageStore(tmp_path, 'collect')
    document = _document(status='failed')
    document = document.model_copy(
        update={
            'meta': document.meta.model_copy(update={'error': StageError(code=code, message='failed', retryable=False)})
        }
    )
    store.checkpoint(document, {'phase': 'records', 'next_page': 1, 'matches': [], 'numbers': [1]})
    with pytest.raises(StorageError, match=f'{code}.*OVERWRITE=1'):
        store.load(CollectDocument, settings=document.meta.settings, producer=_producer(), input_receipt=None)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    """Reject duplicate object keys while reading JSON."""
    path = tmp_path / 'bad.json'
    path.write_text('{"meta": 1, "meta": 2}', encoding='utf-8')
    with pytest.raises(StorageError, match='duplicate JSON key'):
        read_json(path)


def test_atomic_write_and_single_writer(tmp_path: Path) -> None:
    """Replace JSON atomically and allow only one writer at a time."""
    path = tmp_path / 'data.json'
    atomic_write(path, {'value': 1})
    with single_writer(tmp_path):
        with pytest.raises(StorageError, match='another basket collector'), single_writer(tmp_path):
            pass
        atomic_write(path, {'value': 2})
    assert json.loads(path.read_text(encoding='utf-8')) == {'value': 2}


def test_completed_work_is_recovered_after_crash_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover completed work left behind by a crash before promotion."""
    store = StageStore(tmp_path, 'collect')
    previous = _document()
    replacement = _document(title='Replacement')
    store.publish(previous)
    store.checkpoint(
        _document(status='interrupted', title='Replacement'),
        {'phase': 'ready', 'next_page': 1, 'matches': [], 'numbers': [1]},
    )
    original_replace = os.replace

    def fail_promotion(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        """Simulate a crash when completed work is moved into place."""
        if Path(target) == store.publication.current:
            raise OSError('simulated crash before promotion')
        original_replace(source, target)

    monkeypatch.setattr(os, 'replace', fail_promotion)
    with pytest.raises(OSError, match='simulated crash'):
        store.publish(replacement)
    assert read_document(store.path, CollectDocument) == previous

    monkeypatch.setattr(os, 'replace', original_replace)
    resumed, state = store.load(
        CollectDocument,
        settings=replacement.meta.settings,
        producer=_producer(),
        input_receipt=None,
    )
    assert resumed == replacement
    assert state is None
    assert read_document(store.path, CollectDocument) == replacement
