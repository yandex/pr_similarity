"""Keep complete diagnostics in private DEBUG files and safe summaries in the console."""

import errno
import json
import logging
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from uuid import uuid4

from pydantic import ValidationError

if TYPE_CHECKING:
    from basket_collector.progress import Progress


def _validation_error(error: BaseException) -> ValidationError | None:
    """Find a wrapped validator without rendering a storage wrapper's rejected input."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__ or current.__context__
    return None


def friendly_error(error: BaseException) -> str:
    """Describe the failed operation without exposing validator input values or tracebacks."""
    validation = _validation_error(error)
    if validation is not None:
        problems = validation.errors(include_input=False, include_context=False, include_url=False)
        first = problems[0]
        path = '.'.join(str(value) for value in first['loc']) or '<root>'
        more = f' ({len(problems) - 1} more fields; see DEBUG log)' if len(problems) > 1 else ''
        return 'Invalid field ' + path + ': ' + first['msg'] + '. Check the configuration or saved artifact.' + more
    if isinstance(error, FileNotFoundError):
        return 'Required file is missing: ' + str(error.filename or '') + '; check CONFIG and template paths.'
    if isinstance(error, OSError):
        if error.errno == errno.ENOSPC:
            return 'Not enough disk space; free space and repeat the command.'
        if error.errno in (errno.EACCES, errno.EPERM):
            return 'Permission denied; check output/cache permissions and repeat the command.'
        return 'A file or process operation failed; check paths, disk space and permissions.'
    if getattr(error, 'code', None) is not None or type(error).__name__ in {
        'StorageError',
        'SourceChangedError',
        'LLMAuthenticationError',
        'LLMConfigurationError',
    }:
        return str(error)
    if isinstance(error, ValueError):
        # Config readers supply field/line context without including YAML snippets.
        text = str(error)
        if text.startswith(
            (
                'invalid YAML configuration',
                'duplicate YAML key:',
                'YAML mapping keys',
                'configuration must be',
                'smoke-live requires',
                'OVERWRITE=1 and RETRY_FAILED=1',
                'RETRY_FAILED=1 applies',
            )
        ):
            return text
        return 'Configuration or saved data is invalid; check the DEBUG file log for field details.'
    return 'Unexpected runtime failure; see the DEBUG file log and retry after repairing the cause.'


class _RedactedFormatter(logging.Formatter):
    """Keep configured environment credentials and credential-bearing URLs out of logs."""

    def __init__(self, secret_names: list[str]) -> None:
        """Capture known secrets in memory only, without saving their names or values."""
        super().__init__('%(asctime)s %(levelname)s %(name)s: %(message)s')
        self.secrets = [os.environ[name] for name in secret_names if os.environ.get(name)]

    def format(self, record: logging.LogRecord) -> str:
        """Redact after formatting, including any third-party diagnostic record."""
        return self.redact(super().format(record))

    def redact(self, rendered: str) -> str:
        """Use the same URL/credential redaction for console messages and file records."""
        for secret in sorted(self.secrets, key=len, reverse=True):
            rendered = rendered.replace(secret, '<redacted>')
        return re.sub(r'(https?://)[^\s/@]+(?::[^\s/@]*)?@', r'\1<redacted>@', rendered)


class _PrivateHandler(logging.FileHandler):
    """File I/O failures must not spill the raw record into stderr."""

    warned = False

    def handleError(self, record: logging.LogRecord) -> None:
        """Warn once using a constant message that contains no diagnostic payload."""
        if not self.warned:
            self.warned = True
            print('DEBUG file log could not be written; check disk space and permissions.', file=sys.stderr)


class _SourceWarnings(logging.Handler):
    """Hosting adapter warnings remain visible without rendering exception details."""

    def __init__(self, formatter: _RedactedFormatter, owner: 'InvocationLogs') -> None:
        """Bind the invocation redactor and a mandatory warning threshold."""
        super().__init__(logging.WARNING)
        self.redactor = formatter
        self.owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        """Show only the adapter's safe user-facing message."""
        self.owner.console('Warning: ' + self.redactor.redact(record.getMessage()), sys.stderr)


class InvocationLogs:
    """One invocation owns a unique file log and the console filtering threshold."""

    def __init__(self, output: Path, level: str = 'INFO', *, secret_names: list[str] | None = None) -> None:
        """Create private output without changing the root logger's other applications."""
        stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        directory = output / ('logs_' + stamp + '_' + uuid4().hex)
        directory.mkdir(parents=True, mode=0o700)
        self.path = directory / 'collector.log'
        self.path.touch(mode=0o600)
        self.handler = _PrivateHandler(self.path, encoding='utf-8')
        self.handler.setLevel(logging.DEBUG)
        self.formatter = _RedactedFormatter(
            list(set((secret_names or []) + ['GITHUB_TOKEN', 'GITLAB_TOKEN', 'OPENAI_API_KEY']))
        )
        self.handler.setFormatter(self.formatter)
        self.level = getattr(logging, level)
        self.progress: Progress | None = None
        self.previous: list[tuple[logging.Logger, int, bool, list[logging.Handler]]] = []
        for name in ('basket_collector', 'httpx', 'httpcore'):
            logger = logging.getLogger(name)
            self.previous.append((logger, logger.level, logger.propagate, logger.handlers[:]))
            logger.handlers = [self.handler]
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
        logger = logging.getLogger('basket_collector.source')
        self.previous.append((logger, logger.level, logger.propagate, logger.handlers[:]))
        logger.handlers = [self.handler, _SourceWarnings(self.formatter, self)]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

    def message(self, text: str, *, level: str = 'INFO', stream: TextIO | None = None) -> None:
        """Retain every message and show warnings/errors regardless of threshold."""
        priority = getattr(logging, level)
        logging.getLogger('basket_collector.user').log(priority, text)
        if priority >= logging.WARNING or priority >= self.level:
            safe = self.formatter.redact(text)
            self.console(safe, stream or sys.stdout)

    def console(self, safe: str, stream: TextIO) -> None:
        """Keep messages intact under the progress display's shared output lock."""
        if self.progress is not None:
            self.progress.write_message(safe, stream=stream)
        else:
            print(safe, file=stream, flush=True)

    def progress_snapshot(self, text: str) -> None:
        """Save a plain redacted heartbeat without printing a duplicate console line."""
        logging.getLogger('basket_collector.progress').info(self.formatter.redact(text))

    def diagnostic(self, error: BaseException) -> None:
        """Retain the stack and complete validation details without rejected input values."""
        validation = _validation_error(error)
        details = (
            json.dumps(validation.errors(include_input=False, include_context=False, include_url=False))
            if validation is not None
            else str(error)
        )
        stack = ''.join(traceback.format_tb(error.__traceback__))
        # Do not format chained parser errors: their string can contain raw YAML snippets.
        logging.getLogger('basket_collector.diagnostics').debug('%s: %s\n%s', type(error).__name__, details, stack)

    def close(self) -> None:
        """Restore logger ownership after the coordinator and every worker have stopped."""
        for logger, level, propagate, handlers in self.previous:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
        self.handler.close()
