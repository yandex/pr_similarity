"""Neutral contracts bind source ownership and retrieval proof independently of APIs."""

import copy
import json
from pathlib import Path

import pytest
from basket_collector.contracts import BasketDocument
from basket_collector.sources import SourceDescriptor, change_id
from pydantic import ValidationError


def example(name: str = 'github') -> dict:
    """Read a synthetic shared structural fixture from the canonical kit."""
    root = Path(__file__).resolve().parent / 'fixtures/basket-contract'
    return json.loads((root / (name + '.json')).read_text())


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_both_provider_baskets_use_one_model(provider: str) -> None:
    """Round-trip both project kinds through the same strict producer model."""
    document = BasketDocument.model_validate(example(provider))
    assert document.meta.schema_version == 1
    assert BasketDocument.model_validate(document.model_dump(mode='json')) == document
    assert document.tasks[0].source.vcs == 'git'


def test_task_id_is_stable_across_path_renames_and_unique_across_servers() -> None:
    """Immutable server/project identity prevents label and cross-server collisions."""
    identity = change_id('gitlab', 'https://gitlab.com', '1', 'merge_request', 42)
    assert identity == change_id('gitlab', 'https://gitlab.com', '1', 'merge_request', 42)
    assert identity != change_id('gitlab', 'https://gitlab.example.org', '1', 'merge_request', 42)
    assert identity != change_id('github', 'https://gitlab.com', '1', 'pull_request', 42)
    document = example('gitlab')
    document['meta']['repository']['path'] = 'renamed/subgroup/project'
    document['tasks'][0]['change_request']['repository'] = 'renamed/subgroup/project'
    BasketDocument.model_validate(document)


@pytest.mark.parametrize('field', ['project_id', 'server_url'])
def test_foreign_repository_cannot_reuse_task_identity(field: str) -> None:
    """Reject a structurally valid task attached to another source owner."""
    document = example()
    document['meta']['repository'][field] = 'other' if field == 'project_id' else 'https://other.example.org'
    with pytest.raises(ValidationError, match='record identity'):
        BasketDocument.model_validate(document)


@pytest.mark.parametrize('damage', ['source_sha256', 'before_tree', 'after'])
def test_retrieval_proof_must_match_exported_inputs(damage: str) -> None:
    """Refuse a receipt copied from another descriptor or commit/tree pair."""
    document = example()
    receipt = document['tasks'][0]['provenance']['retrieval']
    receipt[damage] = 'f' * len(receipt[damage])
    with pytest.raises(ValidationError, match='retrieval'):
        BasketDocument.model_validate(document)


@pytest.mark.parametrize('reference', ['refs/heads/x.lock', 'refs/heads/a..b', 'refs/heads/a:b', 'refs/heads/a@{b'])
def test_invalid_git_ref_hints_never_become_fetch_argv(reference: str) -> None:
    """Validate full ref syntax before a subprocess or external request."""
    descriptor = copy.deepcopy(example()['tasks'][0]['source'])
    descriptor['fetch_refs'] = [reference]
    with pytest.raises(ValidationError, match='fetch ref'):
        SourceDescriptor.model_validate(descriptor)
