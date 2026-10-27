"""Invocation-owned bin directories with bounded bubblewrap mount arguments."""

import re
import shutil
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Self

from benchmark.execution.cancellation import check_cancelled

_ROOT = '/benchmark-python-bin'
_PYTHON_NAMES = frozenset({'python', 'python3', 'python3.12'})
_PIP_NAMES = frozenset({'pip', 'pip3', 'pip3.12'})


class BinProjection:
    """Share one private projection across concurrent attempts, never across runs."""

    def __init__(self, cancel: threading.Event | None = None) -> None:
        """Delay disk allocation until a sandbox actually needs Python mounts."""
        self._cancel = cancel
        self._lock = threading.Lock()
        self._temporary: TemporaryDirectory[str] | None = None
        self._args: list[str] | None = None

    def __enter__(self) -> Self:
        """Allow explicit ownership in standalone probes and tests."""
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Remove private copies after their last sandbox has exited."""
        self.close()

    def close(self) -> None:
        """Release invocation storage; a later invocation builds a fresh projection."""
        with self._lock:
            if self._temporary is not None:
                self._temporary.cleanup()
            self._temporary = None
            self._args = None

    def mounts(self, venv: str, directories: tuple[Path, ...] | None = None) -> list[str]:
        """Publish mounts only after all copies and launcher replacements succeed.

        Args:
            venv: Virtual environment path inside the sandbox.
            directories: Host command directories to project; defaults to standard
                bin directories. These and venv apply only to the first successful
                preparation after construction or close().

        Returns:
            A fresh list of bubblewrap arguments for the saved projection.
            Later calls reuse the prepared directories until close().

        Raises:
            BenchmarkError: If cancellation is requested during preparation.
            OSError: If creating or copying the projection fails. Partial storage
                is removed before the error propagates, allowing a later retry.

        """
        with self._lock:
            if self._args is None:
                temporary = TemporaryDirectory(prefix='benchmark-python-bin-')
                try:
                    args = self._prepare(
                        Path(temporary.name),
                        venv,
                        directories or tuple(map(Path, ('/usr/bin', '/usr/local/bin', '/bin'))),
                    )
                except BaseException:
                    temporary.cleanup()
                    raise
                self._temporary, self._args = temporary, args
            return list(self._args)

    def _copy(self, source: str, destination: str) -> str:
        """Stop preparation between file copies when the invocation is cancelled."""
        check_cancelled(self._cancel)
        return shutil.copy2(source, destination)

    def _prepare(self, root: Path, venv: str, directories: tuple[Path, ...]) -> list[str]:
        """Keep non-Python executable locations and symlink text unchanged."""
        scripts = {
            'python': f'#!/bin/sh\nexec {venv}/bin/python "$@"\n',
            'pip': f'#!/bin/sh\nexec {venv}/bin/python -m pip "$@"\n',
            'unsupported': '#!/bin/sh\necho "This attempt supports Python 3.12 only; use python3.12." >&2\nexit 126\n',
        }
        for name, content in scripts.items():
            path = root / name
            path.write_text(content)
            path.chmod(0o755)
        args = ['--ro-bind', str(root), _ROOT]
        seen: set[Path] = set()
        for original in directories:
            destination = original.resolve()
            if not original.is_dir() or destination in seen:
                continue
            seen.add(destination)
            projected = root / f'bin-{len(seen)}'
            projected.mkdir()
            entries = {path.name for path in original.iterdir()} | _PYTHON_NAMES | _PIP_NAMES
            for entry in sorted(entries):
                check_cancelled(self._cancel)
                path = projected / entry
                if entry in _PYTHON_NAMES:
                    path.symlink_to(_ROOT + '/python')
                elif entry in _PIP_NAMES:
                    path.symlink_to(_ROOT + '/pip')
                elif re.fullmatch(r'(?:python|pip)3\.\d+(?:[a-z]*)?', entry):
                    path.symlink_to(_ROOT + '/unsupported')
                else:
                    source = original / entry
                    if source.is_symlink():
                        path.symlink_to(source.readlink())
                    elif source.is_dir():
                        shutil.copytree(source, path, symlinks=True, copy_function=self._copy)
                    else:
                        self._copy(str(source), str(path))
            args.extend(['--ro-bind', str(projected), str(destination)])
        return args
