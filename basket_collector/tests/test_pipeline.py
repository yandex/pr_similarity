"""Offline coordinator checks with frozen producer and local fake clients."""

import io
import json
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from basket_collector.config import (
    CollectorConfig,
    FilterConfig,
    GenerationConfig,
    GitHubConfig,
    TemplateSnapshot,
)
from basket_collector.contracts import (
    BasketDocument,
    ChangedFile,
    Changes,
    ChangeStats,
    CollectDocument,
    CollectRecord,
    CommitMessage,
    FailedRecord,
    FilterDocument,
    FilterRecord,
    FilterRevisions,
    InputReceipt,
    Issue,
    Producer,
    Repository,
    ResolvedRevisions,
    StageError,
    TaskRecord,
    TasksDocument,
)
from basket_collector.git import GitRepository
from basket_collector.github import GitHubClient, GitHubError
from basket_collector.github_source import GitHubRemoteVCS
from basket_collector.llm import LLMAuthenticationError, LLMClient
from basket_collector.pipeline import Pipeline, PipelineInterrupted, Shutdown, main
from basket_collector.progress import Progress
from basket_collector.remote import RemoteError
from basket_collector.stages.filter import RemoteVCS, SourceChangedError
from basket_collector.stages.generate import generate_record as real_generate_record
from basket_collector.storage import (
    StorageError,
    read_checkpoint,
    read_document,
    stage_sha256,
)
from fixture_helpers import issue_identity, retrieval

SHA = 'a' * 40
BASE = 'b' * 40
BLOB = 'c' * 40
HASH = 'd' * 64
TIME = '2026-01-02T00:00:00Z'


def _producer() -> Producer:
    """Build a stable producer identity for offline pipeline tests."""
    return Producer(
        version='test',
        implementation_sha256=HASH,
        dependencies_sha256=HASH,
        python_version='3.12.3',
        git_version='git test',
    )


def _config(tmp_path: Path) -> CollectorConfig:
    """Build the minimal issue-backed collector configuration."""
    return CollectorConfig.model_validate(
        {
            'source': {'provider': 'github', 'project': 'owner/repo'},
            'dataset': {'name': 'test basket', 'description': 'offline', 'maintainers': ['alice']},
            'output_dir': str(tmp_path / 'output'),
            'cache_dir': str(tmp_path / 'cache'),
            'collect': {'selection': 'change_ids', 'change_ids': [1]},
            'generation': {'source': 'issue'},
        }
    )


class FakeGitHub(GitHubClient):
    """Count GitHub calls while returning deterministic PR metadata."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    def repository(self, full_name: str) -> Repository:
        """Return a public repository with a non-main default branch."""
        self.calls += 1
        return Repository(
            path=full_name,
            web_url=f'https://github.com/{full_name}',
            default_branch='develop',
            provider='github',
            server_url='https://github.com',
            project_id='123',
        )

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Return a merged PR snapshot with fixed source revisions."""
        self.calls += 1
        return {
            'number': number,
            'merged_at': TIME,
            'html_url': f'https://github.com/{repository}/pull/{number}',
            'title': 'Change',
            'body': '',
            'user': None,
            'commits': 1,
            'merge_commit_sha': None,
            'base': {'sha': BASE, 'ref': 'develop', 'repo': {'full_name': repository, 'id': 123}},
            'head': {'sha': SHA, 'repo': None},
        }


class FakeGitRepository(GitRepository):
    """Stand in for Git preparation when diff processing is mocked."""

    def __init__(self) -> None:
        """Avoid creating a real Git cache for mocked filter workers."""

    def prepare(self) -> None:
        """Accept repository preparation without touching Git."""
        pass


def _filter_one(
    record: CollectRecord,
    repository: GitRepository,
    github: RemoteVCS,
    config: FilterConfig,
    stop_event: threading.Event | None,
    *,
    hosting_repository: Repository | None = None,
) -> FilterRecord:
    """Return one filtered record with a source file and linked issue."""
    changes = Changes(
        files=[
            ChangedFile(
                path='file.py',
                status='added',
                additions=1,
                deletions=0,
                before_blob=None,
                after_blob=BLOB,
                before_mode=None,
                after_mode='100644',
            )
        ],
        stats=ChangeStats(files_count=1, additions=1, deletions=0, changed_lines=1, patch_chars=1),
        diff='x',
        commit_messages=[CommitMessage(sha=SHA, message='Change')],
    )
    return FilterRecord(
        id=record.id,
        change_request=record.change_request,
        revisions=FilterRevisions(
            source=record.revisions.source,
            resolved=ResolvedRevisions(
                policy='change_merge_base_v1',
                before=BASE,
                after=SHA,
                commits=[SHA],
                before_tree='dddddddddddddddddddddddddddddddddddddddd',
                after_tree='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
            ),
        ),
        changes=changes,
        issues=[
            Issue(
                id=issue_identity(2),
                repository='owner/repo',
                number=2,
                url='https://github.com/owner/repo/issues/2',
                title='Implement change',
                body='Details',
            )
        ],
        source=record.source,
        retrieval=retrieval(record.source, BASE, SHA),
    )


def test_basket_runs_in_order_and_reuses_without_external_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify stage order, lineage hashes, and reuse without new API calls."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    github = FakeGitHub()
    pipeline = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=FakeGitRepository(),
    )
    assert pipeline.run('basket') == 0
    assert github.calls == 2

    output = Path(config.output_dir)
    collected = read_document(output / 'collected.json', CollectDocument)
    assert collected.meta.repository.default_branch == 'develop'
    filtered = read_document(output / 'filtered.json', FilterDocument)
    tasks = read_document(output / 'tasks.json', TasksDocument)
    basket = read_document(output / 'basket.json', BasketDocument)
    assert filtered.meta.input is not None
    assert tasks.meta.input is not None
    assert basket.meta.input is not None
    assert filtered.meta.input.sha256 == stage_sha256(collected)
    assert tasks.meta.input.sha256 == stage_sha256(filtered)
    assert basket.meta.input.sha256 == stage_sha256(tasks)
    assert basket.tasks[0].agent_task == 'Task: Implement change\n\nSubtasks:\n- Details'
    assert 'golden_diff' not in basket.model_dump(mode='json')['tasks'][0]
    again = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=FakeGitRepository(),
    )
    assert again.run('basket') == 0
    assert github.calls == 2


def test_progress_modes_preserve_semantic_hashes_and_short_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the display cannot change artifacts or fill an undersized result bar."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    hashes = []
    for mode in ('off', 'plain', 'auto'):
        config = _config(tmp_path / mode)
        config = config.model_copy(
            update={
                'filter': config.filter.model_copy(
                    update={'selection': config.filter.selection.model_copy(update={'limit': 10})}
                )
            }
        )
        stream = io.StringIO()
        progress = Progress(mode, stream=stream)
        pipeline = Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
            progress=progress,
        )
        assert pipeline.run('basket') == 0
        output = Path(config.output_dir)
        hashes.append(
            [
                stage_sha256(read_document(output / name, model))
                for name, model in [
                    ('collected.json', CollectDocument),
                    ('filtered.json', FilterDocument),
                    ('tasks.json', TasksDocument),
                    ('basket.json', BasketDocument),
                ]
            ]
        )
        basket = read_document(output / 'basket.json', BasketDocument)
        assert len(basket.tasks) == 1 and not basket.meta.partial
        if mode != 'off':
            assert 'tasks=1/10' in stream.getvalue() and 'candidate_shortfall=9' in stream.getvalue()
        else:
            assert stream.getvalue() == ''
    assert hashes[0] == hashes[1] == hashes[2]


@pytest.mark.parametrize('transition', ['resume', 'retry_failed'])
def test_progress_modes_preserve_recovery_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    """Display modes preserve interrupted checkpoints, retries and final lineage."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    observations = []
    for mode in ('off', 'plain', 'auto'):
        config = _config(tmp_path / mode)
        config = config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [1, 2]})})
        github = FakeGitHub()
        calls: list[int] = []

        def generate(record: FilterRecord, generation_config: GenerationConfig, **kwargs: Any):
            """Make one deterministic retryable failure before successful recovery."""
            number = record.change_request.number
            calls.append(number)
            if transition == 'retry_failed' and number == 2 and calls.count(2) == 1:
                return FailedRecord(
                    record=record,
                    error=StageError(code='request_failed', message='fixture', retryable=True),
                    attempts=2,
                )
            return real_generate_record(record, generation_config, **kwargs)

        monkeypatch.setattr('basket_collector.pipeline.generate_record', generate)

        def pipeline(*, shutdown: Shutdown | None = None) -> Pipeline:
            """Use fresh coordinators while retaining the same source and output."""
            return Pipeline(
                config,
                producer=_producer(),
                remote=GitHubRemoteVCS(config.source, client=github),
                git_repository=FakeGitRepository(),
                shutdown=shutdown,
                progress=Progress(mode, stream=io.StringIO()),
            )

        assert pipeline().run('collect') == 0
        output = Path(config.output_dir)
        if transition == 'resume':
            shutdown = Shutdown()
            interrupted = pipeline(shutdown=shutdown)
            current_input = interrupted._current_input

            def request_stop(stage: str) -> InputReceipt:
                """Interrupt after workers finish but before filter publication."""
                result = current_input(stage)
                shutdown.request(2, None)
                return result

            monkeypatch.setattr(interrupted, '_current_input', request_stop)
            with pytest.raises(PipelineInterrupted):
                interrupted.run('filter')
            checkpoint, state = read_checkpoint(output / '.work' / 'filter.json', FilterDocument)
            assert checkpoint.meta.status == 'interrupted' and state['phase'] == 'ready'
            recovery_hash = stage_sha256(checkpoint)
            assert pipeline().run('filter') == 0
            assert pipeline().run('generate') == 0
        else:
            assert pipeline().run('filter') == 0
            assert pipeline().run('generate') == 3
            failed = read_document(output / 'tasks.json', TasksDocument)
            assert len(failed.tasks) == len(failed.failed) == 1
            recovery_hash = stage_sha256(failed)
            assert pipeline().run('generate', retry_failed=True) == 0
        assert calls.count(1) == 1 and calls.count(2) == (2 if transition == 'retry_failed' else 1)
        assert pipeline().run('basket') == 0
        documents = [
            read_document(output / name, model)
            for name, model in [
                ('collected.json', CollectDocument),
                ('filtered.json', FilterDocument),
                ('tasks.json', TasksDocument),
                ('basket.json', BasketDocument),
            ]
        ]
        observations.append(
            (
                recovery_hash,
                [stage_sha256(document) for document in documents],
                [document.meta.counts.model_dump() for document in documents],
                [[record.id for record in document.tasks] for document in documents],
            )
        )
    assert observations[0] == observations[1] == observations[2]


def test_changed_predecessor_blocks_downstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject downstream reuse when the saved predecessor content changes."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    pipeline = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
        git_repository=FakeGitRepository(),
    )
    assert pipeline.run('basket') == 0
    collected_path = Path(config.output_dir) / 'collected.json'
    payload = json.loads(collected_path.read_text(encoding='utf-8'))
    payload['tasks'][0]['change_request']['body'] = 'edited'
    collected_path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(StorageError, match='input|lineage'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'generate'
        )


def test_explicit_other_branch_checkpoints_and_override_rebuilds(tmp_path: Path) -> None:
    """Checkpoint an explicit PR on another branch, then rebuild with an override."""

    class BackportGitHub(FakeGitHub):
        """Return PRs targeting a branch other than the repository default."""

        def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
            """Change the fake PR base branch to stable."""
            row = super().get_pull_request(repository, number)
            row['base']['ref'] = 'stable'
            return row

    config = _config(tmp_path)
    with pytest.raises(RemoteError, match='another branch'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=BackportGitHub())).run(
            'collect'
        )
    checkpoint, state = read_checkpoint(
        Path(config.output_dir) / '.work' / 'collect.json',
        CollectDocument,
    )
    assert checkpoint.meta.repository.default_branch == 'develop'
    assert checkpoint.meta.error is not None and checkpoint.meta.error.code == 'non_default_branch'
    assert state['numbers'] == [1]
    override = config.model_copy(update={'collect': config.collect.model_copy(update={'default_branch_only': False})})
    with pytest.raises(StorageError, match='OVERWRITE=1'):
        Pipeline(override, producer=_producer(), remote=GitHubRemoteVCS(override.source, client=BackportGitHub())).run(
            'collect'
        )
    assert (
        Pipeline(override, producer=_producer(), remote=GitHubRemoteVCS(override.source, client=BackportGitHub())).run(
            'collect', overwrite=True
        )
        == 0
    )
    collected = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert collected.meta.repository.default_branch == 'develop'
    assert collected.tasks[0].change_request.number == 1


def test_retry_failed_and_overwrite_are_mutually_exclusive(tmp_path: Path) -> None:
    """Reject conflicting retry and overwrite options before work begins."""
    with pytest.raises(ValueError, match='cannot be combined'):
        Pipeline(_config(tmp_path), producer=_producer()).run('basket', overwrite=True, retry_failed=True)


def test_generate_infrastructure_failure_stays_failed_not_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep a failed generation worker as a failed checkpoint."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    pipeline = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
        git_repository=FakeGitRepository(),
    )
    assert pipeline.run('collect') == 0
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
        ).run('filter')
        == 0
    )

    def fail_generation(*args: object, **kwargs: object) -> None:
        """Simulate an unexpected generation worker failure."""
        raise RuntimeError('worker failed')

    monkeypatch.setattr('basket_collector.pipeline.generate_record', fail_generation)
    with pytest.raises(RuntimeError, match='worker failed'):
        Pipeline(config, producer=_producer()).run('generate')
    checkpoint, _ = read_checkpoint(
        Path(config.output_dir) / '.work' / 'generate.json',
        TasksDocument,
    )
    assert checkpoint.meta.status == 'failed'
    assert checkpoint.meta.error is not None and checkpoint.meta.error.code == 'generate_failed'


def test_generate_persists_same_templates_used_for_two_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the same captured templates for every request in a generation stage."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    monkeypatch.setenv('BASKET_TEST_LLM_KEY', 'test-secret')
    system_path = tmp_path / 'system.jinja2'
    user_path = tmp_path / 'diff.jinja2'
    system_path.write_text('Original system {{ result_schema }}', encoding='utf-8')
    user_path.write_text('Original user {{ diff }}', encoding='utf-8')
    generation = GenerationConfig.model_validate(
        {
            'source': 'llm',
            'context': 'diff',
            'workers': 1,
            'templates': {'system': str(system_path), 'user': str(user_path)},
            'llm': {
                'api': 'openai_responses',
                'base_url': 'https://example.test/v1',
                'api_key_env': 'BASKET_TEST_LLM_KEY',
                'model': 'fixture',
            },
        }
    )
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            'collect': config.collect.model_copy(update={'change_ids': [1, 2]}),
            'generation': generation,
        }
    )
    github = FakeGitHub()
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect') == 0
    )
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run('filter')
        == 0
    )
    prompts: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Mutate template files after the first LLM request and return a task."""
        payload = json.loads(request.content)
        prompts.append([item['content'] for item in payload['input']])
        if len(prompts) == 1:
            system_path.write_text('Mutated system', encoding='utf-8')
            user_path.write_text('Mutated user {{ diff }}', encoding='utf-8')
        return httpx.Response(
            200,
            json={
                'id': 'response',
                'model': 'fixture',
                'status': 'completed',
                'output': [
                    {
                        'type': 'message',
                        'content': [{'type': 'output_text', 'text': '{"task":"Do work","subtasks":[]}'}],
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(generation, http_client=transport) as client:
            assert Pipeline(config, producer=_producer(), llm_client=client).run('basket') == 0
    tasks = read_document(Path(config.output_dir) / 'tasks.json', TasksDocument)
    assert len(tasks.tasks) == 2
    assert len(prompts) == 2 and prompts[0] == prompts[1]
    assert all('Mutated' not in content for pair in prompts for content in pair)
    templates = tasks.meta.settings['templates']
    assert isinstance(templates, dict)
    system = templates['system']
    user = templates['user']
    assert isinstance(system, dict) and isinstance(user, dict)
    assert system['content'] == 'Original system {{ result_schema }}'
    assert user['content'] == 'Original user {{ diff }}'
    basket = read_document(Path(config.output_dir) / 'basket.json', BasketDocument)
    assert basket.meta.input is not None
    assert basket.meta.input.sha256 == stage_sha256(tasks)


def test_filter_initializes_shared_github_client_on_coordinator_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create the shared GitHub client on the coordinator thread."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )
        == 0
    )
    main_thread = threading.current_thread()
    creations: list[int] = []

    class TrackingGitHub(FakeGitHub):
        """Track closure of the coordinator-owned GitHub client."""

        def close(self) -> None:
            """Accept client shutdown after filtering."""
            pass

    def make_github(*args: object, **kwargs: object) -> TrackingGitHub:
        """Record the thread that constructs the shared client."""
        assert threading.current_thread() is main_thread
        creations.append(1)
        return TrackingGitHub()

    monkeypatch.setattr('basket_collector.github_source.GitHubClient', make_github)
    assert Pipeline(config, producer=_producer(), git_repository=FakeGitRepository()).run('filter') == 0
    assert len(creations) == 1


def test_retry_failed_reprocesses_only_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry failed tasks without regenerating successful records."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    config = config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [1, 2]})})
    github = FakeGitHub()
    pipeline = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=FakeGitRepository(),
    )
    assert pipeline.run('collect') == 0
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run('filter')
        == 0
    )
    calls: list[int] = []

    def generate(
        record: FilterRecord,
        generation_config: GenerationConfig,
        *,
        llm_client: LLMClient | None = None,
        stop_event: threading.Event | None = None,
        previous_attempts: int = 0,
        template_snapshot: TemplateSnapshot | None = None,
    ) -> TaskRecord | FailedRecord:
        """Fail one task on its first attempt and generate the others."""
        calls.append(record.change_request.number)
        if record.change_request.number == 2 and calls.count(2) == 1:
            return FailedRecord(
                record=record, error=StageError(code='request_failed', message='fixture', retryable=True), attempts=2
            )
        return real_generate_record(
            record,
            generation_config,
            llm_client=llm_client,
            stop_event=stop_event,
            previous_attempts=previous_attempts,
            template_snapshot=template_snapshot,
        )

    monkeypatch.setattr('basket_collector.pipeline.generate_record', generate)
    assert Pipeline(config, producer=_producer()).run('generate') == 3
    first = read_document(Path(config.output_dir) / 'tasks.json', TasksDocument)
    success = next(item for item in first.tasks if item.change_request.number == 1)
    assert Pipeline(config, producer=_producer()).run('generate') == 3
    assert len(calls) == 2
    assert Pipeline(config, producer=_producer()).run('generate', retry_failed=True) == 0
    assert calls.count(1) == 1 and calls.count(2) == 2
    second = read_document(Path(config.output_dir) / 'tasks.json', TasksDocument)
    assert second.failed == []
    assert next(item for item in second.tasks if item.change_request.number == 1) == success


def test_cli_reports_infrastructure_failure_as_exit_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return exit code four for unexpected pipeline failures."""
    monkeypatch.setattr('basket_collector.pipeline.load_config', lambda path: _config(tmp_path))

    def fail_run(*args: object, **kwargs: object) -> None:
        """Simulate an infrastructure failure after configuration loads."""
        raise RuntimeError('fixture infrastructure failure')

    monkeypatch.setattr('basket_collector.pipeline.Pipeline.run', fail_run)
    assert main(['collect', '--config', str(tmp_path / 'config.yaml')]) == 4


def test_cli_loads_env_file_before_creating_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load the local environment file before constructing stage clients."""
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        'dataset: {name: sample, description: fixture, maintainers: [alice]}\n'
        'source: {provider: github, project: owner/repo}\ncollect: {selection: change_ids, change_ids: [1]}\n'
        'generation: {source: issue}\n',
        encoding='utf-8',
    )
    (tmp_path / '.env').write_text('BASKET_TEST_CLI_SECRET=from-file\n', encoding='utf-8')
    monkeypatch.delenv('BASKET_TEST_CLI_SECRET', raising=False)

    def check_run(*args: object, **kwargs: object) -> int:
        """Assert that the environment fallback is available to the pipeline."""
        assert os.environ['BASKET_TEST_CLI_SECRET'] == 'from-file'
        return 0

    monkeypatch.setattr('basket_collector.pipeline.Pipeline.run', check_run)
    assert main(['collect', '--config', str(config_path)]) == 0
    monkeypatch.delenv('BASKET_TEST_CLI_SECRET', raising=False)


def test_source_drift_has_nonretryable_checkpoint_code(tmp_path: Path) -> None:
    """Write a nonretryable checkpoint when GitHub source revisions drift."""
    config = _config(tmp_path)
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )
        == 0
    )

    class DriftedGitHub(FakeGitHub):
        """Return a PR whose head changed after collection."""

        def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
            """Change the fake PR head SHA to simulate source drift."""
            value = super().get_pull_request(repository, number)
            value['head']['sha'] = 'e' * 40
            return value

    with pytest.raises(SourceChangedError, match='source_changed'):
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=DriftedGitHub()),
            git_repository=FakeGitRepository(),
        ).run('filter')
    checkpoint, _ = read_checkpoint(
        Path(config.output_dir) / '.work' / 'filter.json',
        FilterDocument,
    )
    assert checkpoint.meta.error is not None
    assert checkpoint.meta.error.code == 'source_changed'
    assert checkpoint.meta.error.retryable is False
    with pytest.raises(StorageError, match='source_changed.*OVERWRITE=1'):
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=DriftedGitHub()),
            git_repository=FakeGitRepository(),
        ).run('filter')


def test_rate_limit_403_is_retryable_after_budget_exhaustion(tmp_path: Path) -> None:
    """Resume collection after a retryable GitHub rate limit response."""
    config = _config(tmp_path)
    config = config.model_copy(update={'source': config.source.model_copy(update={'max_retries': 0})})

    def rate_limited(request: httpx.Request) -> httpx.Response:
        """Return a rate-limited HTTP response with no remaining quota."""
        return httpx.Response(
            403,
            json={'message': 'rate limit'},
            headers={'x-ratelimit-remaining': '0', 'retry-after': '0'},
        )

    with GitHubClient(
        GitHubConfig.model_validate(config.source.model_dump(exclude={'provider', 'project', 'server_url'})),
        transport=httpx.MockTransport(rate_limited),
    ) as github:
        with pytest.raises(GitHubError):
            Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect')
    checkpoint, _ = read_checkpoint(
        Path(config.output_dir) / '.work' / 'collect.json',
        CollectDocument,
    )
    assert checkpoint.meta.error is not None
    assert checkpoint.meta.error.retryable is True
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )
        == 0
    )


def test_auth_failure_resumes_collected_records_after_token_repair(tmp_path: Path) -> None:
    """Resume saved PRs after repairing an expired GitHub token."""
    config = _config(tmp_path)
    config = config.model_copy(update={'collect': config.collect.model_copy(update={'change_ids': [1, 2]})})

    class ExpiredTokenGitHub(FakeGitHub):
        """Fail only the second PR with an authentication error."""

        def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
            """Simulate an expired token while fetching the second PR."""
            if number == 2:
                raise GitHubError('GitHub HTTP 401', code='github_auth_error', retryable=False)
            return super().get_pull_request(repository, number)

    with pytest.raises(GitHubError, match='401'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=ExpiredTokenGitHub())).run(
            'collect'
        )
    checkpoint, state = read_checkpoint(Path(config.output_dir) / '.work' / 'collect.json', CollectDocument)
    assert checkpoint.meta.status == 'failed'
    assert checkpoint.meta.counts.succeeded == 1
    assert checkpoint.meta.counts.pending == 1
    assert checkpoint.meta.error is not None and checkpoint.meta.error.retryable is True
    assert state['numbers'] == [1, 2]

    class RepairedTokenGitHub(FakeGitHub):
        """Record which PRs are requested after token repair."""

        def __init__(self) -> None:
            """Initialize the repaired client request log."""
            super().__init__()
            self.requested: list[int] = []

        def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
            """Record resumed requests before returning their PR metadata."""
            self.requested.append(number)
            return super().get_pull_request(repository, number)

    repaired = RepairedTokenGitHub()
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=repaired)).run('collect')
        == 0
    )
    assert repaired.requested == [2]
    collected = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert [item.change_request.number for item in collected.tasks] == [1, 2]


def test_stop_before_discovery_worker_starts_keeps_resumable_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip discovery HTTP calls when shutdown wins the submission race."""
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            'collect': config.collect.model_copy(
                update={
                    'selection': 'date_range',
                    'change_ids': None,
                    'merged_from': '2026-01-01T00:00:00Z',
                    'merged_before': '2026-01-04T00:00:00Z',
                }
            )
        }
    )

    class PagingGitHub(FakeGitHub):
        """Record discovery page calls independently of the stop event."""

        def __init__(self) -> None:
            """Initialize the page request log."""
            super().__init__()
            self.pages: list[int] = []

        def list_closed_prs(
            self, repository: str, *, start_page: int = 1
        ) -> Iterator[tuple[int, list[dict[str, Any]]]]:
            """Record the requested page and return one merged PR."""
            self.pages.append(start_page)
            yield 1, [{'number': 1, 'merged_at': TIME, 'base': {'ref': 'develop'}}]

    shutdown = Shutdown()
    github = PagingGitHub()
    original_submit = ThreadPoolExecutor.submit
    submissions = 0

    def submit_with_stop[**P, R](
        executor: ThreadPoolExecutor, operation: Callable[P, R], *args: P.args, **kwargs: P.kwargs
    ) -> Future[R]:
        """Request shutdown after repository lookup, before discovery starts."""
        nonlocal submissions
        submissions += 1
        if submissions == 2:
            shutdown.request(2, None)
        return original_submit(executor, operation, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, 'submit', submit_with_stop)
    with pytest.raises(PipelineInterrupted):
        Pipeline(
            config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github), shutdown=shutdown
        ).run('collect')
    assert github.pages == []
    checkpoint, state = read_checkpoint(Path(config.output_dir) / '.work' / 'collect.json', CollectDocument)
    assert checkpoint.meta.status == 'interrupted'
    assert checkpoint.meta.error is None
    assert state['phase'] == 'discovery'
    assert state['next_page'] == 1
    assert state['matches'] == []
    assert checkpoint.tasks == []
    assert not (Path(config.output_dir) / 'collected.json').exists()

    monkeypatch.setattr(ThreadPoolExecutor, 'submit', original_submit)
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect') == 0
    )
    assert github.pages == [1]
    collected = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert [record.change_request.number for record in collected.tasks] == [1]


def test_discovery_page_cursor_survives_a_later_page_failure(tmp_path: Path) -> None:
    """Resume date-range discovery after a saved page without replaying it."""
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            'collect': config.collect.model_copy(
                update={
                    'selection': 'date_range',
                    'change_ids': None,
                    'merged_from': '2026-01-01T00:00:00Z',
                    'merged_before': '2026-01-04T00:00:00Z',
                }
            )
        }
    )

    class PagingGitHub(FakeGitHub):
        """Fail once after the first completed discovery page."""

        def __init__(self, fail: bool) -> None:
            """Record the first page requested in each run."""
            super().__init__()
            self.fail = fail
            self.starts: list[int] = []

        def list_closed_prs(
            self,
            repository: str,
            *,
            start_page: int = 1,
        ) -> Iterator[tuple[int, list[dict[str, Any]]]]:
            """Yield the saved page, then fail or yield the next page."""
            self.starts.append(start_page)
            if start_page <= 1:
                yield 1, [{'number': 1, 'merged_at': TIME, 'base': {'ref': 'develop'}}]
            if self.fail:
                raise GitHubError('temporary discovery failure', code='github_timeout', retryable=True)
            if start_page <= 2:
                yield 2, [{'number': 2, 'merged_at': TIME, 'base': {'ref': 'develop'}}]

    first = PagingGitHub(fail=True)
    with pytest.raises(GitHubError, match='temporary discovery failure'):
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=first)).run('collect')
    checkpoint, state = read_checkpoint(Path(config.output_dir) / '.work' / 'collect.json', CollectDocument)
    assert checkpoint.meta.status == 'failed'
    assert state['next_page'] == 2
    assert state['matches'] == [['2026-01-02T00:00:00.000000Z', 1]]

    resumed = PagingGitHub(fail=False)
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=resumed)).run('collect')
        == 0
    )
    assert first.starts == [1] and resumed.starts == [2]
    collected = read_document(Path(config.output_dir) / 'collected.json', CollectDocument)
    assert [item.change_request.number for item in collected.tasks] == [1, 2]


def auth_generation(api: str) -> GenerationConfig:
    """Configure bounded offline generation for credential-rotation recovery."""
    prompts = Path(__file__).resolve().parents[1] / 'prompts'
    generation = GenerationConfig.model_validate(
        {
            'source': 'llm',
            'context': 'diff',
            'workers': 1,
            'max_retries': 3,
            'requests_per_second': 100000,
            'templates': {'system': str(prompts / 'system.jinja2'), 'user': str(prompts / 'diff.jinja2')},
            'llm': {
                'api': api,
                'model': 'fixture',
                'base_url': 'https://example.test/v1',
                'api_key_env': 'BASKET_TEST_LLM_KEY',
            },
        }
    )
    return generation


@pytest.mark.parametrize('api', ['openai_completions', 'openai_responses'])
@pytest.mark.parametrize('status', [401, 403])
def test_llm_auth_failure_resumes_successful_task_after_key_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: str,
    status: int,
) -> None:
    """Resume only the pending LLM task after rotating an expired key."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    monkeypatch.setenv('BASKET_TEST_LLM_KEY', 'expired-key')
    generation = auth_generation(api)
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            'collect': config.collect.model_copy(update={'change_ids': [1, 2]}),
            'generation': generation,
        }
    )
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )
        == 0
    )
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
        ).run('filter')
        == 0
    )

    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail the second request, then return valid tasks for the selected API."""
        keys.append(request.headers['Authorization'])
        if len(keys) == 2:
            return httpx.Response(status, text='expired-key is invalid')
        content = json.dumps({'task': f'Task {len(keys)}', 'subtasks': []})
        if api == 'openai_completions':
            response = {
                'id': f'chat-{len(keys)}',
                'model': 'fixture',
                'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
            }
        else:
            response = {
                'id': f'resp-{len(keys)}',
                'model': 'fixture',
                'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': content}]}],
            }
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(generation, http_client=transport) as client:
            with pytest.raises(LLMAuthenticationError):
                Pipeline(config, producer=_producer(), llm_client=client).run('generate')
        work_path = Path(config.output_dir) / '.work' / 'generate.json'
        checkpoint, _ = read_checkpoint(work_path, TasksDocument)
        assert 'expired-key' not in work_path.read_text(encoding='utf-8')
        assert checkpoint.meta.status == 'failed'
        assert checkpoint.meta.counts.succeeded == 1
        assert checkpoint.meta.counts.pending == 1
        assert checkpoint.meta.error is not None
        assert checkpoint.meta.error.code == 'llm_auth_error'
        assert checkpoint.meta.error.retryable is True
        assert len(checkpoint.tasks) == 1
        first_task = checkpoint.tasks[0]
        assert first_task.task.task == 'Task 1'
        assert len(keys) == 2

        monkeypatch.setenv('BASKET_TEST_LLM_KEY', 'rotated-key')
        with LLMClient(generation, http_client=transport) as client:
            resumed = Pipeline(config, producer=_producer(), llm_client=client)
            assert resumed.run('generate') == 0
            assert resumed.stage_modes['generate'] == 'resumed'

    assert keys == ['Bearer expired-key', 'Bearer expired-key', 'Bearer rotated-key']
    tasks = read_document(Path(config.output_dir) / 'tasks.json', TasksDocument)
    assert {task.change_request.number for task in tasks.tasks} == {1, 2}
    assert next(task for task in tasks.tasks if task.id == first_task.id) == first_task
    assert next(task for task in tasks.tasks if task.id != first_task.id).task.task == 'Task 3'
    assert tasks.failed == []


def test_missing_llm_key_has_retryable_auth_checkpoint_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record missing LLM credentials as a resumable authentication error."""
    monkeypatch.setattr('basket_collector.pipeline.process_one', _filter_one)
    config = _config(tmp_path)
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=FakeGitHub())).run(
            'collect'
        )
        == 0
    )
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=FakeGitHub()),
            git_repository=FakeGitRepository(),
        ).run('filter')
        == 0
    )
    prompts = Path(__file__).resolve().parents[1] / 'prompts'
    generation = GenerationConfig.model_validate(
        {
            'source': 'llm',
            'context': 'diff',
            'templates': {'system': str(prompts / 'system.jinja2'), 'user': str(prompts / 'diff.jinja2')},
            'llm': {'api': 'openai_responses', 'model': 'fixture', 'api_key_env': 'BASKET_TEST_UNSET_KEY'},
        }
    )
    config = config.model_copy(update={'generation': generation})
    monkeypatch.delenv('BASKET_TEST_UNSET_KEY', raising=False)
    with pytest.raises(LLMAuthenticationError, match='missing API key'):
        Pipeline(config, producer=_producer()).run('generate')
    checkpoint, _ = read_checkpoint(
        Path(config.output_dir) / '.work' / 'generate.json',
        TasksDocument,
    )
    assert checkpoint.meta.error is not None
    assert checkpoint.meta.error.code == 'llm_auth_error'
    assert checkpoint.meta.error.retryable is True


def test_stop_before_filter_publish_resumes_without_refiltering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume a ready filter checkpoint without filtering the PR again."""
    calls: list[int] = []

    def filter_one(
        record: CollectRecord,
        repository: GitRepository,
        github: RemoteVCS,
        config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord:
        """Record each PR passed through the filter worker."""
        calls.append(record.change_request.number)
        return _filter_one(record, repository, github, config, stop_event)

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_one)
    config = _config(tmp_path)
    github = FakeGitHub()
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect') == 0
    )
    shutdown = Shutdown()
    pipeline = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=FakeGitRepository(),
        shutdown=shutdown,
    )
    original = pipeline._current_input

    def request_stop(stage: str) -> InputReceipt:
        """Request shutdown just before final input verification."""
        result = original(stage)
        shutdown.request(2, None)
        return result

    monkeypatch.setattr(pipeline, '_current_input', request_stop)
    with pytest.raises(PipelineInterrupted):
        pipeline.run('filter')
    checkpoint, state = read_checkpoint(Path(config.output_dir) / '.work' / 'filter.json', FilterDocument)
    assert checkpoint.meta.status == 'interrupted' and state['phase'] == 'ready'
    resumed = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=FakeGitRepository(),
    )
    assert resumed.run('filter') == 0
    assert calls == [1]


def test_filter_selection_is_not_reapplied_after_predecessor_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve selected PRs across a failed predecessor verification."""
    calls: list[int] = []

    def filter_one(
        record: CollectRecord,
        repository: GitRepository,
        github: RemoteVCS,
        config: FilterConfig,
        stop_event: threading.Event | None,
        *,
        hosting_repository: Repository | None = None,
    ) -> FilterRecord:
        """Record filtered PRs while preserving the normal filter result."""
        calls.append(record.change_request.number)
        return _filter_one(record, repository, github, config, stop_event)

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_one)
    config = _config(tmp_path)
    config = config.model_copy(
        update={
            'collect': config.collect.model_copy(update={'change_ids': list(range(1, 11))}),
            'filter': config.filter.model_copy(
                update={
                    'percentile': config.filter.percentile.model_copy(
                        update={
                            'enabled': True,
                            'lower_fraction': 0.2,
                            'upper_fraction': 0.2,
                        }
                    ),
                }
            ),
        }
    )
    github = FakeGitHub()
    assert (
        Pipeline(config, producer=_producer(), remote=GitHubRemoteVCS(config.source, client=github)).run('collect') == 0
    )
    collected_path = Path(config.output_dir) / 'collected.json'
    original = collected_path.read_bytes()
    pipeline = Pipeline(
        config,
        producer=_producer(),
        remote=GitHubRemoteVCS(config.source, client=github),
        git_repository=FakeGitRepository(),
    )
    verify = pipeline._verified_predecessors
    filter_checks = 0

    def corrupt_before_final_verify(stage: str) -> list[CollectDocument | FilterDocument | TasksDocument]:
        """Corrupt the predecessor on its second final verification."""
        nonlocal filter_checks
        if stage == 'filter':
            filter_checks += 1
            if filter_checks == 2:
                collected_path.write_text('{invalid', encoding='utf-8')
        return verify(stage)

    monkeypatch.setattr(pipeline, '_verified_predecessors', corrupt_before_final_verify)
    with pytest.raises(StorageError):
        pipeline.run('filter')
    collected_path.write_bytes(original)
    checkpoint, state = read_checkpoint(Path(config.output_dir) / '.work' / 'filter.json', FilterDocument)
    assert state['phase'] == 'ready'
    assert checkpoint.meta.counts.succeeded == 6
    assert checkpoint.meta.counts.rejected == 4
    assert (
        Pipeline(
            config,
            producer=_producer(),
            remote=GitHubRemoteVCS(config.source, client=github),
            git_repository=FakeGitRepository(),
        ).run('filter')
        == 0
    )
    filtered = read_document(Path(config.output_dir) / 'filtered.json', FilterDocument)
    assert filtered.meta.counts.succeeded == 6
    assert filtered.meta.counts.rejected == 4
    assert len(calls) == 10
