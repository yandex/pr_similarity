"""Verify record failures, explicit retry passes and complete v2 queue accounting."""

import json
import threading
from pathlib import Path

import httpx
import pytest
from basket_collector.attempts import AttemptRunner
from basket_collector.config import FilterConfig
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    CollectRecord,
    FilterDocument,
    FilterRecord,
    Repository,
)
from basket_collector.git import GitInfrastructureError, GitRepository
from basket_collector.github import GitHubError
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.llm import LLMClient
from basket_collector.pipeline import Pipeline
from basket_collector.remote import RemoteError, RemoteVCS
from basket_collector.storage import StageStore, StorageError, read_checkpoint, read_document
from test_collect_rejections import RejectingGitHub, pipeline
from test_pipeline import FakeGitHub, FakeGitRepository, _config, _filter_one, _producer, auth_generation


def test_collect_failure_does_not_stop_remaining_records(tmp_path: Path) -> None:
    """An exhausted API failure remains identity-only while later records still complete."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3, 4]
    client = RejectingGitHub(failure=RemoteError('fixture unavailable'))
    assert pipeline(config, client).run('collect') == 3
    document = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert document.meta.status == 'completed'
    assert [record.change_request.number for record in document.tasks] == [1, 4]
    assert [record.change_request.number for record in document.rejected] == [2]
    assert [failure.identity.number for failure in document.failed] == [3]
    assert document.meta.counts.pending == 0 and document.meta.error is None
    before = list(client.requested)
    assert pipeline(config, client).run('collect') == 3
    assert client.requested == before
    client.failure = None
    assert pipeline(config, client).run('collect', retry_failed=True) == 0
    assert client.requested == [1, 2, 3, 4, 3]
    retried = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert [record.change_request.number for record in retried.tasks] == [1, 3, 4]
    assert not retried.failed


@pytest.mark.parametrize('wrong_identity', [False, True])
def test_unmerged_record_is_not_a_shared_identity_failure(tmp_path: Path, wrong_identity: bool) -> None:
    """A local eligibility failure continues the queue while a response for another PR stops it."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3]
    requested: list[int] = []

    class Source(FakeGitHub):
        """Return either an unmerged PR or a confirmed wrong identity for one requested record."""

        def get_pull_request(self, repository: str, number: int) -> dict[str, object]:
            """Change only the field that distinguishes record eligibility from adapter identity."""
            requested.append(number)
            row = super().get_pull_request(repository, number)
            if number == 2:
                row.update({'number': 99} if wrong_identity else {'merged_at': None})
            return row

    runner = Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=Source()))
    output = Path(config.output_dir)
    if wrong_identity:
        with pytest.raises(GitHubError) as failure:
            runner.run('collect')
        assert failure.value.code == 'source_identity_changed' and requested == [1, 2]
        document, _ = read_checkpoint(output / '.work/collect.json', CollectDocument)
        assert document.meta.counts.pending == 2 and document.meta.status == 'failed'
    else:
        assert runner.run('collect') == 3
        document = read_document(output / 'collected.json', CollectDocument)
        assert [item.change_request.number for item in document.tasks] == [1, 3]
        assert document.failed[0].identity.number == 2 and document.meta.error is None
        assert requested == [1, 2, 3]


@pytest.mark.parametrize('limit', [1, 3])
def test_upstream_retry_compares_selection_before_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    """An unchanged sample reuses generation; changed selection requires explicit acceptance."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3, 4]
    config.filter.selection.shuffle = False
    config.filter.selection.limit = limit
    client = RejectingGitHub(failure=RemoteError('fixture unavailable'))
    assert pipeline(config, client).run('basket') == 3
    output = Path(config.output_dir)
    assert not (output / 'basket.json').exists()
    requests = list(client.requested)
    assert pipeline(config, client).run('basket', allow_partial=True) == 0
    assert client.requested == requests
    original = (output / 'basket.json').read_bytes()
    basket = read_document(output / 'basket.json', BasketDocument)
    assert basket.meta.partial and basket.meta.coverage.collect.failed == 1
    assert pipeline(config, client).run('basket') == 0
    assert (output / 'basket.json').read_bytes() == original
    client.failure = None
    result = pipeline(config, client).run('basket', retry_failed=True)
    if limit == 1:
        assert result == 0
        updated = read_document(output / 'basket.json', BasketDocument)
        assert not updated.meta.partial and updated.meta.selection.fingerprint == basket.meta.selection.fingerprint
        assert updated.tasks[0].provenance.generation.reused_from is not None
    else:
        assert result == 3 and (output / 'basket.json').read_bytes() == original
        requests = list(client.requested)
        assert pipeline(config, client).run('basket', reselect=True) == 0
        assert client.requested == requests
        updated = read_document(output / 'basket.json', BasketDocument)
        assert [item.change_request.number for item in updated.tasks] == [1, 3, 4]
    candidates = read_document(output / 'filtered.json', FilterDocument)
    assert len(candidates.tasks) == 3


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('retry_failed', [False, True])
@pytest.mark.parametrize('allow_partial', [False, True])
@pytest.mark.parametrize('reselect', [False, True])
def test_all_flag_combinations_on_new_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overwrite: bool,
    retry_failed: bool,
    allow_partial: bool,
    reselect: bool,
) -> None:
    """Reject contradictory intents before API work and accept all other initial-run combinations."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    client = RejectingGitHub()
    runner = pipeline(config, client)
    arguments = {
        'overwrite': overwrite,
        'retry_failed': retry_failed,
        'allow_partial': allow_partial,
        'reselect': reselect,
    }
    if overwrite and (retry_failed or reselect):
        with pytest.raises(ValueError):
            runner.run('basket', **arguments)
        assert client.calls == 0 and not Path(config.output_dir).exists()
    else:
        assert runner.run('basket', **arguments) == 0


def test_reselect_can_reuse_tasks_returning_from_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrinking and expanding selection uses complete cached inputs without repeating successful generation."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    config.collect.change_ids = [1, 3, 4]
    config.filter.selection.shuffle = False
    config.filter.selection.limit = 3
    client = RejectingGitHub()
    assert pipeline(config, client).run('basket') == 0
    config.filter.selection.limit = 1
    assert pipeline(config, client).run('basket', reselect=True) == 0

    def forbidden(*args: object, **kwargs: object) -> None:
        """Prove restored selection does not execute the generation operation again."""
        raise AssertionError('generation should use history')

    monkeypatch.setattr('basket_collector.pipeline.generate_record', forbidden)
    config.filter.selection.limit = 3
    assert pipeline(config, client).run('basket', reselect=True) == 0
    basket = read_document(Path(config.output_dir) / 'basket.json', BasketDocument)
    assert len(basket.tasks) == 3 and all(item.provenance.generation.reused_from for item in basket.tasks)


def test_filter_exhausts_each_record_without_a_failure_streak_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than ten native failures complete their budgets, preserve candidates and remain explicitly retryable."""
    config = _config(tmp_path)
    config.collect.change_ids = list(range(1, 15))
    config.filter.max_retries = 1
    config.filter.selection.shuffle = False
    config.filter.selection.limit = 1
    calls = []
    broken = [True]
    monkeypatch.setattr(AttemptRunner, '_wait_ready', lambda self, journal: None)

    def filter_record(
        record: CollectRecord,
        repository: GitRepository,
        remote: RemoteVCS,
        filter_config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord:
        """Produce one healthy candidate and a long run of expected native operation failures."""
        calls.append(record.change_request.number)
        if broken[0] and record.change_request.number != 1:
            raise GitInfrastructureError('fixture unknown native error')
        return _filter_one(record, repository, remote, filter_config, stop_event, hosting_repository=hosting_repository)

    def run(**flags: bool) -> int:
        """Create a fresh coordinator using the same frozen source and configured retry policy."""
        return Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
        ).run('basket', **flags)

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_record)
    assert run() == 3
    document = read_document(Path(config.output_dir) / 'filtered.json', FilterDocument)
    assert document.meta.status == 'completed' and document.meta.counts.pending == 0
    assert len(document.failed) == 13 and all(item.attempts == 2 for item in document.failed)
    assert calls.count(1) == 1 and all(calls.count(number) == 2 for number in range(2, 15))
    previous_calls = list(calls)
    assert run(allow_partial=True) == 0 and calls == previous_calls
    broken[0] = False
    assert run(retry_failed=True) == 0
    basket = read_document(Path(config.output_dir) / 'basket.json', BasketDocument)
    assert not basket.meta.partial and basket.meta.coverage.filter.succeeded == 14
    assert calls.count(1) == 1 and all(calls.count(number) == 3 for number in range(2, 15))


@pytest.mark.parametrize('crash', [False, True])
def test_generate_overwrite_rebinds_own_settings_without_reprocessing_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash: bool,
) -> None:
    """A changed model recalculates generation alone while preserving the old published basket."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    monkeypatch.setenv('BASKET_TEST_LLM_KEY', 'synthetic-fixture')
    config = _config(tmp_path)
    config.generation = auth_generation('openai_responses')
    assert config.generation.llm is not None
    config.generation.llm.model = 'first-model'
    requests = []
    github = FakeGitHub()

    def respond(request: httpx.Request) -> httpx.Response:
        """Return deterministic valid task text and preserve each requested model identity."""
        model = json.loads(request.content)['model']
        requests.append(model)
        return httpx.Response(
            200,
            json={
                'id': str(len(requests)),
                'model': model,
                'status': 'completed',
                'output': [
                    {
                        'type': 'message',
                        'content': [{'type': 'output_text', 'text': json.dumps({'task': model, 'subtasks': []})}],
                    }
                ],
            },
        )

    def run(command: str, *, overwrite: bool = False) -> int:
        """Exercise the public stage command with real LLM request parsing and offline transport."""
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            with LLMClient(config.generation, http_client=transport) as client:
                return Pipeline(
                    config,
                    producer=_producer(),
                    remote=GitHubRemoteVCS(config.source, client=github),
                    git_repository=FakeGitRepository(),
                    llm_client=client,
                ).run(command, overwrite=overwrite)

    assert run('basket') == 0
    output = Path(config.output_dir)
    original = (output / 'basket.json').read_bytes()
    previous = read_document(output / 'filtered.json', FilterDocument)
    calls = github.calls
    config.generation.llm.model = 'second-model'
    if crash:

        def interrupt(*args: object, **kwargs: object) -> None:
            """Stop after binding publication but before any new generation work is acknowledged."""
            raise OSError('simulated interruption before generation checkpoint')

        with monkeypatch.context() as patch:
            patch.setattr(Pipeline, '_prepare_generate', interrupt)
            with pytest.raises(OSError, match='simulated interruption'):
                run('generate', overwrite=True)
        assert requests == ['first-model'] and (output / 'basket.json').read_bytes() == original
        with pytest.raises(StorageError, match='OVERWRITE=1'):
            run('generate')
    assert run('generate', overwrite=True) == 0
    assert requests == ['first-model', 'second-model'] and github.calls == calls
    assert (output / 'basket.json').read_bytes() == original
    current = read_document(output / 'filtered.json', FilterDocument)
    assert current.tasks == previous.tasks and current.meta.settings == previous.meta.settings
    assert current.selection is not None and previous.selection is not None
    assert current.selection.selected_ids == previous.selection.selected_ids
    assert current.selection.fingerprint != previous.selection.fingerprint
    assert run('export') == 0
    assert read_document(output / 'basket.json', BasketDocument).tasks[0].task.task == 'second-model'


def test_historical_generation_rejection_is_not_repeated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returning a rejected task to selection restores the same outcome and its original journal."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 3]
    config.filter.selection.shuffle = False
    config.filter.selection.limit = 2

    def filter_record(
        record: CollectRecord,
        repository: GitRepository,
        remote: RemoteVCS,
        filter_config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord:
        """Keep one selected record without issue text so generation deterministically rejects it."""
        result = _filter_one(
            record, repository, remote, filter_config, stop_event, hosting_repository=hosting_repository
        )
        return result.model_copy(update={'issues': []}) if record.change_request.number == 3 else result

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_record)
    client = RejectingGitHub()
    assert pipeline(config, client).run('basket', allow_partial=True) == 0
    config.filter.selection.limit = 1
    assert pipeline(config, client).run('basket', reselect=True) == 0

    def forbidden(*args: object, **kwargs: object) -> None:
        """Any repeated operation would violate reuse of a completed deterministic rejection."""
        raise AssertionError('historical rejection must not be processed again')

    monkeypatch.setattr('basket_collector.pipeline.generate_record', forbidden)
    config.filter.selection.limit = 2
    assert pipeline(config, client).run('basket', reselect=True) == 3
    assert pipeline(config, client).run('basket', allow_partial=True) == 0
    basket = read_document(Path(config.output_dir) / 'basket.json', BasketDocument)
    assert len(basket.meta.omissions) == 1 and basket.meta.omissions[0].kind == 'rejected'
    journal = StageStore(Path(config.output_dir), 'generate').operation_state()['attempts'][basket.meta.omissions[0].id]
    assert journal['reused_from'] and len(journal['rounds']) == 1
