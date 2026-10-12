"""Registered native execution, immutable sealing and independent host scoring."""

import copy
import json
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from benchmark.artifact_refs import ArtifactReference, reference
from benchmark.agent_input import validate_agent_input
from benchmark.auth import auth_snapshot
from benchmark.commands import Command
from benchmark.config import RunConfig
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.git import GitCache
from benchmark.layout import TaskRegistration, artifact_path, attempt_path
from benchmark.lifecycle import Stop
from benchmark.opencode import EventTracker, Invocation
from benchmark.process import ProcessResult
from benchmark.provisioning import Runtime
from benchmark.result import pending_task, unavailable_scoring
from benchmark.sandbox import Sandbox
from benchmark.score_process import score_saved
from benchmark.storage import atomic_write, bytes_sha256, read_json, utc_now, write_json
from benchmark.telemetry import capture_telemetry, unavailable_telemetry
from benchmark.triplets import read_triplets, save_triplets
from benchmark.workspace import capture_changes, prepare_workspace, revision_states


def _process_data(result: ProcessResult | None, *, skipped: bool = False) -> dict:
    """Convert a process outcome without inventing measurements."""
    if result is None:
        return {'status': 'skipped' if skipped else 'not_started', 'exit_code': None, 'duration_seconds': None}
    return {'status': result.status, 'exit_code': result.exit_code, 'duration_seconds': result.duration_seconds}


def _oauth(config: RunConfig, *, before_setup: bool, cancel: threading.Event | None = None) -> str | None:
    """Build an access-only snapshot that covers the planned native work."""
    agent = config.data['agent']
    auth = agent['auth']
    if auth['mode'] == 'api_key':
        return None
    setup_time = agent['setup']['timeout_seconds'] if before_setup and agent['setup'] else 0
    return auth_snapshot(
        Path(auth['profile']),
        setup_time + agent['timeout_seconds'] + config.data['shutdown']['grace_seconds'],
        refresh_timeout=auth['refresh_timeout_seconds'],
        expiry_margin=auth['expiry_margin_seconds'],
        cancel=cancel,
    )


def _write_closed(path: Path, value: dict) -> None:
    """Publish one immutable document without replacing any existing fact."""
    if path.exists() or path.is_symlink():
        raise BenchmarkError(
            'immutable_attempt_conflict', 'Attempt evidence already exists; preserve its original bytes.'
        )
    write_json(path, value)


def _reserved_start(
    run_dir: Path, registration: TaskRegistration, basket_row: dict, identity: dict, attempt: dict
) -> Path:
    """Verify coordinator registration and its persisted native reservation."""
    relative = attempt_path(registration, attempt['sequence'])
    start = read_json(artifact_path(run_dir, (relative / 'start.json').as_posix(), must_exist=True))
    if (
        registration['id'] != basket_row['id']
        or registration['basket_row'] != basket_row
        or start.get('schema_family') != 'pr_similarity_os.start'
        or start.get('schema_version') != 1
        or start.get('run_id') != identity['run_id']
        or start.get('task_id') != registration['id']
        or start.get('attempt') != attempt
        or start.get('basket_sha256') != identity['basket_sha256']
        or start.get('config_identity_sha256') != identity['config_identity_sha256']
        or attempt['finished_at'] is not None
    ):
        raise BenchmarkError('attempt_start_mismatch', 'Native attempt differs from its reserved immutable start.')
    for name in ('seal.json', 'scoring.json', 'outcome.json'):
        path = run_dir / relative / name
        if path.exists() or path.is_symlink():
            raise BenchmarkError(
                'immutable_attempt_conflict', 'Closed attempt requires host recovery, not native replay.'
            )
    return relative


class _Attempt:
    """Own one reserved native workspace without writing scheduler selections."""

    def __init__(
        self,
        config: RunConfig,
        basket_row: dict,
        run_dir: Path,
        report_identity: dict,
        runtime: Runtime,
        bwrap: Path,
        command: Command,
        attempt: dict,
        stop: Stop,
        *,
        registration: TaskRegistration,
    ) -> None:
        """Bind validated start ownership and private phase state to one worker."""
        self.relative = _reserved_start(run_dir, registration, basket_row, report_identity, attempt)
        self.artifacts = run_dir / self.relative
        self.logs = self.artifacts
        self.config, self.basket_row, self.run_dir = config, basket_row, run_dir
        self.report_identity = copy.deepcopy(report_identity)
        self.agent_input = read_json(run_dir / "run.json")["agent_input"]
        validate_agent_input(run_dir, self.agent_input, config.identity())
        self.runtime, self.bwrap, self.command = runtime, bwrap, command
        self.attempt, self.stop = copy.deepcopy(attempt), stop
        self.registration = copy.deepcopy(registration)
        self.row = pending_task(basket_row)
        self.row.update(processing_status='running', final_attempt=self.attempt)
        self.row['artifacts']['outcome'] = (self.relative / 'outcome.json').as_posix()
        self.row['scoring'] = unavailable_scoring('unavailable')
        self.stage = 'prepare'
        self.tracker: EventTracker | None = None
        self.invocation: Invocation | None = None
        self.opencode_started = False
        self.archive_complete = True
        self.fatal: dict | None = None
        self.provider_error: BenchmarkError | None = None
        self.scoring_error: dict | None = None
        self.native_timing: dict[str, str | float | None] = {
            'started_at': None,
            'finished_at': None,
            'duration_seconds': None,
        }
        self.evidence: dict = {}
        self.seal: dict | None = None
        self.seal_reference: ArtifactReference | None = None
        self.scoring_reference: ArtifactReference | None = None
        self.label = f'Task {registration['ordinal']:03d} · attempt {attempt['sequence']}'

    def execute(self) -> dict:
        """Capture source and usage exactly once before sealing and host scoring."""
        with TemporaryDirectory(prefix='benchmark-attempt-') as temporary:
            self.private = Path(temporary)
            try:
                if self.stop.is_set():
                    raise BenchmarkError(
                        'operation_cancelled', 'Task stopped before native preparation.', exit_code=130
                    )
                self.prepare()
                self.agent()
            except OPERATION_ERRORS as error:
                self.failed(error)
            finally:
                self.attempt['finished_at'] = self.native_timing['finished_at'] or utc_now()
                if hasattr(self, 'workspace'):
                    try:
                        self.capture()
                    except OPERATION_ERRORS as error:
                        self.failed(error)
                try:
                    self.telemetry()
                except OPERATION_ERRORS as error:
                    self.command.diagnostic(error)
                    self.archive_complete = False
                    self.row['telemetry'] = unavailable_telemetry('telemetry_unavailable', 'Usage could not be saved.')
                    self.evidence['usage'] = {'status': 'incomplete', 'reason': 'telemetry_receipt_failed'}
                self.close_native()
        self.score()
        return self.finish()

    def prepare(self) -> None:
        """Export only before source and retain private HOME through native capture."""
        self.command.phase('git', self.row['id'])
        git = self.config.data['git']
        self.cache = GitCache(
            Path(self.config.data['provisioning']['cache_dir']) / 'git',
            self.basket_row['source']['fetch_url'],
            timeout=git['timeout_seconds'],
            initial_depth=git['initial_depth'],
            max_depth=git['max_depth'],
            max_transfer_bytes=git['max_transfer_bytes'],
            stop=self.stop,
        )
        self.cache.ensure([self.row['before'], self.row['after']], fetch_refs=self.basket_row['source']['fetch_refs'])
        self.cache.verify_reference(self.basket_row)
        self.workspace = prepare_workspace(
            self.cache, self.row['before'], self.private / 'source', self.row['workspace']
        )
        auth = _oauth(self.config, before_setup=True, cancel=self.stop)
        if auth is not None:
            self.command.add_secret(json.loads(auth)['openai']['access'])
        sandbox = Sandbox(
            str(self.bwrap),
            self.workspace.root,
            self.workspace.cwd,
            self.private / 'home',
            self.private / 'runtime',
            self.runtime.binary,
        )
        self.invocation = Invocation(
            self.config,
            sandbox,
            self.artifacts,
            self.logs,
            auth,
            task_label=f'Task {self.registration['ordinal']:03d}',
            attempt_sequence=self.attempt['sequence'],
            run_directory=self.run_dir,
            agent_input=self.agent_input,
        )
        self.prompt = self.invocation.prepare(self.row['agent_task'])
        for value in self.invocation.secrets():
            self.command.add_secret(value)
        self.command.phase('setup', self.row['id'])
        result = self.invocation.setup(self.stop)
        self.row['setup'] = _process_data(result, skipped=result is None)
        if result is not None:
            self.row['artifacts']['setup_log'] = (self.logs / 'setup.log').relative_to(self.run_dir).as_posix()
            self.evidence['setup'] = {'status': result.status, 'warnings': result.warnings}
            if result.status == 'cancelled':
                raise BenchmarkError('operation_cancelled', 'Setup was cancelled.', exit_code=130)
            if result.status != 'completed':
                raise BenchmarkError('setup_failed', 'Setup failed or timed out; see its separate log.', exit_code=1)
        self.command.phase('config', self.row['id'])
        dump = self.invocation.dump_effective_config(self.stop)
        if dump['status'] == 'available':
            self.row['artifacts']['effective_config'] = {
                'status': 'saved',
                'path': dump['path'],
                'message': None,
            }
        else:
            self.row['artifacts']['effective_config'] = {'status': 'warning', 'path': None, 'message': dump['reason']}
            self.command.message(f'{self.label}: Effective native config is unavailable.', level='WARNING')
        if self.stop.is_set():
            raise BenchmarkError('operation_cancelled', 'Task stopped before native execution.', exit_code=130)
        auth = _oauth(self.config, before_setup=False, cancel=self.stop)
        if auth is not None:
            self.command.add_secret(json.loads(auth)['openai']['access'])
            self.invocation.auth_content = auth
            assert self.invocation._environment is not None
            self.invocation._environment['OPENCODE_AUTH_CONTENT'] = auth

    def agent(self) -> None:
        """Measure native execution and classify errors without deciding retries."""
        self.stage = 'agent'
        self.command.phase('agent', self.row['id'])
        assert self.invocation is not None
        started = utc_now()
        try:
            result, self.tracker = self.invocation.run(self.prompt, self.stop)
            self.row['execution'] = _process_data(result)
            if result.status == 'cancelled':
                self.row['processing_status'] = 'interrupted'
            self.native_timing['duration_seconds'] = result.duration_seconds if self.invocation.agent_started else None
        finally:
            self.opencode_started = self.invocation.agent_started
            if self.opencode_started:
                self.native_timing.update(started_at=started, finished_at=utc_now())
        self.row['artifacts']['agent_log'] = (self.relative / 'agent.log').as_posix()
        if (self.logs / 'events.jsonl').is_file():
            self.row['artifacts']['agent_events'] = (self.relative / 'events.jsonl').as_posix()
        self.classify_errors()
        if self.provider_error is not None:
            self.failed(self.provider_error)

    def classify_errors(self) -> None:
        """Keep auth precedence, infrastructure 5xx policy and ordinary agent errors separate."""
        if self.tracker is None or not self.tracker.errors:
            return
        failures = []
        for error in self.tracker.errors:
            data = error.get('data')
            data = data if isinstance(data, dict) else {}
            status_code = data.get('statusCode')
            if status_code in (401, 403) or error.get('name') in ('ProviderAuthError', 'AuthenticationError'):
                failures.append(
                    BenchmarkError('auth_required', 'Provider rejected credentials; renew them and resume.')
                )
            elif type(status_code) is int and 500 <= status_code < 600:
                failures.append(BenchmarkError('provider_unavailable', 'Provider could not complete the request.'))
            else:
                failures.append(BenchmarkError('agent_error', 'OpenCode reported a terminal agent error.'))
        priority = {'auth_required': 0, 'provider_unavailable': 1, 'agent_error': 2}
        failure = min(failures, key=lambda item: priority[item.code])
        if failure.code != 'agent_error':
            self.provider_error = failure
        if failure.code == 'auth_required':
            self.stop.set()
        if self.row['execution']['status'] not in ('timeout', 'cancelled'):
            self.row['execution']['status'] = 'error'
        self.command.message(f'{self.label}: {failure.message}', level='WARNING')
        try:
            _write_closed(self.artifacts / 'agent-error.json', {'code': failure.code, 'message': failure.message})
        except OPERATION_ERRORS as error:
            self.command.diagnostic(error)
            self.archive_complete = False
            self.evidence['native_error'] = {'status': 'incomplete', 'code': failure.code}

    def capture(self) -> None:
        """Save final local changes and source roles even after native cancellation."""
        self.stage = 'capture'
        self.command.phase('capture', self.row['id'])
        self.cache.stop = None
        changes = capture_changes(self.workspace, self.config.data['agent']['diff_excludes'])
        self.check_source_secrets(changes.diff, changes.states)
        atomic_write(self.artifacts / 'agent.diff', changes.diff)
        _write_closed(self.artifacts / 'capture.json', {'excluded': changes.excluded, 'paths': sorted(changes.states)})
        self.row['changes'] = {
            'status': 'nonempty' if changes.diff else 'empty',
            'files_count': len(changes.states),
            'diff': (self.relative / 'agent.diff').as_posix(),
        }
        save_triplets(
            self.artifacts / 'triplets',
            self.cache,
            self.row['before'],
            self.row['after'],
            changes,
            self.row['id'],
            self.attempt['id'],
            run_id=self.report_identity['run_id'],
        )
        self.row['artifacts']['triplets_manifest'] = (self.relative / 'triplets/index.json').as_posix()
        self.evidence['source'] = {'status': 'saved', 'reason': None}

    def check_source_secrets(self, diff: bytes, states: dict) -> None:
        """Withhold exact source evidence that contains a known credential."""
        secrets = [value.encode('utf-8') for value in self.secrets() if value]
        if not secrets:
            return
        before, after = self.cache.tree(self.row['before']), self.cache.tree(self.row['after'])
        paths = sorted(
            states.keys() | {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        )
        contents = [diff] + [state.text.encode('utf-8') for state in states.values()]
        for revision in (self.row['before'], self.row['after']):
            contents.extend(
                state.text.encode('utf-8') for state in revision_states(self.cache, revision, paths).values()
            )
        if any(secret in content for secret in secrets for content in contents):
            raise BenchmarkError('capture_secret', 'Source capture contains a known credential and was withheld.')

    def failed(self, error: BaseException) -> None:
        """Preserve native failure facts while keeping policy decisions with the coordinator."""
        self.command.diagnostic(error)
        code = getattr(error, 'code', 'task_failed')
        cancelled = code == 'operation_cancelled' or (
            self.stop.is_set() and code not in ('auth_required', 'credentials_missing')
        )
        if cancelled:
            self.row['processing_status'] = 'interrupted'
            if self.opencode_started and self.row['execution']['status'] in ('running', 'not_started'):
                self.row['execution']['status'] = 'cancelled'
        else:
            failure = {
                'stage': self.stage,
                'code': code,
                'message': getattr(error, 'message', 'Native task infrastructure failed; see the DEBUG log.'),
                'paths': getattr(error, 'paths', []),
            }
            if self.row['infrastructure_error'] is None:
                self.row['infrastructure_error'] = failure
            if self.opencode_started and self.row['execution']['status'] in ('running', 'not_started'):
                self.row['execution']['status'] = 'error'
            self.command.message(f'{self.label}: {failure['message']}', level='WARNING')
            if code in ('auth_required', 'credentials_missing'):
                self.fatal = {'code': code, 'message': failure['message']}
                self.stop.set()
        if self.stage == 'capture':
            self.evidence['source'] = {'status': 'incomplete', 'reason': code}
            self.archive_complete &= not self.opencode_started
        if code in ('unsupported_change', 'capture_secret'):
            self.save_capture_error(code, error)

    def save_capture_error(self, code: str, error: BaseException) -> None:
        """Retain supported partial diff while letting later telemetry always run."""
        try:
            captured = getattr(error, 'captured_diff', None) if code == 'unsupported_change' else None
            if captured is not None:
                if any(value.encode() in captured for value in self.secrets() if value):
                    captured = None
            if captured is not None:
                atomic_write(self.artifacts / 'agent.diff', captured)
                self.row['changes'] = {
                    'status': 'nonempty' if captured else 'empty',
                    'files_count': getattr(error, 'files_count', None),
                    'diff': (self.relative / 'agent.diff').as_posix(),
                }
            _write_closed(self.artifacts / 'capture-error.json', {'code': code, 'paths': getattr(error, 'paths', [])})
        except OPERATION_ERRORS as failure:
            self.command.diagnostic(failure)
            self.archive_complete = False
            self.evidence['source'] = {'status': 'incomplete', 'reason': 'capture_receipt_failed'}

    def telemetry(self) -> None:
        """Capture native usage once while the private HOME is still available."""
        self.command.phase('telemetry', self.row['id'])
        captured = capture_telemetry(
            self.private / 'home/data/opencode/opencode.db',
            self.artifacts / 'telemetry',
            run_id=self.report_identity['run_id'],
            task_id=self.row['id'],
            attempt_id=self.attempt['id'],
            runtime=self.config.data['agent']['runtime'],
            root_id=None if self.tracker is None else self.tracker.root_session_id,
            opencode_started=self.opencode_started,
            path_prefix=(self.relative / 'telemetry').as_posix(),
            secrets=self.secrets(),
        )
        self.row['telemetry'] = captured.data
        self.archive_complete &= captured.archive_complete
        self.evidence['usage'] = {'status': captured.data['status'], 'archive_complete': captured.archive_complete}
        for warning in captured.data['diagnostics']:
            if warning['code'] != 'not_started':
                self.command.message(f'{self.label}: {warning['message']}', level='WARNING')

    def secrets(self) -> list[str]:
        """Keep credentials in memory and out of all sealed documents."""
        if self.invocation is None or self.invocation._environment is None:
            return []
        return self.invocation.secrets()

    def resource_paths(self) -> set[str]:
        """Enumerate only declared resource inputs from frozen receipts and relocated config."""
        path = self.artifacts / 'runtime/config.json'
        if not path.is_file():
            return set()
        native = read_json(path)
        skills = native.get('skills')
        skills = skills if isinstance(skills, dict) else {}
        groups = [native.get('instructions', []), native.get('plugin', []), skills.get('paths', [])]
        result = set()
        receipts = self.config.identity()['resources']
        fields = {'instructions': 0, 'plugin': 1, 'paths': 2}
        for resource in receipts:
            group = fields[resource['field']]
            prefix = f'/benchmark-runtime/resources/{group}-{resource['index']}-'
            roots = [
                urlsplit(value).path
                for value in groups[group]
                if isinstance(value, str) and urlsplit(value).path.startswith(prefix)
            ]
            for entry in resource['files']:
                matched = False
                for root in roots:
                    relative = 'runtime/' + root.removeprefix('/benchmark-runtime/').rstrip('/')
                    if entry['path']:
                        relative += '/' + entry['path']
                    target = artifact_path(self.artifacts, relative)
                    if target.is_file() and bytes_sha256(target.read_bytes()) == entry['sha256']:
                        result.add(relative)
                        matched = True
                if not matched:
                    self.archive_complete = False
                    self.evidence['declared_resources'] = {'status': 'incomplete', 'reason': 'resource_receipt_missing'}
        return result

    def inventory(self) -> list[dict]:
        """Bind an explicit producer inventory without recursively signing arbitrary files."""
        names = {
            'start.json',
            'prompt.txt',
            'launch.json',
            'runtime/config.json',
            'setup.sh',
            'setup.log',
            'effective_config.json',
            'effective-config.receipt.json',
            'effective-config.log',
            'agent.diff',
            'capture.json',
            'capture-error.json',
            'agent-error.json',
            'events.jsonl',
            'runtime.log',
            'stream_receipt.json',
            'telemetry/receipt.json',
            'telemetry/usage.json',
            'telemetry/opencode.sqlite.xz',
        }
        shared = [self.agent_input['config'], *self.agent_input['resources']]
        effective = self.row['artifacts']['effective_config']
        if effective['status'] == 'saved':
            shared.append(reference(self.run_dir, effective['path']))
        manifest = self.row['artifacts']['triplets_manifest']
        if manifest is not None:
            names.add('triplets/index.json')
            try:
                bundle = read_triplets(
                    self.artifacts / 'triplets', owner=self.row, run_id=self.report_identity['run_id']
                )
                for entry in bundle.index['entries']:
                    names.update('triplets/' + item['path'] for item in entry['files'].values() if item is not None)
            except OPERATION_ERRORS as error:
                self.command.diagnostic(error)
                self.archive_complete = False
                self.evidence['source'] = {'status': 'incomplete', 'reason': 'triplet_receipt_failed'}
                self.row['artifacts']['triplets_manifest'] = None

        result = copy.deepcopy(shared)
        for name in sorted(names):
            try:
                path = artifact_path(self.artifacts, name)
                if path.is_file():
                    result.append(reference(self.run_dir, (self.relative / name).as_posix()))
            except OPERATION_ERRORS as error:
                self.command.diagnostic(error)
                self.archive_complete = False
                inventory = self.evidence.setdefault('inventory', {'status': 'incomplete', 'invalid': []})
                inventory['invalid'].append(name)
        return result

    def required_files(self) -> set[str]:
        """Distinguish legitimate not-started inputs from lost producer evidence."""
        names = {'start.json', 'events.jsonl', 'runtime.log', 'stream_receipt.json', 'telemetry/receipt.json'}
        if self.opencode_started:
            names.update({'prompt.txt', 'launch.json'})
        if self.row['artifacts']['triplets_manifest'] is not None:
            names.update({'agent.diff', 'capture.json', 'triplets/index.json'})
        if self.row['setup']['status'] not in ('skipped', 'not_started'):
            names.update({'setup.log'})
        if self.row['artifacts']['effective_config']['status'] == 'saved':
            names.update({'effective-config.receipt.json'})
        receipt = self.artifacts / 'telemetry/receipt.json'
        if receipt.is_file() and read_json(receipt)['status'] == 'saved':
            names.update({'telemetry/usage.json', 'telemetry/opencode.sqlite.xz'})
        return names

    def close_native(self) -> None:
        """Seal native timing, source, streams and usage before any host scoring."""
        if not self.opencode_started:
            self.native_timing = dict.fromkeys(('started_at', 'finished_at', 'duration_seconds'))
        if self.row['processing_status'] != 'interrupted':
            self.row['processing_status'] = 'completed'
        receipt = None if self.tracker is None else self.tracker.stream_receipt
        if receipt is None and (self.artifacts / 'stream_receipt.json').is_file():
            receipt = read_json(self.artifacts / 'stream_receipt.json')
        if receipt is None and not self.opencode_started:
            receipt = {
                'schema_version': 1,
                'status': 'not_started',
                'events_complete': True,
                'runtime_complete': True,
                'transcript_complete': False,
                'events_count': 0,
                'runtime_lines': 0,
                'warnings': [],
            }
            for name in ('events.jsonl', 'runtime.log'):
                if not (self.artifacts / name).exists():
                    atomic_write(self.artifacts / name, b'')
            _write_closed(self.artifacts / 'stream_receipt.json', receipt)
        raw_complete = receipt is not None and receipt['events_complete'] and receipt['runtime_complete']
        self.archive_complete &= raw_complete
        self.evidence['streams'] = {'status': 'saved' if raw_complete else 'incomplete', 'receipt': receipt}
        self.evidence.setdefault('source', {'status': 'not_started', 'reason': 'workspace_unavailable'})
        self.evidence['configured'] = {
            'status': 'saved' if self.invocation is not None else 'not_started'
        }
        self.evidence['effective'] = copy.deepcopy(self.row['artifacts']['effective_config'])
        receipts = self.inventory()
        bound = {item['path'] for item in receipts}
        missing = sorted(name for name in self.required_files() if (self.relative / name).as_posix() not in bound)
        if missing:
            self.archive_complete = False
        self.evidence['required'] = {'status': 'incomplete' if missing else 'saved', 'missing': missing}
        if 'output_pipe_not_closed' in self.evidence.get('setup', {}).get('warnings', []):
            self.archive_complete = False
        self.seal = {
            'schema_family': 'pr_similarity_os.seal',
            'schema_version': 1,
            **self.report_identity,
            'agent_input': copy.deepcopy(self.agent_input),
            'task': copy.deepcopy(self.row),
            'native_timing': copy.deepcopy(self.native_timing),
            'archive_complete': self.archive_complete,
            'evidence': copy.deepcopy(self.evidence),
            'artifact_receipts': receipts,
        }
        _write_closed(self.artifacts / 'seal.json', self.seal)
        self.seal_reference = reference(self.run_dir, (self.relative / 'seal.json').as_posix())

    def score(self) -> None:
        """Write a complete host result while keeping native failure and retry axes fixed."""
        self.stage = 'score'
        self.command.phase('scoring', self.row['id'])
        assert self.seal_reference is not None
        diagnostics = []
        metric = self.report_identity['metric']
        can_score = self.row['artifacts']['triplets_manifest'] is not None and self.row['execution']['status'] in (
            'completed',
            'timeout',
            'error',
        )
        try:
            if can_score:
                result = score_saved(
                    self.artifacts / 'triplets',
                    (self.relative / 'triplets').as_posix(),
                    self.logs / 'scoring.log',
                    self.stop,
                    grace=self.config.data['shutdown']['grace_seconds'],
                    secrets=self.secrets(),
                )
                if result['metric'] != self.report_identity['metric']:
                    raise BenchmarkError('metric_changed', 'Host scoring differs from the frozen run metric.')
                self.row['scoring'], metric, diagnostics = result['scoring'], result['metric'], result['diagnostics']
            else:
                self.scoring_error = {
                    'code': 'scoring_not_available',
                    'message': 'No complete native source capture is available.',
                }
                self.row['scoring'] = unavailable_scoring('unavailable', self.scoring_error)
        except OPERATION_ERRORS as error:
            self.command.diagnostic(error)
            self.scoring_error = {
                'code': getattr(error, 'code', 'scoring_failed'),
                'message': getattr(error, 'message', 'Host scoring failed; use saved evidence for recovery.'),
            }
            self.row['scoring'] = unavailable_scoring('unavailable', self.scoring_error)
            if self.scoring_error['code'] == 'operation_cancelled':
                self.row['processing_status'] = 'interrupted'
            self.command.message(f'{self.label}: {self.scoring_error['message']}', level='WARNING')
        value = {
            'schema_family': 'pr_similarity_os.scoring',
            'schema_version': 1,
            'run_id': self.report_identity['run_id'],
            'task_id': self.row['id'],
            'attempt_id': self.attempt['id'],
            'seal': self.seal_reference,
            'scoring': self.row['scoring'],
            'metric': metric,
            'diagnostics': diagnostics,
            'error': self.scoring_error,
        }
        _write_closed(self.artifacts / 'scoring.json', value)
        self.scoring_reference = reference(self.run_dir, (self.relative / 'scoring.json').as_posix())
        for warning in diagnostics:
            self.command.message(f'{self.label}: {warning['message']}', level='WARNING')

    def finish(self) -> dict:
        """Publish immutable outcome references; the coordinator decides scheduling next."""
        assert self.seal is not None and self.seal_reference is not None and self.scoring_reference is not None
        receipts = copy.deepcopy(self.seal['artifact_receipts']) + [self.seal_reference, self.scoring_reference]
        log = self.artifacts / 'scoring.log'
        if log.is_file():
            receipts.append(reference(self.run_dir, (self.relative / 'scoring.log').as_posix()))
        outcome = {
            'schema_family': 'pr_similarity_os.outcome',
            'schema_version': 1,
            **self.report_identity,
            'agent_input': copy.deepcopy(self.agent_input),
            'task': copy.deepcopy(self.row),
            'seal': self.seal_reference,
            'scoring_result': self.scoring_reference,
            'scoring_error': self.scoring_error,
            'fatal': self.fatal,
            'archive_complete': self.archive_complete,
            'artifact_receipts': receipts,
        }
        _write_closed(self.artifacts / 'outcome.json', outcome)
        return copy.deepcopy(outcome)


def run_attempt(
    config: RunConfig,
    basket_row: dict,
    run_dir: Path,
    report_identity: dict,
    runtime: Runtime,
    bwrap: Path,
    command: Command,
    attempt: dict,
    stop: Stop,
    *,
    registration: TaskRegistration,
) -> dict:
    """Execute one coordinator-reserved native attempt without scheduler writes.

    Args:
        config: Frozen runtime, sandbox and scoring settings.
        basket_row: Exact frozen task inputs owned by registration.
        run_dir: Owned run directory with an already-persisted numbered start.
        report_identity: Run, basket, config, runtime and metric ownership.
        runtime: Verified pinned OpenCode executable.
        bwrap: Verified source-only sandbox executable.
        command: Disposable phase observer and redacted logger.
        attempt: Coordinator-reserved sequence and native attempt identity.
        stop: Shared cancellation intent, including provider auth rejection.
        registration: Immutable ordinal and readable task directory ownership.

    Returns:
        Closed native outcome with exact seal and host scoring references.

    Raises:
        BenchmarkError: If reservation differs or immutable evidence cannot be saved.

    """
    state = _Attempt(
        config, basket_row, run_dir, report_identity, runtime, bwrap, command, attempt, stop, registration=registration
    )
    return state.execute()
