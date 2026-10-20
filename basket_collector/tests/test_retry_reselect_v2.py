"""Exercise retry and selection decisions together on an existing partial basket."""

import threading
from pathlib import Path

import pytest
from basket_collector.attempts import AttemptRunner
from basket_collector.config import FilterConfig, GenerationConfig, TemplateSnapshot
from basket_collector.contracts import (
    BasketDocument,
    CollectRecord,
    FilterRecord,
    RejectedRecord,
    Repository,
    TaskRecord,
    TasksDocument,
)
from basket_collector.git import GitInfrastructureError, GitRepository
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.llm import LLMCallError, LLMClient
from basket_collector.pipeline import Pipeline
from basket_collector.remote import RemoteVCS
from basket_collector.storage import StageStore, read_document
from test_pipeline import FakeGitHub, FakeGitRepository, _config, _filter_one, _producer, real_generate_record


@pytest.mark.parametrize('allow_partial', [False, True])
def test_retry_and_reselect_existing_partial_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_partial: bool
) -> None:
    """Reconcile upstream recovery and generation retry in one pass without losing prior success."""
    config = _config(tmp_path)
    config.collect.change_ids = [1, 2, 3, 4]
    config.filter.selection.shuffle = False
    config.generation.max_retries = 1
    monkeypatch.setattr(AttemptRunner, '_wait_ready', lambda self, journal: None)
    recovering = [False]
    generated: list[int] = []
    output = Path(config.output_dir)
    original: list[bytes] = []

    def filter_record(
        record: CollectRecord,
        repository: GitRepository,
        remote: RemoteVCS,
        filter_config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord:
        """Recover one failed input while keeping a second upstream omission."""
        number = record.change_request.number
        if number == 4 or (number == 3 and not recovering[0]):
            raise GitInfrastructureError('fixture unavailable source')
        return _filter_one(record, repository, remote, filter_config, stop_event, hosting_repository=hosting_repository)

    def generate(
        record: FilterRecord,
        generation_config: GenerationConfig,
        *,
        llm_client: LLMClient | None = None,
        stop_event: threading.Event | None = None,
        template_snapshot: TemplateSnapshot | None = None,
    ) -> TaskRecord | RejectedRecord:
        """Retain success and verify the old basket remains intact throughout new generation."""
        number = record.change_request.number
        generated.append(number)
        if original:
            assert (output / 'basket.json').read_bytes() == original[0]
        if number == 2 and not recovering[0]:
            raise LLMCallError('fixture_failure', 'fixture generation failure', True, 1)
        return real_generate_record(
            record, generation_config, llm_client=llm_client, stop_event=stop_event, template_snapshot=template_snapshot
        )

    def run(**flags: bool) -> int:
        """Use a fresh coordinator for each pass through the same frozen artifacts."""
        return Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
        ).run('basket', **flags)

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_record)
    monkeypatch.setattr('basket_collector.pipeline.generate_record', generate)
    assert run(allow_partial=True) == 0
    original.append((output / 'basket.json').read_bytes())
    previous = (output / '.current').resolve() / 'published/basket.json'
    assert generated == [1, 2, 2]
    old = read_document(output / 'tasks.json', TasksDocument)
    failed_id = old.failed[0].record.id
    successful_id = old.tasks[0].id
    recovering[0] = True
    assert run(retry_failed=True, reselect=True, allow_partial=allow_partial) == (0 if allow_partial else 3)
    if not allow_partial:
        assert generated == [1, 2, 2]
        assert (output / 'basket.json').read_bytes() == original[0]
        state = StageStore(output, 'generate').operation_state()
        assert len(state['attempts'][failed_id]['rounds']) == 1
        assert run(retry_failed=True, reselect=True, allow_partial=True) == 0
    assert generated == [1, 2, 2, 2, 3]
    state = StageStore(output, 'generate').operation_state()
    assert len(state['attempts'][failed_id]['rounds']) == 2
    assert [len(item['attempts']) for item in state['attempts'][failed_id]['rounds']] == [2, 1]
    assert len(state['attempts'][successful_id]['rounds']) == 1
    basket = read_document(output / 'basket.json', BasketDocument)
    assert [item.change_request.number for item in basket.tasks] == [1, 2, 3]
    assert len(basket.meta.omissions) == 1 and basket.meta.omissions[0].stage == 'filter'
    assert basket.tasks[0].provenance.generation.reused_from is not None
    assert previous.read_bytes() == original[0]
    original[0] = (output / 'basket.json').read_bytes()
    assert run() == 0 and generated == [1, 2, 2, 2, 3]
    assert (output / 'basket.json').read_bytes() == original[0]
