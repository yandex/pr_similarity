"""Candidate exclusions preserve collection accounting, checkpoint integrity and resume."""

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from basket_collector.config import CollectorConfig
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    RejectedChangeRequest,
    RejectedRecord,
    RejectionReason,
)
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.pipeline import Pipeline, PipelineInterrupted, Shutdown
from basket_collector.progress import Progress
from basket_collector.remote import ChangeRejected, RemoteError, RemoteInterrupted
from basket_collector.sources import change_id
from basket_collector.storage import StorageError, read_checkpoint, read_document, stage_sha256
from test_contracts import _collect
from test_gitlab_source import configured, merged, project
from test_pipeline import TIME, FakeGitHub, FakeGitRepository, _config, _filter_one, _producer


def rejection(number: int = 2) -> RejectedRecord:
    """Keep a stable source identity without inventing missing revision evidence."""
    return RejectedRecord(
        id=change_id('github', 'https://github.com', '123', 'pull_request', number),
        change_request=RejectedChangeRequest(
            kind='pull_request',
            repository='owner/repo',
            number=number,
            url=f'https://github.com/owner/repo/pull/{number}',
            title='No commits',
            author=None,
        ),
        reasons=[
            RejectionReason(
                code='source_commits_unavailable',
                message='No source commits',
                observed=0,
                expected='at least one source commit',
            )
        ],
    )


class RejectingGitHub(FakeGitHub):
    """Make one candidate uncollectable, independently of global API failures."""

    def __init__(self, *, shutdown: Shutdown | None = None, failure: RemoteError | None = None) -> None:
        """Record requests and optional interruption/failure after the rejected candidate."""
        super().__init__()
        self.requested: list[int] = []
        self.shutdown = shutdown
        self.failure = failure

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Reject candidate two while keeping source failures on candidate three fatal."""
        self.requested.append(number)
        if number == 2:
            raise ChangeRejected(rejection(number))
        if number == 3 and self.shutdown is not None:
            self.shutdown.request(2, None)
            raise RemoteInterrupted('fixture stop')
        if number == 3 and self.failure is not None:
            raise self.failure
        return super().get_pull_request(repository, number)

    def list_closed_prs(self, repository: str, *, start_page: int = 1) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        """Expose the same ordered population through date-range discovery."""
        yield 1, [{'number': n, 'merged_at': TIME, 'base': {'ref': 'develop'}} for n in (1, 2, 3)]


def setup(tmp_path: Path) -> CollectorConfig:
    """Select a deterministic three-candidate population."""
    config = _config(tmp_path)
    return config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [1, 2, 3]})})


def pipeline(
    config: CollectorConfig,
    client: RejectingGitHub,
    *,
    shutdown: Shutdown | None = None,
    progress: Progress | None = None,
) -> Pipeline:
    """Use real checkpoint coordination with frozen producer and offline source/Git."""
    return Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=client),
        git_repository=FakeGitRepository(),
        shutdown=shutdown,
        progress=progress,
    )


@pytest.mark.parametrize('selection', ['change_ids', 'date_range'])
def test_collect_rejection_continues_basket_and_preserves_progress_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    """Both collection modes produce identical semantic output across display modes."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    hashes = []
    for mode in ('off', 'plain', 'auto'):
        config = setup(tmp_path / mode)
        if selection == 'date_range':
            config = config.model_copy(
                update={
                    'collect': config.collect.model_copy(
                        update={
                            'selection': 'date_range',
                            'change_ids': None,
                            'merged_from': '2026-01-01T00:00:00Z',
                            'merged_before': '2026-01-03T00:00:00Z',
                        }
                    )
                }
            )
        client = RejectingGitHub()
        stream = io.StringIO()
        assert pipeline(config, client, progress=Progress(mode, stream=stream)).run('basket') == 0
        output = Path(config.output_dir)
        collected = read_document(output / 'collected.json', CollectDocument)
        assert [record.change_request.number for record in collected.tasks] == [1, 3]
        assert collected.rejected == [rejection()]
        assert collected.meta.counts.model_dump() == {
            'input': 3,
            'succeeded': 2,
            'rejected': 1,
            'failed': 0,
            'pending': 0,
        }
        filtered = read_document(output / 'filtered.json', FilterDocument)
        basket = read_document(output / 'basket.json', BasketDocument)
        assert filtered.meta.counts.input == 2
        assert {task.change_request.number for task in basket.tasks} == {1, 3}
        assert not basket.meta.partial
        hashes.append([stage_sha256(document) for document in (collected, filtered, basket)])
        if mode != 'off':
            assert 'rejected=1' in stream.getvalue()
        assert pipeline(config, client).run('collect') == 0
        assert client.requested == [1, 2, 3]
    assert hashes[0] == hashes[1] == hashes[2]


@pytest.mark.parametrize('commit_status', [200, 401, 403, 404, 429, 500])
def test_gitlab_pipeline_skips_only_successful_empty_commit_response(tmp_path: Path, commit_status: int) -> None:
    """Real provider normalization distinguishes no commits from unavailable API data."""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Serve two MRs, with empty or failed commit lookup for the second."""
        path = request.url.path
        requests.append(path)
        status = 200
        if path.endswith('/commits'):
            data = [{'id': 'c' * 40}] if '/41/' in path else []
            if '/42/' in path:
                status = commit_status
        elif path.endswith('/versions'):
            data = [{'id': 1, 'base_commit_sha': 'a' * 40, 'start_commit_sha': 'b' * 40, 'head_commit_sha': 'c' * 40}]
        elif path.endswith('/merge_requests/41') or path.endswith('/merge_requests/42'):
            number = int(path.rsplit('/', 1)[1])
            data = merged()
            data.update(iid=number, web_url=project()['web_url'] + f'/-/merge_requests/{number}')
        else:
            data = project()
        return httpx.Response(status, json=data, headers={'x-next-page': ''})

    remote = configured(respond)
    config = setup(tmp_path)
    config = config.model_copy(
        update={'source': remote.config, 'collect': config.collect.model_copy(update={'change_ids': [41, 42]})}
    )
    try:
        run = Pipeline(config, producer=_producer(), remote=remote)
        output = Path(config.output_dir)
        if commit_status == 200:
            assert run.run('collect') == 0
            document = read_document(output / 'collected.json', CollectDocument)
            assert [record.change_request.number for record in document.tasks] == [41]
            assert [record.change_request.number for record in document.rejected] == [42]
            assert document.meta.counts.rejected == 1 and document.meta.counts.pending == 0
            assert document.rejected[0].id == change_id('gitlab', remote.config.server_url, '123', 'merge_request', 42)
        elif commit_status == 401:
            with pytest.raises(RemoteError) as caught:
                run.run('collect')
            assert caught.value.code == 'source_auth_error'
            document, _ = read_checkpoint(output / '.work/collect.json', CollectDocument)
            assert document.meta.status == 'failed' and document.meta.counts.pending == 1
        else:
            assert run.run('collect') == 3
            document = read_document(output / 'collected.json', CollectDocument)
            assert document.meta.status == 'completed'
            assert document.rejected == [] and document.meta.counts.failed == 1
            assert document.failed[0].identity.number == 42
        assert sum(path.endswith('/42/commits') for path in requests) == 1
    finally:
        remote.client.close()


def test_interruption_after_rejection_resumes_only_pending_candidates(tmp_path: Path) -> None:
    """Resume uses accepted and rejected outcomes as one completed queue prefix."""
    config = setup(tmp_path)
    shutdown = Shutdown()
    with pytest.raises(PipelineInterrupted):
        pipeline(config, RejectingGitHub(shutdown=shutdown), shutdown=shutdown).run('collect')
    output = Path(config.output_dir)
    saved, state = read_checkpoint(output / '.work/collect.json', CollectDocument)
    assert saved.meta.status == 'interrupted'
    assert saved.meta.counts.succeeded == saved.meta.counts.rejected == saved.meta.counts.pending == 1
    assert saved.rejected == [rejection()] and state['numbers'] == [1, 2, 3]
    resumed = RejectingGitHub()
    assert pipeline(config, resumed).run('collect', retry_failed=True) == 0
    assert resumed.requested == [3]
    completed = read_document(output / 'collected.json', CollectDocument)
    assert completed.rejected == saved.rejected
    fresh_config = setup(tmp_path / 'fresh')
    assert pipeline(fresh_config, RejectingGitHub()).run('collect') == 0
    fresh = read_document(Path(fresh_config.output_dir) / 'collected.json', CollectDocument)
    assert stage_sha256(completed) == stage_sha256(fresh)


@pytest.mark.parametrize(
    'code,retryable',
    [
        ('source_auth_error', True),
        ('source_transport_error', True),
        ('source_rate_limit', True),
        ('source_changed', False),
        ('invalid_source_metadata', False),
        ('source_commits_unavailable', False),
    ],
)
def test_global_source_errors_are_not_candidate_skips(tmp_path: Path, code: str, retryable: bool) -> None:
    """Distinguish fatal shared access and identity errors from exhausted record failures."""
    config = setup(tmp_path)
    if code not in {'source_auth_error', 'source_changed'}:
        assert (
            pipeline(
                config, RejectingGitHub(failure=RemoteError('fixture failure', code=code, retryable=retryable))
            ).run('collect')
            == 3
        )
        document = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
        assert document.meta.status == 'completed' and document.meta.counts.failed == 1
        assert document.failed[0].error.code == code and document.rejected == [rejection()]
        return
    with pytest.raises(RemoteError, match='fixture fatal'):
        pipeline(config, RejectingGitHub(failure=RemoteError('fixture fatal', code=code, retryable=retryable))).run(
            'collect'
        )
    output = Path(config.output_dir)
    saved, _ = read_checkpoint(output / '.work/collect.json', CollectDocument)
    assert saved.meta.status == 'failed' and saved.meta.error is not None
    assert saved.meta.error.code == code and saved.meta.error.retryable == retryable
    assert saved.meta.counts.pending == 1 and saved.rejected == [rejection()]
    resumed = RejectingGitHub()
    if retryable:
        assert pipeline(config, resumed).run('collect', retry_failed=True) == 0
        assert resumed.requested == [3]
    else:
        with pytest.raises(StorageError):
            pipeline(config, resumed).run('collect')
        assert resumed.requested == []


def test_all_candidates_rejected_keeps_empty_stage_failure_and_reasons(tmp_path: Path) -> None:
    """An empty usable population stays an error without losing the rejected MR."""
    config = setup(tmp_path)
    config = config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [2]})})
    assert pipeline(config, RejectingGitHub()).run('collect') == 3
    saved = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert saved.rejected == [rejection()]
    assert saved.meta.counts.succeeded == saved.meta.counts.pending == 0
    assert saved.meta.counts.rejected == 1
    assert saved.meta.status == 'completed'


@pytest.mark.parametrize(
    'corruption', ['duplicate', 'overlap', 'foreign', 'hole', 'counts', 'phase', 'kind', 'identity']
)
def test_rejected_checkpoint_corruption_blocks_resume_before_api(tmp_path: Path, corruption: str) -> None:
    """Rejected outcomes must remain a disjoint, valid part of the frozen prefix."""
    config = setup(tmp_path)
    shutdown = Shutdown()
    with pytest.raises(PipelineInterrupted):
        pipeline(config, RejectingGitHub(shutdown=shutdown), shutdown=shutdown).run('collect')
    work = Path(config.output_dir) / '.work/collect.json'
    payload = json.loads(work.read_text())
    if corruption == 'duplicate':
        payload['rejected'].append(payload['rejected'][0])
    elif corruption in ('overlap', 'foreign', 'hole'):
        payload['rejected'] = [rejection({'overlap': 1, 'foreign': 4, 'hole': 3}[corruption]).model_dump(mode='json')]
    elif corruption == 'counts':
        payload['meta']['counts'].update(rejected=0, pending=2)
    elif corruption == 'phase':
        payload['checkpoint']['phase'] = 'discovery'
    elif corruption == 'kind':
        payload['rejected'][0]['change_request']['kind'] = 'merge_request'
    else:
        payload['rejected'][0]['id'] = 'change:' + 'f' * 64
    work.write_text(json.dumps(payload))
    client = RejectingGitHub()
    with pytest.raises(StorageError):
        pipeline(config, client).run('collect')
    assert client.requested == [] and client.calls == 0
    assert not (Path(config.output_dir) / 'collected.json').exists()


def test_collect_without_rejections_has_explicit_empty_v2_outcomes() -> None:
    """Persist explicit outcome partitions even when collection has no rejected records."""
    original = _collect()
    restored = CollectDocument.model_validate(original)
    assert restored.rejected == []
    dumped = restored.model_dump(mode='json')
    assert dumped['failed'] == [] and dumped['rejected'] == []
    assert stage_sha256(restored) == stage_sha256(CollectDocument.model_validate(dumped))
