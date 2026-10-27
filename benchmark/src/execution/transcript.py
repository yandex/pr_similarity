"""Native capture and disposable human transcripts with independent failure domains."""

import json
import os
import re
from pathlib import Path
from typing import Protocol

from benchmark.errors import BenchmarkError
from benchmark.execution.process import ProcessResult
from benchmark.json_types import JsonObject, JsonValue
from benchmark.models.base import json_value
from benchmark.models.evidence import StreamReceipt
from benchmark.persistence.storage import utc_now, write_json


class NativeTracker(Protocol):
    """Describe classification metadata independently of its concrete module identity."""

    warnings: list[str]
    stream_receipt: StreamReceipt | None

    def accept_event(self, value: JsonObject) -> None:
        """Classify a decoded native object independently of its storage sinks."""
        ...


class Transcript:
    """Route sanitized native lines before attempting any presentation work."""

    def __init__(
        self,
        directory: Path,
        tracker: NativeTracker,
        *,
        task_label: str | None = None,
        attempt_sequence: int | None = None,
        model: str | None = None,
        output_limit: int = 2000,
        secrets: list[str] | None = None,
    ) -> None:
        """Create live logs, retaining only counters and completeness metadata."""
        self.directory, self.tracker = directory, tracker
        self.output_limit = output_limit
        self.secrets = sorted({value for value in secrets or [] if value}, key=len, reverse=True)
        self.warnings: list[str] = []
        self.events_count = 0
        self.runtime_count = 0
        self.sealed = False
        directory.mkdir(parents=True, exist_ok=True)
        for name, failure in (
            ('events.jsonl', 'event_capture_failed'),
            ('runtime.log', 'runtime_capture_failed'),
            ('agent.log', 'transcript_failed'),
        ):
            try:
                (directory / name).write_text('', encoding='utf-8')
            except OSError:
                self._warn(failure)
        labels = [task_label, f'attempt {attempt_sequence}' if attempt_sequence is not None else None, model]
        self._human(' · '.join(label for label in labels if label) or 'OpenCode attempt')

    def _safe(self, value: str) -> str:
        """Remove terminal controls, retaining readable multiline blocks."""
        for secret in self.secrets:
            value = value.replace(secret, '<redacted>')
        value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
        return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value).replace('\r', '')

    def _warn(self, code: str) -> None:
        """Keep each completeness warning once without buffering native payloads."""
        if code not in self.warnings:
            self.warnings.append(code)

    def _append(self, name: str, content: str, failure: str) -> bool:
        """Flush live output and report sink failures without disabling classification."""
        try:
            with (self.directory / name).open('a', encoding='utf-8') as output:
                output.write(content)
                output.flush()
            return True
        except (OSError, ValueError):
            self._warn(failure)
            return False

    def _human(self, content: str) -> None:
        """Timestamp readable blocks while removing terminal controls and secrets."""
        lines = self._safe(content).splitlines() or ['']
        self._append(
            'agent.log',
            f'[{utc_now()}] {lines[0]}\n' + ''.join(f'    {line}\n' for line in lines[1:]),
            'transcript_failed',
        )

    def accept(self, stream: str, line: str) -> None:
        """Store every JSON object and classify independently from human formatting."""
        if self.sealed:
            raise ValueError('native streams are sealed')
        value = None
        if stream == 'stdout':
            try:
                value = json_value(json.loads(line))
                # Re-encode with strict JSON to reject nonfinite diagnostics.
                json.dumps(value, allow_nan=False)
            except (ValueError, TypeError):
                value = None
        if not isinstance(value, dict):
            if self._append('runtime.log', f'[{utc_now()}] [{stream}] {self._safe(line)}\n', 'runtime_capture_failed'):
                self.runtime_count += 1
            return

        def redact_value(item: JsonValue) -> JsonValue:
            """Redact decoded strings so JSON escaping cannot bypass known secrets."""
            if isinstance(item, str):
                for secret in self.secrets:
                    item = item.replace(secret, '<redacted>')
                return item
            if isinstance(item, dict):
                return {str(redact_value(key)): redact_value(content) for key, content in item.items()}
            if isinstance(item, list):
                return [redact_value(content) for content in item]
            return item

        value = redact_value(value)
        assert isinstance(value, dict)
        self.tracker.accept_event(value)
        content = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
        stored = self._append('events.jsonl', content, 'event_capture_failed')
        if stored:
            self.events_count += 1
        try:
            self.render_event(value, self.events_count if stored else None)
        except Exception:
            # No formatter exception may disable later error or session classification.
            self._warn('transcript_failed')

    def render_event(self, event: JsonObject, raw_line: int | None) -> None:
        """Render text and tool actions; unrecognized event types stay in JSONL."""
        kind = event.get('type')
        part = event.get('part')
        part = part if isinstance(part, dict) else {}
        if kind == 'text':
            text = part.get('text', event.get('text'))
            if isinstance(text, str):
                self._human(text)
        elif kind == 'tool_use':
            state = part.get('state')
            state = state if isinstance(state, dict) else {}
            inputs = state.get('input')
            inputs = inputs if isinstance(inputs, dict) else {}
            target = next(
                (inputs[key] for key in ('filePath', 'path', 'command', 'url', 'pattern') if key in inputs), ''
            )
            self._human(f"Tool {part.get('tool', 'unknown')} · {target} · {state.get('status', 'unknown')}")
            result = state.get('output', state.get('error'))
            if result is not None:
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                if len(text) > self.output_limit:
                    reference = f'events.jsonl:{raw_line}' if raw_line is not None else 'raw capture unavailable'
                    text = text[: self.output_limit] + f'\n[truncated; full output: {reference}]'
                self._human(text)
        elif kind == 'error':
            self._human('Native error: ' + json.dumps(event.get('error'), ensure_ascii=False))
        elif kind == 'step_finish':
            self._human('Step finished: ' + str(part.get('reason', 'completed')))

    def finish(self, result: ProcessResult) -> StreamReceipt:
        """Seal streams and save explicit completeness without interpreting absence."""
        if self.sealed:
            assert self.tracker.stream_receipt is not None
            return self.tracker.stream_receipt
        self._human(f'Process {result.status} · exit {result.exit_code}')
        for name, failure in (('events.jsonl', 'event_capture_failed'), ('runtime.log', 'runtime_capture_failed')):
            try:
                with (self.directory / name).open('rb') as stream:
                    os.fsync(stream.fileno())
            except OSError:
                self._warn(failure)
        self.sealed = True
        self.warnings.extend(code for code in result.warnings if code not in self.warnings)
        self.warnings.extend(code for code in self.tracker.warnings if code not in self.warnings)
        drained = 'output_pipe_not_closed' not in self.warnings and 'output_callback_failed' not in self.warnings
        receipt = StreamReceipt(
            schema_version=1,
            status='not_started' if result.exit_code is None and result.status == 'cancelled' else 'sealed',
            events_complete=drained and 'event_capture_failed' not in self.warnings,
            runtime_complete=drained and 'runtime_capture_failed' not in self.warnings,
            transcript_complete='transcript_failed' not in self.warnings,
            events_count=self.events_count,
            runtime_lines=self.runtime_count,
            warnings=self.warnings,
        )
        try:
            write_json(self.directory / 'stream_receipt.json', receipt)
        except BenchmarkError:
            self._warn('stream_receipt_failed')
            receipt.warnings = list(self.warnings)
            receipt.events_complete = False
            receipt.runtime_complete = False
        self.tracker.stream_receipt = receipt
        return receipt
