"""Interpret real PTY output to check physical rows, viewport updates and terminal resizing."""

import errno
import fcntl
import os
import re
import struct
import termios
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

import pytest

from basket_collector.runtime.progress import Progress


class Screen:
    """Interpret the terminal controls emitted by the collector and detect automatic wrapping."""

    def __init__(self, columns: int, lines: int = 24) -> None:
        """Create a bounded screen with its cursor at the first physical cell."""
        self.columns = columns
        self.lines = lines
        self.rows = [''] * lines
        self.row = 0
        self.column = 0
        self.wraps = 0

    def feed(self, output: str) -> None:
        """Apply cursor movement, line erasure and ordinary Unicode output independently of the renderer."""
        for token in re.split(r'(\x1b\[[0-9;]*[A-Za-z])', output):
            if token.startswith('\x1b['):
                if token.endswith('A'):
                    self.row = max(0, self.row - int(token[2:-1] or '1'))
                elif token == '\x1b[2K':
                    self.rows[self.row] = ''
                else:
                    raise AssertionError(f'unexpected terminal command: {token!r}')
                continue
            for char in token:
                if char == '\r':
                    self.column = 0
                elif char == '\n':
                    self._newline()
                elif unicodedata.combining(char):
                    continue
                else:
                    cells = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
                    if self.column + cells > self.columns:
                        self.wraps += 1
                        self.column = 0
                        self._newline()
                    row = self.rows[self.row].ljust(self.column)
                    self.rows[self.row] = row[: self.column] + char + ' ' * (cells - 1) + row[self.column + cells :]
                    self.column += cells

    def _newline(self) -> None:
        """Scroll a physical terminal line at the bottom of the screen."""
        self.row += 1
        if self.row == self.lines:
            self.rows.pop(0)
            self.rows.append('')
            self.row -= 1

    def resize(self, columns: int) -> None:
        """Apply a terminal resize without allowing stale text outside the new viewport."""
        self.columns = columns
        self.rows = [row[:columns] for row in self.rows]
        self.column = min(self.column, columns - 1)


def set_size(stream: TextIO, columns: int, lines: int = 24) -> None:
    """Set the dimensions of the actual stream observed by the renderer."""
    fcntl.ioctl(stream.fileno(), termios.TIOCSWINSZ, struct.pack('HHHH', lines, columns, 0, 0))


def drain(master: int, screen: Screen) -> str:
    """Consume flushed PTY output without waiting for a background process."""
    pieces = []
    while True:
        try:
            pieces.append(os.read(master, 65536))
        except BlockingIOError:
            break
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            break
    output = b''.join(pieces).decode()
    screen.feed(output)
    return output


@contextmanager
def terminal(columns: int) -> Iterator[tuple[TextIO, int, Screen]]:
    """Own both ends of a real terminal while keeping all test output private."""
    master, slave = os.openpty()
    os.set_blocking(master, False)
    try:
        with os.fdopen(slave, 'w', buffering=1) as stream:
            set_size(stream, columns)
            yield stream, master, Screen(columns or 80)
    finally:
        os.close(master)


@pytest.mark.parametrize('width', [0, 40, 80, 120])
def test_tty_panel_updates_without_physical_wraps(width: int) -> None:
    """Exercise large counters, Unicode, warnings, shrinking details and interrupted finish."""
    with terminal(width) as (stream, master, screen):
        now = [0.0]
        progress = Progress('auto', stream=stream, clock=lambda: now[0])
        progress.basket(1000)
        progress.start('filter', 'local')
        drain(master, screen)
        now[0] += 1
        progress.update(
            n=104,
            total=221,
            succeeded=16,
            rejected=88,
            failed=0,
            pending=117,
            active=30,
            wait='Git fetch ' + '界e\u0301' * 100,
        )
        drain(master, screen)
        assert any('104/221' in row for row in screen.rows)
        progress.write_message('WARNING fixture', stream=stream)
        drain(master, screen)
        assert any('WARNING fixture' in row for row in screen.rows)
        now[0] += 1
        progress.update(active=0, wait='')
        drain(master, screen)
        progress.finish('interrupted')
        progress.finish_basket('interrupted')
        progress.close()
        drain(master, screen)
        assert screen.wraps == 0
        assert any('interrupted' in row for row in screen.rows)


def test_resize_and_small_viewport_leave_no_stale_panel_rows() -> None:
    """Read changing stream dimensions and clear old detail rows when the viewport shrinks."""
    with terminal(120) as (stream, master, screen):
        now = [0.0]
        progress = Progress('auto', stream=stream, clock=lambda: now[0])
        progress.start('generate', 'tasks')
        progress.update(n=7, total=100, wait='long ' * 40, active=50)
        drain(master, screen)
        set_size(stream, 40, 4)
        screen.resize(40)
        now[0] += 1
        progress.update(n=8, wait='', active=0)
        drain(master, screen)
        assert any('8/100' in row for row in screen.rows)
        assert not any('long' in row for row in screen.rows)
        assert screen.wraps == 0
        assert len([row for row in screen.rows if row.strip()]) <= 3
        progress.close()
        drain(master, screen)
