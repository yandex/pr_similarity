"""Bounded sandbox mounts with independent copies of host executables."""

import errno
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import benchmark.execution.bin_projection as module
from benchmark.errors import BenchmarkError
from benchmark.execution.bin_projection import BinProjection


def test_large_directory_keeps_mount_arguments_bounded(tmp_path: Path) -> None:
    """Thousands of host tools require one directory bind and independent files."""
    source = tmp_path / 'bin'
    source.mkdir()
    for index in range(3100):
        (source / f'tool-{index}').write_text('tool')
    (source / 'python3').symlink_to('python3.10')
    (source / 'python3.10').write_text('host python')
    (source / 'python3.99').symlink_to('/missing')
    with BinProjection() as projection:
        args = projection.mounts('/sandbox-home/.venv', (source,))
        assert len(args) == 6
        projected = Path(args[4])
        assert (projected / 'python3').readlink() != Path('python3.10')
        assert (projected / 'python3.10').readlink().name == 'unsupported'
        assert (projected / 'python3.99').readlink().name == 'unsupported'
        assert (projected / 'python3.12').is_symlink()
        assert (projected / 'tool-0').read_text() == 'tool'
        assert (projected / 'tool-0').stat().st_ino != (source / 'tool-0').stat().st_ino
        (projected / 'tool-0').write_text('private change')
        assert (source / 'tool-0').read_text() == 'tool'
        assert projection.mounts('/sandbox-home/.venv', (source,)) == args
        root = Path(args[1])
    assert not root.exists()
    assert (source / 'python3').readlink() == Path('python3.10')


def test_projection_preserves_executable_mode(tmp_path: Path) -> None:
    """Copied tools retain their bytes and execute permissions with independent inodes."""
    source = tmp_path / 'bin'
    source.mkdir()
    executable = source / 'tool'
    executable.write_bytes(b'content')
    executable.chmod(0o751)
    with BinProjection() as projection:
        args = projection.mounts('/venv', (source,))
        copied = Path(args[4]) / 'tool'
        assert copied.read_bytes() == b'content'
        assert copied.stat().st_mode == executable.stat().st_mode
        assert copied.stat().st_ino != executable.stat().st_ino


def test_parallel_projection_preserves_links_and_subdirectories(tmp_path: Path) -> None:
    """Concurrent attempts receive one complete tree including relative helpers."""
    source = tmp_path / 'bin'
    source.mkdir()
    (source / 'helpers').mkdir()
    (source / 'helpers/tool').write_text('helper')
    (source / 'alias').symlink_to('helpers/tool')
    (source / 'broken').symlink_to('/missing')
    with BinProjection() as projection, ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: projection.mounts('/venv', (source,)), range(8)))
        assert all(args == results[0] for args in results)
        projected = Path(results[0][4])
        assert (projected / 'alias').readlink() == Path('helpers/tool')
        assert (projected / 'broken').readlink() == Path('/missing')
        assert (projected / 'helpers/tool').read_text() == 'helper'


def test_failed_projection_is_discarded_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed copy must not publish partially prepared mount sources."""
    source = tmp_path / 'bin'
    source.mkdir()
    (source / 'tool').write_text('tool')
    original = module.shutil.copy2
    attempted: list[Path] = []

    def fail(src: str, dst: str) -> str:
        """Simulate ENOSPC while copying a host executable."""
        attempted.append(Path(dst))
        raise OSError(errno.ENOSPC, 'full')

    with BinProjection() as projection:
        monkeypatch.setattr(module.shutil, 'copy2', fail)
        with pytest.raises(OSError, match='full'):
            projection.mounts('/venv', (source,))
        assert attempted and not attempted[0].parent.exists()
        monkeypatch.setattr(module.shutil, 'copy2', original)
        args = projection.mounts('/venv', (source,))
        assert (Path(args[4]) / 'tool').read_text() == 'tool'


def test_usrmerge_alias_is_mounted_once(tmp_path: Path) -> None:
    """A directory symlink must not duplicate the copied executable tree."""
    source = tmp_path / 'usr-bin'
    source.mkdir()
    (source / 'tool').write_text('tool')
    alias = tmp_path / 'bin'
    alias.symlink_to(source)
    with BinProjection() as projection:
        args = projection.mounts('/venv', (source, alias))
        assert len(args) == 6
        assert args[-1] == str(source)


def test_cancelled_preparation_can_be_retried(tmp_path: Path) -> None:
    """Cancellation does not retain a partial projection or poison a later build."""
    source = tmp_path / 'bin'
    source.mkdir()
    (source / 'tool').write_text('tool')
    cancel = threading.Event()
    cancel.set()
    with BinProjection(cancel) as projection:
        with pytest.raises(BenchmarkError) as caught:
            projection.mounts('/venv', (source,))
        assert caught.value.code == 'operation_cancelled'
        cancel.clear()
        args = projection.mounts('/venv', (source,))
        assert (Path(args[4]) / 'tool').read_text() == 'tool'


def test_cancellation_during_copy_discards_partial_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel after a real copy, remove its tree, and rebuild after clearing cancellation."""
    source = tmp_path / 'bin'
    source.mkdir()
    for name in ('a-tool', 'b-tool'):
        (source / name).write_text(name)
    cancel = threading.Event()
    original = module.shutil.copy2
    copied: list[Path] = []

    def copy_then_cancel(src: str, dst: str) -> str:
        """Request cancellation after the first destination file exists."""
        result = original(src, dst)
        copied.append(Path(dst))
        assert Path(dst).is_file()
        cancel.set()
        return result

    with BinProjection(cancel) as projection:
        monkeypatch.setattr(module.shutil, 'copy2', copy_then_cancel)
        with pytest.raises(BenchmarkError) as caught:
            projection.mounts('/venv', (source,))
        assert caught.value.code == 'operation_cancelled'
        assert len(copied) == 1
        assert not copied[0].parent.parent.exists()
        cancel.clear()
        monkeypatch.setattr(module.shutil, 'copy2', original)
        args = projection.mounts('/venv', (source,))
        for name in ('a-tool', 'b-tool'):
            assert (Path(args[4]) / name).read_text() == name
