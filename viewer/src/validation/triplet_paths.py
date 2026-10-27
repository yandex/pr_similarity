"""Portable source-variant names with adjacent files and collision-free entry folders."""

import hashlib
from pathlib import PurePosixPath
from types import MappingProxyType

_ROLE_NAMES = MappingProxyType({'base': 'base', 'human': 'human', 'ai': 'agent'})


def triplet_path(number: int, source: str, role: str) -> str:
    """Keep source parents and extensions while storing a file's variants together.

    Args:
        number: Stable positive file ordinal from the triplet index.
        source: Normalized repository-relative original file path.
        role: Semantic base, human or ai key; the ai filename uses agent.

    Returns:
        A path relative to the triplet root, such as 001/src/main_base.py.

    Raises:
        ValueError: If ownership, source path or filename cannot be represented safely.

    """
    path = PurePosixPath(source)
    if (
        type(number) is not int
        or number < 1
        or role not in _ROLE_NAMES
        or path.is_absolute()
        or path.as_posix() != source
        or not path.name
        or any(part in ('.', '..', '.git') for part in path.parts)
        or '\\' in source
        or '\x00' in source
    ):
        raise ValueError('invalid triplet owner or source path')
    stem, extension = path.stem, path.suffix
    budget = 255 - len(('_human' + extension).encode('utf-8'))
    if len(stem.encode('utf-8')) > budget:
        if budget < 10:
            raise ValueError('source extension is too long for a role suffix')
        digest = hashlib.sha256(path.name.encode('utf-8')).hexdigest()[:8]
        stem = stem.encode('utf-8')[: budget - 9].decode('utf-8', errors='ignore') + '-' + digest
    return (PurePosixPath(f'{number:03d}') / path.parent / f'{stem}_{_ROLE_NAMES[role]}{extension}').as_posix()
