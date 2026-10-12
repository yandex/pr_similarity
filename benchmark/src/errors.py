"""Safe user-facing errors; detailed diagnostics belong in file logs."""

import sqlite3
import subprocess

import httpx


class BenchmarkError(Exception):
    """Identify a failure without embedding credentials or arbitrary payloads."""

    captured_diff: bytes | None = None
    files_count: int | None = None

    def __init__(self, code: str, message: str, *, exit_code: int = 2, paths: list[str] | None = None) -> None:
        """Attach a safe code, console message and process exit status."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.paths = paths or []


# Failures from input validation, native processes, storage and HTTP adapters.
OPERATION_ERRORS = (
    BenchmarkError,
    OSError,
    ValueError,
    TypeError,
    LookupError,
    RuntimeError,
    ArithmeticError,
    ImportError,
    AssertionError,
    AttributeError,
    sqlite3.Error,
    subprocess.SubprocessError,
    httpx.HTTPError,
)
