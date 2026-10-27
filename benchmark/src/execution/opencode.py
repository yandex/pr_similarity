"""Native OpenCode configuration, separate setup and one new session per attempt."""

import json
import logging
import threading
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from benchmark.config import RunConfig
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.execution.agent_input import (
    _native_resources,
    install_agent_input,
    save_effective,
)
from benchmark.execution.process import ProcessResult, run_process
from benchmark.execution.sandbox import Sandbox
from benchmark.execution.transcript import Transcript
from benchmark.json_types import JsonObject
from benchmark.models.base import WireModel, json_value
from benchmark.models.evidence import StreamReceipt
from benchmark.models.report import ReportAgentInput
from benchmark.persistence.storage import (
    atomic_write,
    bytes_sha256,
    parse_json,
    utc_now,
    write_json,
)


class EffectiveConfigResult(WireModel):
    """Keep optional native configuration capture separate from agent execution."""

    status: str
    path: str | None
    reason: str | None


@dataclass
class EventTracker:
    """Session identity comes from launch events, never a first-root DB heuristic."""

    root_session_id: str | None = None
    errors: list[JsonObject] = field(default_factory=list)
    events_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    stream_receipt: StreamReceipt | None = None
    _ambiguous_session: bool = False

    def accept(self, line: str) -> None:
        """Capture launch session identity and sanitized native events."""
        try:
            value = json_value(json.loads(line))
        except ValueError:
            return
        if isinstance(value, dict):
            self.accept_event(value)
            if self.events_path is not None:
                try:
                    with self.events_path.open('a', encoding='utf-8') as output:
                        output.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
                except OSError:
                    if 'event_capture_failed' not in self.warnings:
                        self.warnings.append('event_capture_failed')

    def accept_event(self, value: JsonObject) -> None:
        """Classify native events without depending on evidence or presentation sinks."""
        error = value.get('error')
        if value.get('type') == 'error' and isinstance(error, dict):
            self.errors.append(error)
        if value.get('type') not in {'step_start', 'step_finish', 'text', 'reasoning', 'tool_use', 'error'}:
            return
        session = value.get('sessionID')
        if isinstance(session, str) and session and not self._ambiguous_session:
            if self.root_session_id is None:
                self.root_session_id = session
            elif self.root_session_id != session and 'ambiguous_root_session' not in self.warnings:
                self.warnings.append('ambiguous_root_session')
                self._ambiguous_session = True
                self.root_session_id = None


@dataclass
class Invocation:
    """Own the private native configuration, setup and agent launch."""

    config: RunConfig
    sandbox: Sandbox
    artifacts: Path
    logs: Path
    auth_content: str | None = None
    task_label: str | None = None
    attempt_sequence: int | None = None
    _environment: dict[str, str] | None = None
    agent_started: bool = False
    run_directory: Path | None = None
    agent_input: ReportAgentInput | None = None

    def prepare(self, task: str) -> bytes:
        """Agent-facing files live outside the scored source tree."""
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.sandbox.runtime_files.mkdir(parents=True, exist_ok=True)
        agent = self.config.data.agent
        credentials = self.config.credential_environment()
        secret_values = list(credentials.values())
        if self.auth_content is not None:
            secret_values.append(json.loads(self.auth_content)['openai']['access'])

        def save_public(path: Path, content: bytes) -> None:
            """Save a declared input only when it contains no known credential."""
            if any(value and value.encode() in content for value in secret_values):
                raise BenchmarkError(
                    'resource_secret', 'A declared input contains a credential; use environment bindings.', exit_code=1
                )
            atomic_write(path, content)

        if self.run_directory is not None:
            assert self.agent_input is not None
            native = install_agent_input(
                self.run_directory, self.agent_input, self.config.identity(), self.sandbox.runtime_files
            )
            instructions = (self.sandbox.runtime_files / 'task_instructions.txt').read_text(encoding='utf-8')
        elif agent.instructions_file is None:
            instructions = files('benchmark').joinpath('resources', 'task_instructions.txt').read_text(encoding='utf-8')
        else:
            instructions = Path(agent.instructions_file).read_text(encoding='utf-8')
        if bytes_sha256(instructions.encode('utf-8')) != self.config.identity().instructions.sha256:
            raise BenchmarkError('resource_changed', 'Task instructions changed after config validation.', exit_code=1)
        prompt = (instructions + '\n\n' + task).encode('utf-8')
        save_public(self.artifacts / 'prompt.txt', prompt)
        if self.run_directory is None:
            native = _native_resources(self.config, self.sandbox.runtime_files)
            write_json(self.sandbox.runtime_files / 'config.json', native, preserve_order=True)
            for source in sorted(self.sandbox.runtime_files.rglob('*')):
                if source.is_file():
                    save_public(
                        self.artifacts / 'runtime' / source.relative_to(self.sandbox.runtime_files), source.read_bytes()
                    )
        write_json(
            self.artifacts / 'launch.json',
            {
                'runtime': agent.runtime,
                'created_at': utc_now(),
                'cwd': self.sandbox.cwd.relative_to(self.sandbox.source).as_posix(),
                'argv': ['/benchmark-bin/opencode', 'run', '--format', 'json'],
                'prompt_transport': 'stdin',
                'native_config': 'agent/opencode.json' if self.run_directory is not None else 'runtime/config.json',
                'agent_input': self.agent_input,
                'environment_names': sorted({**agent.env, **credentials}),
            },
        )
        if agent.setup is not None and self.run_directory is None:
            content = Path(agent.setup.script).read_bytes()
            setup_identity = self.config.identity().setup
            assert setup_identity is not None
            if bytes_sha256(content) != setup_identity.sha256:
                raise BenchmarkError('resource_changed', 'Setup script changed after config validation.', exit_code=1)
            save_public(self.artifacts / 'setup.sh', content)
            atomic_write(self.sandbox.runtime_files / 'setup.sh', content)
        bindings = {**agent.env, **credentials}
        self._environment = self.sandbox.environment(
            bindings, config_content=json.dumps(native, ensure_ascii=False), auth_content=self.auth_content
        )
        return prompt

    def secrets(self) -> list[str]:
        """Return only known in-memory values for stream redaction."""
        assert self._environment is not None
        values = [self._environment[key] for key in self.config.data.agent.credentials]
        if self.auth_content is not None:
            values.append(json.loads(self.auth_content)['openai']['access'])
        return values

    def dump_effective_config(self, cancel: threading.Event) -> EffectiveConfigResult:
        """Warn on diagnostic failure without changing agent outcome or retries."""
        assert self._environment is not None
        lines = []
        log = self.logs / 'effective-config.log'
        try:
            result = run_process(
                self.sandbox.command(['/benchmark-bin/opencode', 'debug', 'config']),
                env=self._environment,
                cwd=self.sandbox.source,
                timeout=self.config.data.provisioning.download_timeout_seconds,
                grace=self.config.data.shutdown.grace_seconds,
                cancel=cancel,
                log=log,
                secrets=self.secrets(),
                on_stdout=lines.append,
                log_stdout=False,
            )
            if result.status != 'completed':
                raise ValueError('config dump did not complete')

            data = parse_json(('\n'.join(lines)).encode('utf-8'))
            if self.run_directory is not None:
                effective = save_effective(self.run_directory, data)
                effective_path = effective.path
            else:
                write_json(self.artifacts / 'effective_config.json', data, preserve_order=True)
                effective_path = 'effective_config.json'
            write_json(
                self.artifacts / 'effective-config.receipt.json',
                {
                    'status': 'saved',
                    'runtime': self.config.data.agent.runtime,
                    'created_at': utc_now(),
                    'method': 'opencode debug config',
                    'cwd': self.sandbox.cwd.relative_to(self.sandbox.source).as_posix(),
                    'limitations': ['Repository config inheritance is part of the frozen basket environment.'],
                },
            )
            return EffectiveConfigResult(status='available', path=effective_path, reason=None)
        except OPERATION_ERRORS as error:
            if lines:
                try:
                    with log.open('a', encoding='utf-8') as output:
                        output.write('\n[failed config stdout]\n' + '\n'.join(lines) + '\n')
                except OSError:
                    logging.getLogger(__name__).debug('Cannot save failed config stdout', exc_info=True)
            logging.getLogger(__name__).debug('Effective config dump failed', exc_info=True)
            try:
                write_json(
                    self.artifacts / 'effective-config.receipt.json',
                    {
                        'status': 'warning',
                        'runtime': self.config.data.agent.runtime,
                        'created_at': utc_now(),
                        'method': 'opencode debug config',
                        'code': getattr(error, 'code', 'effective_config_unavailable'),
                        'message': 'Effective config was not captured; the agent can continue.',
                    },
                )
            except BenchmarkError:
                logging.getLogger(__name__).debug('Cannot save optional config diagnostic receipt', exc_info=True)
            return EffectiveConfigResult(
                status='unavailable',
                path=None,
                reason='Effective OpenCode config could not be dumped; see the diagnostic log.',
            )

    def setup(self, cancel: threading.Event) -> ProcessResult | None:
        """Run the optional setup with its separate timeout and log."""
        setup = self.config.data.agent.setup
        if setup is None:
            return None
        assert self._environment is not None
        return run_process(
            self.sandbox.command(['/bin/bash', '/benchmark-runtime/setup.sh']),
            env=self._environment,
            cwd=self.sandbox.source,
            timeout=setup.timeout_seconds,
            grace=self.config.data.shutdown.grace_seconds,
            cancel=cancel,
            log=self.logs / 'setup.log',
            secrets=self.secrets(),
        )

    def run(self, prompt: bytes, cancel: threading.Event) -> tuple[ProcessResult, EventTracker]:
        """No --continue, --session, --attach, auto permission or sharing overrides."""
        assert self._environment is not None
        tracker = EventTracker()
        model = self.config.data.agent.config.opencode_json.get('model')
        if model is not None and not isinstance(model, str):
            raise TypeError('Native model label must be a string.')
        transcript = Transcript(
            self.logs,
            tracker,
            task_label=self.task_label,
            attempt_sequence=self.attempt_sequence,
            model=model,
            secrets=self.secrets(),
        )

        def started() -> None:
            """Record a successfully spawned native process before draining output."""
            self.agent_started = True

        result = run_process(
            self.sandbox.command(['/benchmark-bin/opencode', 'run', '--format', 'json', '--log-level', 'DEBUG']),
            env=self._environment,
            cwd=self.sandbox.source,
            timeout=self.config.data.agent.timeout_seconds,
            grace=self.config.data.shutdown.grace_seconds,
            cancel=cancel,
            log=self.logs / 'runtime.log',
            secrets=self.secrets(),
            stdin=prompt,
            on_output=transcript.accept,
            on_start=started,
        )
        if tracker.errors and result.status == 'completed':
            result = ProcessResult('error', result.exit_code, result.duration_seconds, result.warnings)
        receipt = transcript.finish(result)
        result = ProcessResult(result.status, result.exit_code, result.duration_seconds, receipt.warnings)
        return result, tracker
