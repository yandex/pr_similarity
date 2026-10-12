"""RunStore scheduling owns reservations, retry decisions and bounded phase observations."""

import fcntl
import os
import shlex
import signal
import stat
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from tempfile import TemporaryDirectory
from types import FrameType

from benchmark.artifact_refs import read_reference, reference
from benchmark.attempt import run_attempt
from benchmark.auth import auth_snapshot
from benchmark.basket import Basket, load_basket
from benchmark.bubblewrap import resolve_bubblewrap
from benchmark.commands import Command
from benchmark.config import RunConfig
from benchmark.errors import OPERATION_ERRORS, BenchmarkError
from benchmark.inference import build_inference
from benchmark.layout import artifact_path, run_name
from benchmark.lifecycle import Stop, native_policy, needs_host_score, retry_reasons
from benchmark.pricing import PricingPolicy, load_policy
from benchmark.progress import MODES
from benchmark.provisioning import Runtime, provision_opencode
from benchmark.recovery import recover_host_scores, recover_native_facts
from benchmark.repricing import apply_pricing, freeze_pricing
from benchmark.result import pending_task
from benchmark.run_store import RunStore
from benchmark.sandbox import Sandbox
from benchmark.scoring import diagnostic_metrics, metric_receipt
from benchmark.storage import run_lock, sha256, utc_now, write_json


def initial_report(config: RunConfig, basket: Basket, run_id: str, invocation_id: str) -> dict:
    """Create a report containing every frozen task before runtime preflight."""
    now = utc_now()
    return {
        'schema_family': 'pr_similarity_os.report',
        'schema_version': 1,
        'run': {
            'id': run_id,
            'revision': 1,
            'status': 'running',
            'created_at': now,
            'updated_at': now,
            'finished_at': None,
            'current_invocation_id': invocation_id,
            'tags': list(config.data['tags']),
            'error': None,
        },
        'basket': {
            'sha256': basket.sha256,
            'path': 'basket.json',
            'partial': basket.data['meta']['partial'],
            'total_tasks': len(basket.rows),
            'omitted_failed_count': len(basket.data['meta']['omitted_failed_ids']),
        },
        'agent': {
            'name': config.data['agent']['name'],
            'runtime': config.data['agent']['runtime'],
            'auth_mode': config.data['agent']['auth']['mode'],
            'resolved_runtime': None,
        },
        'config': {
            'identity_version': 1,
            'identity_sha256': sha256(config.identity()),
            'identity_path': 'config_identity.json',
        },
        'metric': metric_receipt(check_environment=False),
        'inference': build_inference(config.identity()),
        'summary': {},
        'tasks': [pending_task(row) for row in basket.rows],
        'last_rescore': None,
        'last_reprice': None,
    }


def _selections(store: RunStore) -> list[dict]:
    """List explicitly registered native owners, including immutable attempt history."""
    return [
        {**item, 'task_id': selected['id']}
        for selected in store.state['tasks']
        for item in [*selected['history'], selected]
        if item['attempt'] is not None
    ]


def _invocation_resumes(store: RunStore) -> int:
    """Count readable resume notices without granting disposable metadata execution authority."""
    count = 0
    for path in (store.directory / '_meta/invocations').glob('*/invocation.json'):
        try:
            metadata = read_reference(
                store.directory, reference(store.directory, path.relative_to(store.directory).as_posix())
            )
            count += metadata.get('kind') == 'resume'
        except OPERATION_ERRORS:
            continue
    return count


def retry_statistics(run_dir: Path) -> dict:
    """Summarize only state-owned starts and native retry causes without directory discovery."""
    store = RunStore.open(run_dir)
    owners = _selections(store)
    repeated = [item for item in owners if item['attempt']['retry_index'] > 0]
    retried = {item['task_id'] for item in repeated}
    identity = store.registry['baseline']['config']['identity_path']
    from_identity = read_reference(store.directory, reference(store.directory, identity))
    categories = from_identity['retries']['categories']
    causes = []
    for item in owners:
        if item['seal'] is not None:
            native = read_reference(store.directory, item['seal'])
            causes.extend(retry_reasons(native['task'], categories))
    final = {row['id']: row for row in store.snapshot()['tasks']}
    return {
        'attempts': len(owners),
        'retries_started': len(repeated),
        'tasks_retried': len(retried),
        'tasks_completed_after_retry': sum(
            row['processing_status'] == 'completed' and not retry_reasons(row, categories)
            for identifier, row in final.items()
            if identifier in retried
        ),
        'trigger_counts': {reason: causes.count(reason) for reason in sorted(set(causes))},
        'tasks_exhausted': sum(
            row['processing_status'] == 'completed' and bool(retry_reasons(row, categories)) for row in final.values()
        ),
        'resumes': _invocation_resumes(store),
    }


def _validate_inputs(store: RunStore, config: RunConfig, basket: Basket) -> None:
    """Reject changed frozen inputs before invocation, recovery or presentation writes."""
    baseline = store.registry['baseline']
    if baseline['basket']['sha256'] != basket.sha256 or baseline['config']['identity_sha256'] != sha256(
        config.identity()
    ):
        raise BenchmarkError(
            'resume_identity_mismatch', 'Basket or meaningful agent configuration changed; use a new run.'
        )
    if [row['basket_row'] for row in store.registry['tasks']] != basket.rows:
        raise BenchmarkError('resume_identity_mismatch', 'Saved task registry differs from the complete frozen basket.')


def final_summary(report: dict) -> str:
    """Present processing, native execution, scoring and infrastructure as separate readable axes."""
    rows, summary = report['tasks'], report['summary']
    processed = sum(
        row['final_attempt'] is not None and row['final_attempt']['finished_at'] is not None for row in rows
    )
    native_completed = sum(row['execution']['status'] == 'completed' for row in rows)
    return (
        f'Final: processed {processed}/{len(rows)}; tasks complete {summary['completed_tasks']}/{len(rows)}; '
        f'native completed {native_completed}; timeout {summary['timeout_tasks']}; '
        f'error {summary['error_tasks']}; cancelled {summary['cancelled_tasks']}; '
        f'scored {summary['scored_tasks']}/{len(rows)}; '
        f'infrastructure errors {summary['infrastructure_error_tasks']}.'
    )


def spend_summary(costs: dict) -> str:
    """Show known subtotals and coverage without treating unknown usage or prices as free."""
    messages = []
    for field, label in [
        ('native_reported', 'All-attempt native reported'),
        ('api_equivalent', 'API-equivalent estimate'),
    ]:
        value = costs[field]
        coverage = value['coverage']
        total = coverage['total_requests'] if coverage['total_requests'] is not None else 'unknown'
        messages.append(
            f'{label}: known subtotal {value['currency']} {value['known_subtotal']}; '
            f'coverage {coverage['known_requests']}/{total} requests; status {value['status']}; '
            f'missing tasks {coverage['missing_tasks']}.'
        )
    return ' '.join(messages)


class Coordinator:
    """Keep one writer lease while coordinating immutable native facts and host recovery."""

    def __init__(
        self,
        config: RunConfig,
        basket: Basket,
        store: RunStore,
        dataset: Path,
        *,
        resumed: bool,
        invocation_id: str,
        level: str,
        policy: PricingPolicy | None,
        attempt_function: Callable,
        progress: str,
    ) -> None:
        """Create an observer only after run creation or read-only resume validation."""
        self.config, self.basket, self.store = config, basket, store
        self.run_dir, self.dataset, self.resumed = store.directory, dataset.expanduser().resolve(), resumed
        self.policy, self.attempt_function = policy, attempt_function
        self.stop = Stop()
        self.fatal: dict | None = None
        self.issued: set[str] = set()
        self._phase_lock = threading.Lock()
        self._phase_updates: dict[str, tuple[str, str]] = {}
        self._next_observation = 0.0
        self.recovered = recover_native_facts(store, config) if resumed else []
        self.retries_started = retry_statistics(self.run_dir)['retries_started']
        previous = store.state['run']['current_invocation_id'] if resumed else None
        self.report = store.prepare_invocation(invocation_id, resumed=resumed)
        self.command = Command(
            self.run_dir,
            'resume' if resumed else 'run',
            config=config.data,
            level=level,
            previous=previous,
            secrets=[os.environ[name] for name in config.data['agent']['credentials'].values() if name in os.environ],
            progress_mode=progress,
            progress_total=len(basket.rows),
            resumed=resumed,
            invocation_id=invocation_id,
            phase_observer=self.queue_phase,
        )
        self.command.watch_interrupts(self.stop.observed_interrupts)
        self.originals: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                self.originals[number] = signal.getsignal(number)
                signal.signal(number, self.stop.signal)
        self.runtime: Runtime | None = None
        self.bwrap: Path | None = None
        self.pricing_request: dict | None = None
        self.counts()

    def queue_phase(self, task_id: str, phase: str) -> None:
        """Coalesce worker observations in memory without granting them state ownership."""
        if task_id not in {row['id'] for row in self.store.registry['tasks']}:
            return
        with self._phase_lock:
            self._phase_updates[task_id] = (phase, utc_now())

    def flush_observations(self, *, force: bool = False) -> None:
        """Persist the latest coarse observation at a bounded coordinator cadence."""
        if not force and time.monotonic() < self._next_observation:
            return
        self._next_observation = time.monotonic() + 0.25
        with self._phase_lock:
            updates, self._phase_updates = self._phase_updates, {}
        for identifier, (phase, stamp) in updates.items():
            selected = next(row for row in self.store.state['tasks'] if row['id'] == identifier)
            if selected['processing_status'] == 'running':
                self.report = self.store.observe(identifier, phase, stamp)

    def counts(self) -> None:
        """Publish aggregate presentation counters from the authoritative snapshot."""
        self.report = self.store.snapshot()
        progress = self.command.progress
        if progress is not None:
            if self.report['run']['status'] == 'stopping':
                progress.draining()
            progress.counts(
                completed=self.report['summary']['completed_tasks'],
                active=self.report['summary']['active_tasks'],
                retries=self.retries_started,
                errors=sum(
                    row['infrastructure_error'] is not None or row['scoring']['status'] == 'error'
                    for row in self.report['tasks']
                ),
            )

    def persist(self) -> None:
        """Commit scheduling through RunStore and refresh disposable presentation counters."""
        self.report = self.store.commit(self.report)
        self.counts()

    def notices(self) -> None:
        """Explain frozen metadata and recovered native owners before any new paid work."""
        baseline = self.store.registry['baseline']
        if self.resumed and (
            self.config.data['agent']['name'] != baseline['agent']['name']
            or self.config.data['tags'] != baseline['run']['tags']
        ):
            self.command.message(
                'Resume preserves the original run name and Setup tags; current YAML label edits are not applied.'
            )
        for notice in self.recovered:
            ordinal = self.store.task(notice['task_id'])['ordinal']
            self.command.message(f'Task {ordinal:03d}: recovered {notice['kind']} for saved native evidence.')
            if notice.get('fatal') is not None:
                self.command.message(
                    'A previous native attempt failed authentication; this explicit resume checks current access.',
                    level='WARNING',
                )
        configured = baseline['inference']['configured']
        self.command.message(
            f'Run {baseline['agent']['name']}: {'resume' if self.resumed else 'new'}; '
            f'basket={self.basket.data['meta'].get('name', 'unnamed')} ({self.basket.sha256[:12]}); '
            f'model={configured['model']['value'] or configured['model']['status']}; '
            f'effort={configured['effort']['value'] or configured['effort']['status']}; '
            f'Setup={baseline['run']['tags']}; tasks={len(self.basket.rows)} workers={self.config.data['workers']}.'
        )
        self.command.message(f'Report: {self.run_dir / 'report.json'}; logs: {self.command.logs_dir}.')
        if self.basket.data['meta']['partial']:
            self.command.message(
                f'Partial basket: all {len(self.basket.rows)} exported tasks are included; '
                f'{len(self.basket.data['meta']['omitted_failed_ids'])} collector failures stay outside this run.',
                level='WARNING',
            )

    def resume_plan(self) -> None:
        """Explain reused, host-only and new native work plus known previous spend before issuance."""
        report = self.store.snapshot()
        owned = list(zip(self.store.state['tasks'], report['tasks'], strict=True))
        host = {row['id'] for selected, row in owned if self.resumed and needs_host_score(selected, row)}
        reused = sum(row['processing_status'] == 'completed' and row['id'] not in host for _selected, row in owned)
        native = [
            (selected, row)
            for selected, row in owned
            if row['processing_status'] != 'completed' and row['id'] not in host
        ]
        self.command.message(
            f'{'Resume' if self.resumed else 'Run'} plan: reused {reused}; '
            f'host-only recovery {len(host)}; new attempts {len(native)}; blocked 0.'
        )
        if host:
            self.command.message(
                f'{len(host)} host-only task(s): score captured native source using the same attempt; '
                'no agent provisioning.'
            )
        reasons: dict[str, int] = {}
        for selected, row in native:
            if selected['attempt'] is None:
                reason = 'not attempted yet'
            elif selected['seal'] is None:
                reason = 'native work unsealed; previous execution and usage remain unknown'
            elif row['infrastructure_error'] is not None:
                reason = 'previous native infrastructure failure: ' + row['infrastructure_error']['code']
            elif row['execution']['status'] == 'cancelled' or row['processing_status'] == 'interrupted':
                reason = 'interrupted native work'
            else:
                triggers = retry_reasons(row, self.config.data['agent']['retries']['categories'])
                reason = 'native retry policy: ' + ', '.join(triggers) if triggers else 'unfinished native work'
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in reasons.items():
            self.command.message(f'{count} new attempt(s): {reason}.')
        if self.resumed:
            self.command.message(spend_summary(report['costs']['all_attempts']))

    def prepare(self) -> None:
        """Finish stopped host recovery before validating or provisioning new native work."""
        self.notices()
        self.resume_plan()
        if self.resumed:
            self.report = recover_host_scores(self.store, self.config, self.command, self.stop)
        pending = [row for row in self.store.state['tasks'] if row['processing_status'] != 'completed']
        host = [row for row in pending if row['seal'] is not None and row['outcome'] is None]
        native = [row for row in pending if row not in host]
        if self.stop.is_set():
            return
        if host:
            self.command.message(
                f'Resume blocked: {len(host)} sealed host task(s) remain unresolved; native issuance stopped.'
            )
            raise BenchmarkError(
                'host_recovery_incomplete',
                'Sealed host work remains unresolved; native work is not issued.',
                exit_code=1,
            )
        self.report = self.store.snapshot()
        self.report['run'].update(status='running', finished_at=None, error=None)
        self.persist()
        if native:
            self.command.phase('metric-check')
            current = metric_receipt()
            if current != self.store.state['metric']:
                raise BenchmarkError(
                    'resume_metric_mismatch', 'Metric implementation changed; use rescore or a new run.', exit_code=1
                )
            self.prepare_agents()
        if self.policy is not None:
            self.pricing_request = freeze_pricing(self.run_dir, self.policy, self.command, store=self.store)

    def prepare_agents(self) -> None:
        """Verify credentials, pinned executable and private source-only sandbox for native issuance."""
        self.config.credential_environment()
        self.command.phase('provisioning/cache-wait')
        runtime = provision_opencode(
            self.config.data['agent']['runtime'],
            Path(self.config.data['provisioning']['cache_dir']),
            timeout=self.config.data['provisioning']['download_timeout_seconds'],
            cancel=self.stop,
        )
        saved = self.store.state['resolved_runtime']
        if saved is not None and saved['binary_sha256'] != runtime.receipt['binary_sha256']:
            raise BenchmarkError('resume_runtime_mismatch', 'Pinned executable contents changed; use a new run.')
        self.runtime = runtime
        write_json(self.run_dir / '_meta/runtime.json', runtime.receipt)
        self.report = self.store.snapshot()
        self.report['agent']['resolved_runtime'] = {
            'version': runtime.receipt['actual_version'],
            'asset_name': runtime.receipt['asset_name'],
            'platform': runtime.receipt['platform'],
            'archive_sha256': runtime.receipt['archive_sha256'],
            'binary_sha256': runtime.receipt['binary_sha256'],
            'receipt': '_meta/runtime.json',
        }
        self.persist()
        self.prepare_sandbox()

    def prepare_sandbox(self) -> None:
        """Retain bubblewrap isolation identity and cover setup plus native OAuth lifetime."""
        assert self.runtime is not None
        self.command.phase('sandbox-check')
        self.bwrap = resolve_bubblewrap(
            self.config.data['sandbox']['executable'],
            Path(self.config.data['provisioning']['cache_dir']),
            auto_install=self.config.data['sandbox']['auto_install'],
            timeout=self.config.data['provisioning']['download_timeout_seconds'],
            cancel=self.stop,
        )
        with TemporaryDirectory(prefix='benchmark-preflight-') as temporary:
            root = Path(temporary)
            (root / 'source').mkdir()
            sandbox = Sandbox(
                str(self.bwrap), root / 'source', root / 'source', root / 'home', root / 'runtime', self.runtime.binary
            )
            resolved = sandbox.preflight(cancel=self.stop)
            saved_path = artifact_path(self.run_dir, '_meta/sandbox.json')
            if self.resumed and saved_path.is_file():
                saved = read_reference(self.run_dir, reference(self.run_dir, '_meta/sandbox.json'))
                if any(saved[key] != resolved[key] for key in ('backend', 'binary_sha256', 'network')):
                    raise BenchmarkError(
                        'resume_sandbox_mismatch', 'Sandbox executable or isolation changed; use a new run.'
                    )
            write_json(saved_path, resolved)
        auth = self.config.data['agent']['auth']
        if auth['mode'] == 'chatgpt_oauth':
            self.command.phase('auth')
            agent = self.config.data['agent']
            setup = agent['setup']['timeout_seconds'] if agent['setup'] else 0
            auth_snapshot(
                Path(auth['profile']),
                setup + agent['timeout_seconds'] + self.config.data['shutdown']['grace_seconds'],
                refresh_timeout=auth['refresh_timeout_seconds'],
                expiry_margin=auth['expiry_margin_seconds'],
                cancel=self.stop,
            )

    def submit(self, pool: ThreadPoolExecutor, pending: list[str], active: dict[Future, str]) -> None:
        """Reserve state and start evidence before submitting each native executor."""
        assert self.runtime is not None and self.bwrap is not None
        while pending and len(active) < self.config.data['workers'] and not self.stop.is_set():
            identifier = pending.pop(0)
            selected = next(row for row in self.store.state['tasks'] if row['id'] == identifier)
            metadata = {
                'id': str(uuid.uuid4()),
                'invocation_id': self.command.id,
                'sequence': selected['next_sequence'],
                'retry_index': selected['retry_index'] + int(identifier in self.issued),
                'started_at': utc_now(),
                'finished_at': None,
            }
            self.report = self.store.reserve(identifier, metadata)
            self.issued.add(identifier)
            self.retries_started += int(metadata['retry_index'] > 0)
            registration = self.store.task(identifier)
            self.command.task_start(identifier, ordinal=registration['ordinal'], attempt_sequence=metadata['sequence'])
            identity = {
                'run_id': self.store.registry['id'],
                'basket_sha256': self.basket.sha256,
                'config_identity_sha256': self.report['config']['identity_sha256'],
                'runtime_binary_sha256': self.runtime.receipt['binary_sha256'],
                'metric': self.report['metric'],
            }
            future = pool.submit(
                self.attempt_function,
                self.config,
                registration['basket_row'],
                self.run_dir,
                identity,
                self.runtime,
                self.bwrap,
                self.command,
                metadata,
                self.stop,
                registration=registration,
            )
            active[future] = identifier
            self.counts()

    def decision(self, native: dict, outcome: dict, task_id: str) -> tuple[str, list[str]]:
        """Separate native policy from host score errors and stop intent."""
        selected = next(item for item in self.store.state['tasks'] if item['id'] == task_id)
        return native_policy(
            native['task'],
            self.config.data['agent']['retries'],
            selected['retry_index'],
            fatal=outcome['fatal'] is not None,
            scoring_error=outcome['scoring_error'],
        )

    def accept(self, identifier: str, returned: dict | None) -> str:
        """Select exact closed native facts before deciding whether to enqueue a fresh attempt."""
        selected = next(row for row in self.store.state['tasks'] if row['id'] == identifier)
        directory = self.store.attempt_directory(identifier, selected['attempt']['sequence'])
        if returned is None:
            if not self.stop.is_set():
                raise BenchmarkError('attempt_incomplete', 'Executor returned without a closed outcome.', exit_code=1)
            self.report = self.store.snapshot()
            next(row for row in self.report['tasks'] if row['id'] == identifier)['processing_status'] = 'interrupted'
            self.persist()
            return 'interrupted'
        outcome_ref = reference(self.run_dir, (directory / 'outcome.json').as_posix())
        outcome = read_reference(self.run_dir, outcome_ref)
        if returned != outcome:
            raise BenchmarkError(
                'attempt_return_mismatch', 'Executor return differs from its immutable outcome.', exit_code=1
            )
        seal = read_reference(self.run_dir, outcome['seal'])
        status, reasons = self.decision(seal, outcome, identifier)
        self.report = self.store.accept_fact(identifier, 'seal', outcome['seal'])
        self.report = self.store.accept_fact(identifier, 'outcome', outcome_ref, processing_status=status)
        if outcome['fatal'] is not None:
            self.fatal = outcome['fatal']
            self.stop.set()
        row = next(item for item in self.report['tasks'] if item['id'] == identifier)
        ordinal = self.store.task(identifier)['ordinal']
        if status != 'completed' and not self.stop.is_set():
            if status == 'retry_pending':
                self.command.message(f'Task {ordinal:03d}: native retry pending; reason={', '.join(reasons)}.')
            self.command.task_phase(
                identifier,
                'retry pending' if status == 'retry_pending' else 'recovering score',
                attempt_sequence=selected['attempt']['sequence'],
            )
            self.command.worker_done(identifier)
        else:
            self.command.task_finish(
                identifier,
                execution=row['execution']['status'],
                scoring='scored' if row['scoring']['status'] == 'available' else row['scoring']['status'],
                attempt_sequence=selected['attempt']['sequence'],
            )
        with self._phase_lock:
            self._phase_updates.pop(identifier, None)
        self.counts()
        return status

    def drain(self, active: dict[Future, str]) -> None:
        """Stop owned workers before pool exit and preserve any closed sibling facts."""
        self.stop.set()
        for future, identifier in list(active.items()):
            try:
                self.accept(identifier, future.result())
            except OPERATION_ERRORS as error:
                self.command.diagnostic(error)
        active.clear()

    def schedule(self) -> None:
        """Issue only unfinished native tasks and drain safely when stop intent arrives."""
        pending = [row['id'] for row in self.store.state['tasks'] if row['processing_status'] != 'completed']
        if not pending or self.stop.is_set():
            return
        self.command.phase('tasks')
        active: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.config.data['workers']) as pool:
            try:
                while pending or active:
                    if self.stop.is_set() and self.store.state['run']['status'] == 'running':
                        self.report = self.store.snapshot()
                        self.report['run']['status'] = 'stopping'
                        self.persist()
                    self.submit(pool, pending, active)
                    self.flush_observations()
                    if not active:
                        break
                    done, _waiting = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        identifier = active.pop(future)
                        status = self.accept(identifier, future.result())
                        if status == 'retry_pending' and not self.stop.is_set():
                            pending.append(identifier)
            except OPERATION_ERRORS:
                self.drain(active)
                raise
        self.flush_observations(force=True)

    def finish(self) -> tuple[int, Path]:
        """Close run scheduling while retaining native facts and offline error distinctions."""
        self.report = self.store.snapshot()
        if self.stop.signal_number is not None:
            status, run_status = 128 + self.stop.signal_number, 'interrupted'
        elif self.fatal is not None:
            status, run_status = 1, 'failed'
            self.report['run']['error'] = self.fatal
        elif self.stop.is_set():
            status, run_status = 1, 'failed'
            self.report['run']['error'] = {'code': 'run_cancelled', 'message': 'Run stopped after a fatal task error.'}
        elif any(row['processing_status'] != 'completed' for row in self.report['tasks']):
            status, run_status = 1, 'interrupted'
        else:
            errors = any(
                row['infrastructure_error'] is not None
                or row['scoring']['status'] in ('error', 'unavailable')
                or row['execution']['status'] == 'error'
                for row in self.report['tasks']
            )
            status, run_status = int(errors), 'completed_with_errors' if errors else 'completed'
        self.report['run'].update(status=run_status, finished_at=utc_now())
        self.persist()
        if self.pricing_request is not None and not self.stop.is_set():
            try:
                pricing_status = apply_pricing(self.run_dir, self.pricing_request, self.command, store=self.store)
                status = status or pricing_status
                if pricing_status:
                    self.command.message(
                        f'Agent run status: {run_status}. API-equivalent pricing is incomplete; '
                        'the command returns a nonzero exit code for pricing gaps. '
                        'Saved diffs and quality scores remain available.',
                        level='WARNING',
                    )
                self.report = self.store.snapshot()
            except OPERATION_ERRORS as error:
                self.command.diagnostic(error)
                self.command.message(
                    'Run evidence is saved; offline pricing failed. Use make reprice to retry.', level='WARNING'
                )
                status = status or 1
        metrics = diagnostic_metrics(self.report['tasks'])
        self.command.message(
            f'Scored {metrics['scored_tasks']}/{len(self.report['tasks'])}; LCpr_LCf={metrics['LCpr_LCf_mlp']}.'
        )
        self.command.message(final_summary(self.report))
        self.command.message(spend_summary(self.report['costs']['all_attempts']))
        if run_status == 'interrupted':
            config_path = str(self.config.source_path) if self.config.source_path else 'config.yaml'
            values = [f'CONFIG={config_path}', f'DATASET={self.dataset}', f'RESUME={self.run_dir}']
            self.command.message(
                'Resume with: make run ' + ' '.join(shlex.quote(value) for value in values), level='WARNING'
            )
        self.command.finish(run_status)
        return status, self.run_dir

    def failed(self, error: BaseException) -> tuple[int, Path]:
        """Save a readable failed invocation after stopping all owned native work."""
        self.command.diagnostic(error)
        if self.stop.signal_number is not None:
            return self.finish()
        self.report = self.store.snapshot()
        for row in self.report['tasks']:
            if row['processing_status'] == 'running':
                row['processing_status'] = 'interrupted'
        self.report['run'].update(
            status='failed',
            finished_at=utc_now(),
            error={
                'code': getattr(error, 'code', 'run_failed'),
                'message': getattr(error, 'message', 'Benchmark failed; see the DEBUG file log.'),
            },
        )
        self.persist()
        self.command.message(self.report['run']['error']['message'], level='ERROR')
        self.command.message(final_summary(self.report))
        self.command.message(spend_summary(self.report['costs']['all_attempts']))
        self.command.finish('failed')
        return error.exit_code if isinstance(error, BenchmarkError) else 1, self.run_dir

    def execute(self) -> tuple[int, Path]:
        """Restore signal handlers and observers on every coordinator exit."""
        try:
            self.prepare()
            self.schedule()
            return self.finish()
        except OPERATION_ERRORS as error:
            self.stop.set()
            return self.failed(error)
        finally:
            for number, previous in self.originals.items():
                signal.signal(number, previous)
            self.command.close()


def _inode(value: os.stat_result) -> tuple[int, int]:
    """Bind a cleanup target to the originally allocated filesystem object."""
    return value.st_dev, value.st_ino


def _cleanup_empty_allocation(directory: Path, allocation: os.stat_result, lock: os.stat_result | None) -> None:
    """Remove only a fresh empty directory or its original idle lock, preserving all other evidence."""
    directory_fd = lock_fd = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if _inode(os.fstat(directory_fd)) != _inode(allocation):
            return
        names = os.listdir(directory_fd)
        if not names:
            if _inode(directory.lstat()) == _inode(allocation):
                directory.rmdir()
            return
        if names != ['.lock'] or lock is None:
            return
        lock_fd = os.open('.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory_fd)
        current = os.fstat(lock_fd)
        if not stat.S_ISREG(current.st_mode) or _inode(current) != _inode(lock) or current.st_size != 0:
            return
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.listdir(directory_fd) != ['.lock']:
            return
        if _inode(os.stat('.lock', dir_fd=directory_fd, follow_symlinks=False)) != _inode(current):
            return
        if _inode(directory.lstat()) != _inode(allocation):
            return
        os.unlink('.lock', dir_fd=directory_fd)
        directory.rmdir()
    except OSError:
        pass
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def run(
    config: RunConfig,
    dataset: Path,
    *,
    output: Path = Path('./runs'),
    resume: Path | None = None,
    level: str = 'INFO',
    pricing: Path | None = None,
    progress: str = 'auto',
    attempt_function: Callable = run_attempt,
) -> tuple[int, Path]:
    """Run one frozen basket under a single writer-owned RunStore authority.

    Resume validates immutable inputs before any operation writes. Host recovery
    precedes runtime/auth setup; fresh native work is always durably reserved.
    """
    if progress not in MODES:
        raise BenchmarkError('invalid_progress', 'PROGRESS must be auto, plain or off.')
    policy = load_policy(pricing) if pricing is not None else None
    basket = load_basket(dataset)
    invocation_id = str(uuid.uuid4())
    initial = None
    if resume is not None:
        run_dir = resume.expanduser().resolve()
        if not run_dir.is_dir():
            raise BenchmarkError('resume_missing', 'RESUME must name an existing run directory.')
        _validate_inputs(RunStore.open(run_dir), config, basket)
    else:
        run_id = str(uuid.uuid4())
        initial = initial_report(config, basket, run_id, invocation_id)
        run_dir = output.expanduser().resolve() / run_name(
            config.data['agent']['name'], run_id, initial['run']['created_at']
        )
    auth = config.data['agent']['auth']
    if auth['mode'] == 'chatgpt_oauth' and Path(auth['profile']).resolve().is_relative_to(run_dir):
        raise BenchmarkError('auth_profile_location', 'Private OAuth profile must be outside the run directory.')
    allocation = lock_identity = None
    if initial is not None:
        run_dir.mkdir(parents=True, mode=0o700)
        allocation = run_dir.lstat()
    try:
        with run_lock(run_dir) as lease:
            if allocation is not None:
                lock_identity = (run_dir / '.lock').lstat()
            store = (
                RunStore.create(run_dir, initial, basket, config, lease=lease)
                if initial is not None
                else RunStore.open(run_dir, lease=lease)
            )
            _validate_inputs(store, config, basket)
            return Coordinator(
                config,
                basket,
                store,
                dataset,
                resumed=resume is not None,
                invocation_id=invocation_id,
                level=level,
                policy=policy,
                attempt_function=attempt_function,
                progress=progress,
            ).execute()
    except BaseException:
        if allocation is not None:
            _cleanup_empty_allocation(run_dir, allocation, lock_identity)
        raise
