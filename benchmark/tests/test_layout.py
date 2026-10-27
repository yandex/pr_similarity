"""Stable human navigation keeps immutable task and attempt ownership."""

import copy
from pathlib import Path

import pytest
from test_basket import payload

from benchmark.errors import BenchmarkError
from benchmark.models.basket import BasketV3
from benchmark.persistence.layout import (
    artifact_path,
    attempt_path,
    run_name,
    task_registry,
)


def test_readable_layout_preserves_ordinal_and_native_sequence() -> None:
    """Numbered folders match console task numbers and retain source labels."""
    basket = BasketV3.model_validate(payload())
    registry = task_registry(basket.tasks, basket.meta.repository)
    entry = registry[0]
    assert entry.ordinal == 1 and entry.id == basket.tasks[0].id
    assert entry.key.startswith('001-github-owner-repo-pr-1-')
    assert entry.path == 'tasks/' + entry.key
    assert attempt_path(entry, 1) == Path(entry.path) / 'attempts/001'
    assert attempt_path(entry, 1000).name == '1000'
    assert registry == task_registry(basket.tasks, basket.meta.repository)


def test_distinct_task_ids_and_hosts_keep_distinct_keys() -> None:
    """PR numbers are labels rather than global ownership identifiers."""
    basket = BasketV3.model_validate(payload())
    rows = [basket.tasks[0], copy.deepcopy(basket.tasks[0])]
    rows[1].id = 'other-task'
    registry = task_registry(rows, basket.meta.repository)
    assert len({entry.key for entry in registry}) == 2
    repository = basket.meta.repository.model_copy(deep=True)
    repository.provider = 'gitlab'
    repository.server_url = 'https://git.example.org'
    assert 'git-example-org' in task_registry(rows, repository)[0].key
    with pytest.raises(BenchmarkError):
        task_registry([rows[0], rows[0]], repository)


def test_run_name_is_readable_and_does_not_escape_output() -> None:
    """Labels cannot create paths or leak raw terminal controls into folder names."""
    name = run_name('../../Agent / model\n', '11111111-1111-4111-8111-111111111111', '2026-10-08T14:30:00Z')
    assert name == '20261008T143000Z-agent-model-11111111'
    assert '/' not in name and '..' not in name and '\n' not in name


def test_artifact_reference_rejects_symlink_ancestors_and_traversal(tmp_path: Path) -> None:
    """Resolving an apparently safe final file never follows an intermediate symlink."""
    root = tmp_path / 'run'
    root.mkdir()
    other = tmp_path / 'outside'
    other.mkdir()
    (other / 'file.py').write_text('outside')
    (root / 'link').symlink_to(other, target_is_directory=True)
    for reference in ['link/file.py', '../outside/file.py', '/absolute', 'a/../b']:
        with pytest.raises(BenchmarkError):
            artifact_path(root, reference)
    assert artifact_path(root, 'not-created/agent.log') == root / 'not-created/agent.log'


@pytest.mark.parametrize('sequence', [0, -1, True])
def test_attempt_sequence_must_be_a_positive_integer(sequence: int) -> None:
    """Boolean and nonpositive numbers never reserve native attempt slots."""
    entry = task_registry(BasketV3.model_validate(payload()).tasks, BasketV3.model_validate(payload()).meta.repository)[
        0
    ]
    with pytest.raises(BenchmarkError):
        attempt_path(entry, sequence)
