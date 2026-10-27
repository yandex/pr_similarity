"""Side-by-side names retain source paths while separating each triplet namespace."""

from pathlib import PurePosixPath

import pytest

from benchmark.validation.triplet_paths import triplet_path


@pytest.mark.parametrize('source', ['src/main.py', 'src/file.test.ts', 'naïve file.py', '.env', 'LICENSE'])
def test_variants_are_adjacent_with_role_suffixes_and_original_extension(source: str) -> None:
    """All existing variants of one source file share a parent and keep their extension."""
    variants = [PurePosixPath(triplet_path(1, source, role)) for role in ('base', 'human', 'ai')]
    assert len({path.parent for path in variants}) == 1
    assert variants[0].parent == PurePosixPath('001') / PurePosixPath(source).parent
    assert all(path.suffix == PurePosixPath(source).suffix for path in variants)
    assert variants[2].stem.endswith('_agent')


def test_entry_namespace_prevents_variant_file_and_source_directory_collisions() -> None:
    """A source directory named like another file's variant cannot shadow its payload."""
    file = PurePosixPath(triplet_path(1, 'src/main.py', 'base'))
    child = PurePosixPath(triplet_path(2, 'src/main_base.py/leaf.py', 'human'))
    assert file not in child.parents
    assert triplet_path(1000, 'src/main.py', 'ai') == '1000/src/main_agent.py'


def test_long_unicode_stem_is_bounded_without_losing_extension_or_role() -> None:
    """Adding a role suffix must not exceed a valid source filename's filesystem limit."""
    source = 'é' * 124 + '.py'
    variants = [PurePosixPath(triplet_path(1, source, role)) for role in ('base', 'human', 'ai')]
    assert all(len(path.name.encode('utf-8')) <= 255 and path.suffix == '.py' for path in variants)
    assert variants[0].stem.removesuffix('_base') == variants[1].stem.removesuffix('_human')


@pytest.mark.parametrize(
    'number,path,role',
    [
        (True, 'x.py', 'base'),
        (0, 'x.py', 'base'),
        (1, '../x.py', 'base'),
        (1, '/x.py', 'base'),
        (1, 'src//x.py', 'base'),
        (1, 'x.py', 'agent'),
    ],
)
def test_invalid_owner_paths_and_role_keys_are_rejected(number: int, path: str, role: str) -> None:
    """Do not normalize traversal, invent an entry owner or silently rename semantic roles."""
    with pytest.raises(ValueError):
        triplet_path(number, path, role)
