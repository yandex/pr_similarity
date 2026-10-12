"""Stop at the first definitive empty output and preserve recoverable task failures."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from basket_collector.config import CollectorConfig
from basket_collector.contracts import (
    BasketDocument,
    CollectRecord,
    FailedRecord,
    FilterRecord,
    RejectionReason,
    StageError,
    TaskRecord,
)
from basket_collector.filters import compact_rejection
from basket_collector.pipeline import EmptyStageError, Pipeline
from basket_collector.remote import Candidate, RemoteError
from basket_collector.sources import Repository
from basket_collector.storage import StorageError, read_json
from basket_collector.vcs import VCS
from test_neutral_models import example


def records() -> tuple[BasketDocument, CollectRecord, FilterRecord]:
    """Build explicit owning-project stage fixtures from its independent wire example."""
    basket = BasketDocument.model_validate(example())
    row = basket.tasks[0]
    collected = CollectRecord.model_validate(
        {
            'id': row.id,
            'source': row.source.model_dump(mode='json'),
            'change_request': {**row.change_request.model_dump(mode='json'), 'body': ''},
            'revisions': {'source': row.provenance.source_revisions.model_dump(mode='json')},
        }
    )
    filtered = FilterRecord.model_validate(
        {
            **collected.model_dump(mode='json'),
            'revisions': {
                **collected.revisions.model_dump(mode='json'),
                'resolved': {**row.revisions.model_dump(mode='json'), 'commits': [row.revisions.after]},
            },
            'changes': {
                **row.changes.model_dump(mode='json'),
                'diff': '',
                'commit_messages': [{'sha': row.revisions.after, 'message': 'Fixture'}],
            },
            'issues': [],
            'retrieval': row.provenance.retrieval.model_dump(mode='json'),
        }
    )
    return basket, collected, filtered


class Remote:
    """Supply a complete normalized empty or one-task discovery population."""

    def __init__(self, *, empty: bool = False, error: RemoteError | None = None) -> None:
        """Record the selected discovery case and observable source operations."""
        self.empty = empty
        self.error = error
        self.calls: list[str] = []

    def resolve_repository(self, locator: str) -> Repository:
        """Resolve fixture identity without interpreting repository visibility."""
        self.calls.append('repository')
        return records()[0].meta.repository

    def list_merged_candidates(
        self,
        repository: Repository,
        *,
        start_page: int = 1,
        merged_from: str | None = None,
        merged_before: str | None = None,
    ) -> Iterator[tuple[int, list[Candidate]]]:
        """Fail explicitly or exhaust the configured source population."""
        self.calls.append('discovery')
        if self.error is not None:
            raise self.error
        yield start_page, [] if self.empty else [Candidate(1, '2026-01-01T00:00:00Z', 'main')]

    def get_change_request(self, repository: Repository, number: int) -> CollectRecord:
        """Return the frozen row if discovery selected it."""
        self.calls.append('change')
        return records()[1]

    def get_commits(self, record: CollectRecord) -> list[str]:
        """Refuse unexpected object-validation calls in this scheduler test."""
        raise AssertionError('filter worker is supplied separately')

    def get_closing_issues(self, record: CollectRecord) -> list:
        """Refuse unexpected issue requests after an empty boundary."""
        raise AssertionError('filter worker is supplied separately')

    def close(self) -> None:
        """Leave injected fixture resources reusable across resume."""


class PreparedCache:
    """Represent a prepared local cache when object filtering is a test seam."""

    def prepare(self) -> None:
        """Perform no external operations."""


def config(tmp_path: Path) -> CollectorConfig:
    """Keep all stage identities fixed while isolating outputs for each test."""
    return CollectorConfig.model_validate(
        {
            'dataset': {'name': 'fixture', 'description': 'Fixture', 'maintainers': ['test']},
            'source': {'provider': 'github', 'project': 'owner/repo'},
            'collect': {
                'selection': 'date_range',
                'merged_from': '2026-01-01T00:00:00Z',
                'merged_before': '2026-01-02T00:00:00Z',
            },
            'generation': {'source': 'issue'},
            'output_dir': str(tmp_path / 'output'),
            'cache_dir': str(tmp_path / 'cache'),
        }
    )


@pytest.mark.parametrize('stage', ['collect', 'filter', 'generate'])
def test_empty_population_fails_at_owning_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    """Checkpoint zero discovery/rejections/failures without issuing downstream work."""
    remote = Remote(empty=stage == 'collect')
    filtered = records()[2]

    def filter_one(*args: object, **kwargs: object) -> object:
        """Return a deterministic rejection only for the all-filtered scenario."""
        if stage == 'filter':
            return compact_rejection(
                records()[1], RejectionReason(code='missing_issue', message='No issue', observed=0, expected=1)
            )
        return filtered

    def generate(*args: object, **kwargs: object) -> FailedRecord:
        """Allow only the expected generation stage; earlier empties must stop first."""
        assert stage == 'generate'
        return FailedRecord(
            record=filtered,
            attempts=1,
            error=StageError(code='fixture_failure', message='Repair fixture', retryable=True),
        )

    monkeypatch.setattr('basket_collector.pipeline.process_one', filter_one)
    monkeypatch.setattr('basket_collector.pipeline.generate_record', generate)
    settings = config(tmp_path)
    pipeline = Pipeline(
        settings, producer=records()[0].meta.producer, remote=remote, git_repository=cast(VCS, PreparedCache())
    )
    with pytest.raises(EmptyStageError) as caught:
        pipeline.run('basket')
    assert caught.value.code == 'empty_stage_output'
    saved = read_json(tmp_path / f'output/.work/{stage}.json')
    assert saved['meta']['status'] == 'failed' and saved['meta']['error']['code'] == 'empty_stage_output'
    assert not (tmp_path / 'output/basket.json').exists()
    if stage == 'collect':
        assert remote.calls == ['repository', 'discovery']
        assert not (tmp_path / 'output/.work/filter.json').exists()
    elif stage == 'filter':
        assert len(saved['rejected']) == 1
        assert not (tmp_path / 'output/.work/generate.json').exists()
    else:
        assert len(saved['failed']) == 1


def test_all_failed_generation_can_retry_without_recollecting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RETRY_FAILED explicitly repairs a terminal zero-success generation checkpoint."""
    settings = config(tmp_path)
    basket, _collected, filtered = records()
    remote = Remote()
    monkeypatch.setattr('basket_collector.pipeline.process_one', lambda *args, **kwargs: filtered)
    monkeypatch.setattr(
        'basket_collector.pipeline.generate_record',
        lambda *args, **kwargs: FailedRecord(
            record=filtered,
            attempts=1,
            error=StageError(code='fixture_failure', message='Repair fixture', retryable=True),
        ),
    )

    def pipeline() -> Pipeline:
        """Create a fresh coordinator without changing producer or meaningful inputs."""
        return Pipeline(
            settings, producer=basket.meta.producer, remote=remote, git_repository=cast(VCS, PreparedCache())
        )

    with pytest.raises(EmptyStageError):
        pipeline().run('basket')
    calls = list(remote.calls)
    with pytest.raises(StorageError, match='empty_stage_output'):
        pipeline().run('basket')
    assert remote.calls == calls
    task = TaskRecord.model_validate(
        {
            **filtered.model_dump(mode='json'),
            'task': basket.tasks[0].task.model_dump(mode='json'),
            'generation': basket.tasks[0].provenance.generation.model_dump(mode='json'),
        }
    )
    monkeypatch.setattr('basket_collector.pipeline.generate_record', lambda *args, **kwargs: task)
    assert pipeline().run('basket', retry_failed=True) == 0
    assert remote.calls == calls
    assert (tmp_path / 'output/basket.json').is_file()


def test_auth_failure_is_not_replaced_by_empty_output(tmp_path: Path) -> None:
    """Persist the original access error before discovery can claim exhaustion."""
    remote = Remote(error=RemoteError('Check source.token_env permissions.', code='source_auth_error', retryable=True))
    with pytest.raises(RemoteError):
        Pipeline(config(tmp_path), producer=records()[0].meta.producer, remote=remote).run('basket')
    saved = read_json(tmp_path / 'output/.work/collect.json')
    assert saved['meta']['error']['code'] == 'source_auth_error'
    assert not (tmp_path / 'output/.work/filter.json').exists()
