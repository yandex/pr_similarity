"""One authoritative run state selects immutable attempts and offline results."""

import copy
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from benchmark.agent_input import freeze_agent_input, validate_agent_input
from benchmark.config import RunConfig
from benchmark.artifact_refs import ArtifactReference
from benchmark.artifact_refs import read_reference as read_reference
from benchmark.artifact_refs import reference as reference
from benchmark.basket import Basket, load_basket
from benchmark.contracts import validate_document
from benchmark.cost_summary import CostEvidenceError, build_cost_summary
from benchmark.errors import BenchmarkError
from benchmark.inference import build_inference, generated_groups
from benchmark.layout import (
    TaskRegistration,
    artifact_path,
    attempt_path,
    task_registry,
)
from benchmark.operations import operation_identifier, validate_operation
from benchmark.result import pending_task, summary, unavailable_scoring, validate_report
from benchmark.storage import (
    RunLease,
    atomic_write,
    bytes_sha256,
    document_bytes,
    parse_json,
    read_json,
    run_lock,
    sha256,
    utc_now,
    write_json,
)
from benchmark.usage import validate_usage


def _selection(registration: TaskRegistration) -> dict:
    """Initialize scheduling without a second copy of task execution results."""
    return {
        'id': registration['id'],
        'ordinal': registration['ordinal'],
        'processing_status': 'pending',
        'attempt': None,
        'next_sequence': 1,
        'retry_index': 0,
        'retry_epoch': 0,
        'outcome': None,
        'start': None,
        'seal': None,
        'scoring': None,
        'pricing': None,
        'progress': {'phase': 'pending', 'updated_at': None},
        'history': [],
    }


class RunStore:
    """Own the immutable registry, atomic selections and deterministic public snapshot."""

    def __init__(
        self,
        directory: Path,
        registry: dict,
        state: dict,
        *,
        lease: RunLease | None = None,
        state_digest: str | None = None,
        allow_omitted_publish_payloads: bool = False,
    ) -> None:
        """Bind validated records without writing during read-only construction."""
        validate_document('run', registry)
        validate_document('state', state)
        self.directory = directory
        self.registry = registry
        self.state = state
        self.lease = lease
        self.allow_omitted_publish_payloads = allow_omitted_publish_payloads
        self._disk_digest = state_digest or bytes_sha256(document_bytes(state))
        self._tasks = {row['id']: row for row in registry['tasks']}
        self._facts_cache: dict[tuple, dict] = {}

    @classmethod
    def create(
        cls, directory: Path, initial: dict, basket: Basket, config: RunConfig, *, lease: RunLease | None = None
    ) -> 'RunStore':
        """Serialize initialization and never overwrite an already allocated registry."""
        if lease is not None:
            if not lease.active or lease.directory != directory.resolve():
                raise BenchmarkError('writer_lease_expired', 'Run creation requires its active directory lease.')
            return cls._create(directory, initial, basket, config, lease)
        with run_lock(directory) as acquired:
            store = cls._create(directory, initial, basket, config, acquired)
            store.lease = None
            return store

    @classmethod
    def _create(cls, directory: Path, initial: dict, basket: Basket, config: RunConfig, lease: RunLease) -> 'RunStore':
        """Freeze creation inputs and reserve the only mutable state before work starts."""
        if any(path.name != '.lock' for path in directory.iterdir()):
            raise BenchmarkError(
                'run_exists', 'Run directory already contains artifacts; preserve it and use RESUME or a new run.'
            )
        identity = config.identity()
        agent_input = freeze_agent_input(directory, config)
        atomic_write(directory / 'basket.json', basket.content)
        write_json(directory / 'config_identity.json', identity)
        baseline = {key: copy.deepcopy(value) for key, value in initial.items() if key not in ('tasks', 'summary')}
        baseline['inference'] = build_inference(identity)
        registry = {
            'schema_family': 'pr_similarity_os.run',
            'schema_version': 1,
            'id': initial['run']['id'],
            'agent_input': agent_input,
            'baseline': baseline,
            'tasks': task_registry(basket.rows, basket.data['meta']['repository']),
        }
        write_json(directory / 'run.json', registry)
        state = {
            'schema_family': 'pr_similarity_os.state',
            'schema_version': 1,
            'run_id': registry['id'],
            'registry_sha256': bytes_sha256(document_bytes(registry)),
            'revision': 1,
            'updated_at': initial['run']['updated_at'],
            'run': {key: initial['run'][key] for key in ('status', 'finished_at', 'error', 'current_invocation_id')},
            'resolved_runtime': initial['agent']['resolved_runtime'],
            'metric': initial['metric'],
            'selected_operations': {'rescore': None, 'reprice': None},
            'pending_publish': None,
            'last_publish': None,
            'tasks': [_selection(entry) for entry in registry['tasks']],
        }
        write_json(directory / '_meta/state.json', state)
        store = cls(directory, registry, state, lease=lease)
        store.validate()
        store.rebuild()
        return store

    @classmethod
    def open(
        cls, directory: Path, *, lease: RunLease | None = None, allow_omitted_publish_payloads: bool = False
    ) -> 'RunStore':
        """Reject incompatible pre-release layouts before any mutation or execution."""
        if (directory / 'run.json').is_file() != (directory / '_meta/state.json').is_file():
            raise BenchmarkError(
                'incomplete_run_state',
                'Run registry/state is incomplete; preserve this directory and use a new run or restore its backup.',
            )
        if not (directory / 'run.json').is_file() or not (directory / '_meta/state.json').is_file():
            raise BenchmarkError(
                'unsupported_run_format',
                'Run lacks the current schema-1 registry/state layout; use the tool version that created it.',
            )
        registry = read_json(artifact_path(directory, 'run.json', must_exist=True))
        if 'agent_input' not in registry:
            raise BenchmarkError('unsupported_run_format', 'Run predates shared native inputs; use its creating tool version.')
        content = artifact_path(directory, '_meta/state.json', must_exist=True).read_bytes()
        state = parse_json(content)
        store = cls(
            directory,
            registry,
            state,
            lease=lease,
            state_digest=bytes_sha256(content),
            allow_omitted_publish_payloads=allow_omitted_publish_payloads,
        )
        store.validate()
        return store

    def task(self, task_id: str) -> TaskRegistration:
        """Resolve ownership by the frozen task registry rather than display guesses."""
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise BenchmarkError(
                'unknown_task_identity', 'Artifact belongs to a task outside the frozen run.'
            ) from error

    def attempt_directory(self, task_id: str, sequence: int) -> Path:
        """Return the registered numbered location for a native attempt."""
        return attempt_path(self.task(task_id), sequence)

    def validate(self) -> None:
        """Validate frozen input bytes, registry mapping and each state selection."""
        for value, family in ((self.registry, 'run'), (self.state, 'state')):
            if (
                value.get('schema_family') != 'pr_similarity_os.' + family
                or type(value.get('schema_version')) is not int
            ):
                raise BenchmarkError('unsupported_run_format', 'Run registry/state has an incompatible schema.')
            if value['schema_version'] != 1:
                raise BenchmarkError('unsupported_run_format', 'Run registry/state has an unsupported schema version.')
        if self.state['run_id'] != self.registry['id']:
            raise BenchmarkError('state_owner_mismatch', 'State and immutable registry belong to different runs.')
        if bytes_sha256((self.directory / 'run.json').read_bytes()) != self.state['registry_sha256']:
            raise BenchmarkError('registry_digest_mismatch', 'Immutable run registry is damaged.')
        basket = load_basket(artifact_path(self.directory, 'basket.json', must_exist=True))
        baseline = self.registry['baseline']
        if (
            baseline['run']['id'] != self.registry['id']
            or baseline['basket']['path'] != 'basket.json'
            or baseline['config']['identity_path'] != 'config_identity.json'
        ):
            raise BenchmarkError('run_input_mismatch', 'Run baseline has foreign ownership or input locations.')
        identity = read_json(artifact_path(self.directory, 'config_identity.json', must_exist=True))
        if basket.sha256 != baseline['basket']['sha256'] or sha256(identity) != baseline['config']['identity_sha256']:
            raise BenchmarkError(
                'run_input_mismatch', 'Frozen basket/config inputs are damaged or belong to another run.'
            )
        validate_agent_input(self.directory, self.registry['agent_input'], identity)
        if baseline['inference'] != build_inference(identity):
            raise BenchmarkError(
                'inference_owner_mismatch', 'Configured inference differs from its frozen native config.'
            )
        expected = task_registry(basket.rows, basket.data['meta']['repository'])
        if self.registry['tasks'] != expected:
            raise BenchmarkError('registry_task_mismatch', 'Registry differs from the complete frozen basket.')
        if type(self.state['revision']) is not int or self.state['revision'] < 1:
            raise BenchmarkError('invalid_state', 'State revision must be a positive integer.')
        self._validate_runtime_projection()
        if self.state['selected_operations']['rescore'] is None and self.state['metric'] != baseline['metric']:
            raise BenchmarkError('metric_owner_mismatch', 'State metric differs from the frozen initial metric.')
        for kind, selected in self.state['selected_operations'].items():
            if selected is not None:
                operation = validate_operation(self.directory, self.registry, selected, kind)
                if kind == 'rescore' and operation.result['metric'] != self.state['metric']:
                    raise BenchmarkError(
                        'metric_owner_mismatch', 'State metric differs from its selected score operation.'
                    )
                if selected['accepted_revision'] > self.state['revision']:
                    raise BenchmarkError('operation_commit_mismatch', 'Root operation selection is newer than state.')
        for key, closed in (('pending_publish', False), ('last_publish', True)):
            selected = self.state[key]
            if selected is not None:
                self._validate_publication(selected, closed=closed)
        self._validate_selections()

    def _validate_runtime_projection(self) -> None:
        """Bind resolved executable metadata to its actual root receipt and frozen runtime selector."""
        resolved = self.state['resolved_runtime']
        if resolved is None:
            return
        if resolved['receipt'] != '_meta/runtime.json':
            raise BenchmarkError('runtime_receipt_mismatch', 'Resolved runtime has a foreign receipt location.')
        receipt = read_json(artifact_path(self.directory, '_meta/runtime.json', must_exist=True))
        expected = {
            'version': receipt.get('actual_version'),
            **{key: receipt.get(key) for key in ('asset_name', 'platform', 'archive_sha256', 'binary_sha256')},
            'receipt': '_meta/runtime.json',
        }
        selector = self.registry['baseline']['agent']['runtime']
        if (
            resolved != expected
            or not selector.startswith('opencode:v')
            or resolved['version'] != selector.removeprefix('opencode:v')
        ):
            raise BenchmarkError(
                'runtime_receipt_mismatch', 'Resolved runtime differs from its root receipt or frozen version.'
            )

    def _validate_selections(self) -> None:
        """Reject duplicate owners, foreign sequences and contradictory selected facts."""
        selections = self.state['tasks']
        if [row['id'] for row in selections] != list(self._tasks):
            raise BenchmarkError('invalid_state', 'State task queue differs from the immutable registry.')
        seen: set[str] = set()
        for row in selections:
            registration = self.task(row['id'])
            if row['ordinal'] != registration['ordinal'] or row['processing_status'] not in {
                'pending',
                'running',
                'completed',
                'retry_pending',
                'interrupted',
            }:
                raise BenchmarkError('invalid_state', 'Task ordinal or processing state is invalid.')
            for key, minimum in (('next_sequence', 1), ('retry_index', 0), ('retry_epoch', 0)):
                if type(row[key]) is not int or row[key] < minimum:
                    raise BenchmarkError('invalid_state', 'Attempt scheduling sequence or retry budget is invalid.')
            if row['attempt'] is None and row['processing_status'] != 'pending':
                raise BenchmarkError('invalid_state', 'Only pending tasks can have no reserved native attempt.')
            if row['attempt'] is None and any(
                row[key] is not None for key in ('start', 'seal', 'outcome', 'scoring', 'pricing')
            ):
                raise BenchmarkError('attempt_owner_mismatch', 'Selected facts require a registered native attempt.')
            attempts = [{**item, 'id': row['id']} for item in row['history']]
            if row['attempt'] is not None:
                attempts.append(row)
            sequences = [item['attempt']['sequence'] for item in attempts]
            if len(sequences) != row['next_sequence'] - 1 or any(
                sequence != ordinal for ordinal, sequence in enumerate(sequences, 1)
            ):
                raise BenchmarkError('invalid_state', 'Attempt history must contain every reserved sequence in order.')
            if row['attempt'] is None and attempts:
                raise BenchmarkError('attempt_owner_mismatch', 'Attempt history requires a current native selection.')
            for item in attempts:
                self._validate_attempt(item, seen)

    def _validate_attempt(self, selection: dict, seen: set[str]) -> None:
        """Validate every selected or historical native owner and its immutable references."""
        attempt = selection['attempt']
        if attempt['id'] in seen:
            raise BenchmarkError(
                'attempt_owner_mismatch', 'Attempt UUID is reused across current or historical owners.'
            )
        seen.add(attempt['id'])
        self._validate_start(selection)
        if selection['outcome'] is not None:
            if selection['seal'] is None:
                raise BenchmarkError('attempt_seal_missing', 'Final outcome requires its explicitly selected seal.')
            outcome = read_reference(self.directory, selection['outcome'])
            if outcome.get('seal') != selection['seal']:
                raise BenchmarkError('attempt_seal_mismatch', 'Outcome references a different native seal.')
            native_seal = read_reference(self.directory, selection['seal'])
            self._initial_score(selection, outcome, native_seal)
            native = native_seal['task']
            for field in ('final_attempt', 'setup', 'execution', 'changes', 'telemetry', 'infrastructure_error'):
                if outcome['task'][field] != native[field]:
                    raise BenchmarkError('native_fact_mismatch', 'Outcome changes immutable native execution evidence.')
        for key in ('scoring', 'pricing'):
            if selection[key] is not None:
                self._selected_result(selection, key)
        for kind in ('outcome', 'seal'):
            if selection[kind] is not None:
                self._facts(selection, kind)

    def _initial_score(self, selection: dict, outcome: dict, native_seal: dict) -> dict:
        """Bind the initial host score to its exact sealed owner and immutable result."""
        receipt = outcome.get('scoring_result')
        expected = (
            self.attempt_directory(selection['id'], selection['attempt']['sequence']) / 'scoring.json'
        ).as_posix()
        if not isinstance(receipt, dict) or receipt.get('path') != expected:
            raise BenchmarkError('scoring_result_missing', 'Outcome requires its owned initial scoring.json receipt.')
        if receipt not in outcome['artifact_receipts']:
            raise BenchmarkError('scoring_receipt_missing', 'Outcome inventory does not own its initial host score.')
        result = read_reference(self.directory, receipt)
        if (
            result.get('schema_family') != 'pr_similarity_os.scoring'
            or type(result.get('schema_version')) is not int
            or result['schema_version'] != 1
            or result.get('run_id') != self.registry['id']
            or result.get('task_id') != selection['id']
            or result.get('attempt_id') != selection['attempt']['id']
            or result.get('seal') != selection['seal']
            or result.get('metric') != native_seal.get('metric')
            or not isinstance(result.get('metric'), dict)
        ):
            raise BenchmarkError('scoring_owner_mismatch', 'Initial scoring result differs from its sealed owner.')
        scoring = result.get('scoring')
        if (
            not isinstance(scoring, dict)
            or scoring.get('status') not in {'available', 'unavailable'}
            or scoring != outcome['task']['scoring']
            or 'error' not in result
            or 'scoring_error' not in outcome
            or result['error'] != outcome['scoring_error']
        ):
            raise BenchmarkError('scoring_result_mismatch', 'Outcome scoring differs from its immutable host result.')
        return scoring

    def _facts(self, selection: dict, kind: str) -> dict:
        """Bind an execution fact and its receipts to the selected task and attempt."""
        attempt = selection['attempt']
        receipt = selection[kind]
        cache_key = (selection['id'], attempt['id'], kind, receipt['path'], receipt['sha256'], receipt['size_bytes'])
        if cache_key in self._facts_cache:
            return self._facts_cache[cache_key]
        value = read_reference(self.directory, receipt)
        if (
            value.get('schema_family') != 'pr_similarity_os.' + kind
            or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1
        ):
            raise BenchmarkError(
                'attempt_owner_mismatch', 'Execution fact has an unsupported schema family or version.'
            )
        expected = (self.attempt_directory(selection['id'], attempt['sequence']) / (kind + '.json')).as_posix()
        if selection[kind]['path'] != expected or value['run_id'] != self.registry['id']:
            raise BenchmarkError('attempt_owner_mismatch', 'Selected facts have an unexpected run or owner path.')
        if value['basket_sha256'] != self.registry['baseline']['basket']['sha256']:
            raise BenchmarkError('attempt_owner_mismatch', 'Execution facts belong to another frozen basket.')
        if value['config_identity_sha256'] != self.registry['baseline']['config']['identity_sha256']:
            raise BenchmarkError('attempt_owner_mismatch', 'Execution facts belong to another agent configuration.')
        if value.get('agent_input') != self.registry['agent_input']:
            raise BenchmarkError('attempt_owner_mismatch', 'Native fact has a foreign shared input.')
        row = value['task']
        if (
            row['id'] != selection['id']
            or row['final_attempt'] is None
            or any(row['final_attempt'][key] != attempt[key] for key in attempt if key != 'finished_at')
        ):
            raise BenchmarkError('attempt_owner_mismatch', 'Selected fact task/attempt identity differs.')
        if row['setup']['status'] == 'running' or row['execution']['status'] == 'running':
            raise BenchmarkError('attempt_not_terminal', 'Native evidence cannot be sealed while execution is active.')
        frozen = pending_task(self.task(row['id'])['basket_row'])
        for key in (
            'repository',
            'number',
            'url',
            'title',
            'kind',
            'source',
            'diff_policy',
            'before',
            'after',
            'before_tree',
            'after_tree',
            'workspace',
            'agent_task',
        ):
            if row[key] != frozen[key]:
                raise BenchmarkError('attempt_owner_mismatch', 'Execution fact differs from its frozen task.')
        paths = [receipt['path'] for receipt in value['artifact_receipts']]
        if len(paths) != len(set(paths)):
            raise BenchmarkError('invalid_artifact_reference', 'Native fact duplicates an evidence receipt.')
        for receipt in value['artifact_receipts']:
            prefix = self.attempt_directory(row['id'], attempt['sequence']).as_posix() + '/'
            shared = self.registry['agent_input']
            allowed = [shared['config'], *shared['resources']]
            effective = row['artifacts']['effective_config']
            if effective['status'] == 'saved' and receipt['path'] == effective['path']:
                if not re.fullmatch(r'agent/effective/[0-9a-f]{64}\.json', receipt['path']):
                    raise BenchmarkError('attempt_owner_mismatch', 'Invalid shared effective config path.')
                if receipt['sha256'] + '.json' != receipt['path'].split('/')[-1]:
                    raise BenchmarkError('artifact_digest_mismatch', 'Effective filename differs from its digest.')
                allowed.append(receipt)
            if not receipt['path'].startswith(prefix) and receipt not in allowed:
                raise BenchmarkError('attempt_owner_mismatch', 'Evidence receipt escapes its native attempt.')
            path = artifact_path(self.directory, receipt['path'], must_exist=True)
            content = path.read_bytes()
            if len(content) != receipt['size_bytes'] or bytes_sha256(content) != receipt['sha256']:
                raise BenchmarkError('artifact_digest_mismatch', 'Sealed attempt evidence is damaged.')
        self._facts_cache[cache_key] = row
        return row

    def _live_row(self, selection: dict) -> dict:
        """Expose future locations and actual availability without inventing terminal results."""
        row = pending_task(self.task(selection['id'])['basket_row'])
        attempt = selection['attempt']
        if attempt is None:
            return row
        directory = self.attempt_directory(selection['id'], attempt['sequence'])
        row['final_attempt'] = copy.deepcopy(attempt)
        row['locations'] = {
            key: (directory / name).as_posix()
            for key, name in (
                ('agent_log', 'agent.log'),
                ('agent_events', 'events.jsonl'),
                ('runtime_log', 'runtime.log'),
                ('setup_log', 'setup.log'),
                ('outcome', 'outcome.json'),
                ('triplets_manifest', 'triplets/index.json'),
            )
        }
        for key in row['artifacts']:
            location = row['locations'].get(key)
            if location is not None:
                path = artifact_path(self.directory, location)
                if path.is_file():
                    row['artifacts'][key] = location
        phase = selection['progress']['phase']
        if phase == 'running':
            row['execution']['status'] = 'running'
        if phase == 'setup':
            row['setup']['status'] = 'running'
        if selection['processing_status'] == 'interrupted':
            row['execution']['status'] = 'unknown'
            row['setup']['status'] = 'unknown'
            row['scoring'] = unavailable_scoring(
                'unavailable',
                {'code': 'native_seal_missing', 'message': 'Native work stopped before evidence was sealed.'},
            )
        return row

    def _selected_result(self, selection: dict, kind: str) -> dict | None:
        """Choose an exact operation item rather than a newest directory heuristic."""
        receipt = selection[kind]
        if receipt is None:
            return None
        operation = validate_operation(
            self.directory, self.registry, receipt, 'rescore' if kind == 'scoring' else 'reprice'
        )
        if receipt['accepted_revision'] > self.state['revision']:
            raise BenchmarkError('operation_commit_mismatch', 'Selected result is newer than committed state.')
        value = operation.result
        items = value['tasks'] if kind == 'scoring' else value['attempts']
        attempt = selection['attempt']
        matches = [row for row in items if row['task_id'] == selection['id'] and row['attempt_id'] == attempt['id']]
        if len(matches) != 1:
            raise BenchmarkError(
                'operation_owner_mismatch', 'Selected operation result has no unique task/attempt owner.'
            )
        return matches[0]

    def _effective_configs(self) -> list[dict]:
        """Project only current/history configs explicitly owned by selected native seals."""
        configs = []
        for selected in self.state['tasks']:
            attempts = [{**item, 'id': selected['id']} for item in selected['history']]
            if selected['attempt'] is not None:
                attempts.append(selected)
            for attempt in attempts:
                if attempt['seal'] is None:
                    continue
                native = read_reference(self.directory, attempt['seal'])
                effective = native['task']['artifacts']['effective_config']
                if effective['status'] != 'saved':
                    continue
                expected = effective['path']
                if not isinstance(expected, str) or not re.fullmatch(
                    r'agent/effective/[0-9a-f]{64}\.json', expected
                ):
                    raise BenchmarkError('inference_owner_mismatch', 'Effective config has an unexpected shared path.')
                receipts = {item['path']: item for item in native['artifact_receipts']}
                if expected not in receipts:
                    raise BenchmarkError(
                        'inference_owner_mismatch', 'Effective config is not in its selected native seal.'
                    )
                configs.append(
                    {
                        'attempt_id': attempt['attempt']['id'],
                        'source_ref': expected,
                        'config': read_reference(self.directory, receipts[expected]),
                    }
                )
        return configs

    def _publication_fingerprint(self, request: dict) -> dict:
        """Validate the full immutable HTTPS fingerprint even when its old payload is omitted."""
        value = request.get('fingerprint')
        if not isinstance(value, dict) or set(value) != {
            'server',
            'run_id',
            'archive_sha256',
            'report_sha256',
            'overwrite',
        }:
            raise BenchmarkError('publication_fingerprint_invalid', 'Publication fingerprint fields are invalid.')
        server = value['server']
        if not isinstance(server, str):
            raise BenchmarkError('publication_fingerprint_invalid', 'Publication server origin is invalid.')
        parsed = urlsplit(server)
        if (
            parsed.scheme != 'https'
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ('', '/')
            or parsed.query
            or parsed.fragment
            or value['run_id'] != self.registry['id']
            or type(value['overwrite']) is not bool
            or sha256(value) != request.get('fingerprint_sha256')
        ):
            raise BenchmarkError(
                'publication_fingerprint_invalid', 'Publication fingerprint has inconsistent ownership.'
            )
        for field in ('archive_sha256', 'report_sha256'):
            digest = value[field]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in '0123456789abcdef' for char in digest)
            ):
                raise BenchmarkError('publication_fingerprint_invalid', 'Publication content digest is invalid.')
        return value

    def _publication_confirmation(self, request: dict, receipt: dict) -> None:
        """Require completed server identity and public metadata to match the frozen request."""
        if receipt['status'] != 'completed':
            if receipt.get('server_receipt') is not None or receipt.get('public_metadata') is not None:
                raise BenchmarkError(
                    'publication_receipt_invalid', 'Rejected publication cannot claim server confirmation.'
                )
            return
        confirmed = receipt.get('server_receipt')
        fingerprint = self._publication_fingerprint(request)
        expected = {
            'run_id': self.registry['id'],
            'archive_sha256': fingerprint['archive_sha256'],
            'report_sha256': fingerprint['report_sha256'],
            'idempotency_key': request['id'],
        }
        if (
            not isinstance(confirmed, dict)
            or set(confirmed) != {*expected, 'author_login'}
            or any(confirmed[key] != value for key, value in expected.items())
            or not isinstance(confirmed['author_login'], str)
            or not confirmed['author_login']
        ):
            raise BenchmarkError(
                'publication_receipt_invalid', 'Server confirmation differs from the immutable request.'
            )
        metadata = receipt.get('public_metadata')
        if not isinstance(metadata, dict) or metadata.get('inference') != request['metadata']['inference']:
            raise BenchmarkError('publication_receipt_invalid', 'Public inference differs from the frozen config.')
        if (metadata.get('config_source') != request['metadata']['agent_input']['config']
                or type(metadata.get('config_supported')) is not bool
                or metadata.get('benchmark') != request['metadata']['benchmark']):
            raise BenchmarkError('publication_receipt_invalid', 'Public native input or harness metadata differs.')
        tags, groups = metadata.get('tags'), metadata.get('tag_groups')
        if not isinstance(tags, list) or not isinstance(groups, dict) or groups.get('setup') != tags:
            raise BenchmarkError('publication_receipt_invalid', 'Public Setup metadata is inconsistent.')
        generated = generated_groups(request['metadata']['inference'])
        if set(groups) != set(generated) | {'setup'} or any(
            groups.get(key) != value for key, value in generated.items()
        ):
            raise BenchmarkError('publication_receipt_invalid', 'Public generated model/effort group differs.')

    def _validate_publication(self, selected: dict, *, closed: bool) -> dict:
        """Bind a publication journal to one immutable request, payload and optional receipt."""
        identifier = operation_identifier(selected['id'])
        prefix = 'operations/publish/' + identifier + '/'
        if selected['request']['path'] != prefix + 'request.json':
            raise BenchmarkError('publication_owner_mismatch', 'Publication request has a foreign operation path.')
        request = read_reference(self.directory, selected['request'])
        baseline = self.registry['baseline']
        if (
            request.get('schema_family') != 'pr_similarity_os.publish_request'
            or type(request.get('schema_version')) is not int
            or request['schema_version'] != 1
            or request.get('id') != identifier
            or request.get('run_id') != self.registry['id']
            or request.get('basket_sha256') != baseline['basket']['sha256']
            or request.get('config_identity_sha256') != baseline['config']['identity_sha256']
            or request.get('idempotency_key') != identifier
        ):
            raise BenchmarkError('publication_owner_mismatch', 'Publication belongs to different frozen inputs.')
        self._publication_fingerprint(request)
        payload = request['payload']
        path = artifact_path(self.directory, payload['path'])
        available = path.is_file()
        if (
            set(payload) != {'path', 'size_bytes', 'sha256'}
            or type(payload['size_bytes']) is not int
            or payload['size_bytes'] < 0
            or payload['path'] != prefix + 'payload.tar.gz'
            or payload['sha256'] != request['fingerprint']['archive_sha256']
            or (available and reference(self.directory, payload['path']) != payload)
            or (not available and not self.allow_omitted_publish_payloads)
        ):
            raise BenchmarkError('publication_payload_mismatch', 'Frozen publication payload differs from its receipt.')
        if closed:
            if selected['receipt']['path'] != prefix + 'receipt.json':
                raise BenchmarkError('publication_owner_mismatch', 'Publication receipt has a foreign operation path.')
            receipt = read_reference(self.directory, selected['receipt'])
            if (
                receipt.get('schema_family') != 'pr_similarity_os.publish_receipt'
                or type(receipt.get('schema_version')) is not int
                or receipt['schema_version'] != 1
                or receipt.get('id') != identifier
                or receipt.get('run_id') != self.registry['id']
                or receipt.get('request') != selected['request']
                or receipt.get('status') not in ('completed', 'conflict', 'rejected')
            ):
                raise BenchmarkError(
                    'publication_owner_mismatch', 'Closed publication receipt has inconsistent ownership.'
                )
            self._publication_confirmation(request, receipt)
        return request

    def select_publication(self, identifier: str, receipt: ArtifactReference | dict) -> dict:
        """Commit the unresolved exact upload before any authentication or network call."""
        with self._writer():
            selected = {'id': identifier, 'request': copy.deepcopy(receipt)}
            self._validate_publication(selected, closed=False)
            existing = self.state['pending_publish']
            if existing is not None:
                if existing != selected:
                    raise BenchmarkError(
                        'publication_pending', 'Recover the unresolved publication before starting another.'
                    )
                return self.snapshot()
            state = copy.deepcopy(self.state)
            state['pending_publish'] = selected
            return self._publish_selection(state)

    def finish_publication(self, receipt: ArtifactReference | dict) -> dict:
        """Select an immutable confirmation or rejection and clear only its matching pending request."""
        with self._writer():
            pending = self.state['pending_publish']
            if pending is None:
                raise BenchmarkError('publication_not_pending', 'No unresolved publication can accept this receipt.')
            selected = {**copy.deepcopy(pending), 'receipt': copy.deepcopy(receipt)}
            self._validate_publication(selected, closed=True)
            state = copy.deepcopy(self.state)
            state['last_publish'], state['pending_publish'] = selected, None
            return self._publish_selection(state)

    def last_operation(self, kind: str) -> dict | None:
        """Return the exact selected root operation, including results with no task items."""
        if kind not in ('rescore', 'reprice'):
            raise BenchmarkError('invalid_operation_kind', 'Only score and price operations have root selections.')
        return copy.deepcopy(self.state['selected_operations'][kind])

    def read_artifact(self, receipt: dict) -> dict:
        """Read an immutable cost artifact with existing exact-byte ownership validation."""
        return read_reference(self.directory, receipt)

    def price_result(self, selection: dict) -> dict:
        """Expose only an operation-validated selected reprice result to portable billing."""
        return validate_operation(self.directory, self.registry, selection, 'reprice').result

    def validate_usage(self, ledger: dict) -> None:
        """Apply existing usage-schema and session/request ownership checks without new dependencies."""
        validate_usage(ledger)

    def _operation_metadata(self, kind: str) -> dict | None:
        """Derive display metadata from immutable selected receipts rather than another state copy."""
        selected = self.state['selected_operations'][kind]
        if selected is None:
            return None
        receipt = read_reference(self.directory, selected['receipt'])
        metadata = {
            'at': receipt['finished_at'],
            'invocation_id': receipt['invocation_id'],
            'previous_report_sha256': receipt['input_report']['sha256'],
            'backup_path': receipt['input_report']['path'],
            'receipt_path': selected['receipt']['path'],
        }
        if kind == 'reprice':
            metadata['calculation_id'] = selected['id']
        return metadata

    def snapshot(self) -> dict:
        """Project saved timestamps and accepted immutable facts deterministically."""
        report = copy.deepcopy(self.registry['baseline'])
        report['agent_input'] = copy.deepcopy(self.registry['agent_input'])
        report['run'].update(self.state['run'], revision=self.state['revision'], updated_at=self.state['updated_at'])
        report['agent']['resolved_runtime'] = copy.deepcopy(self.state['resolved_runtime'])
        report['metric'] = copy.deepcopy(self.state['metric'])
        report['last_rescore'] = self._operation_metadata('rescore')
        report['last_reprice'] = self._operation_metadata('reprice')
        report['state'] = {'path': '_meta/state.json', 'sha256': self._disk_digest}
        identity = read_json(artifact_path(self.directory, 'config_identity.json', must_exist=True))
        report['inference'] = build_inference(identity, self._effective_configs())
        rows = []
        for selection in self.state['tasks']:
            row = self._live_row(selection)
            locations = row.get('locations', {})
            for kind in ('seal', 'outcome'):
                if selection[kind] is not None:
                    row = copy.deepcopy(self._facts(selection, kind))
            row.update(
                ordinal=selection['ordinal'],
                directory=self.task(selection['id'])['path'],
                processing_status=selection['processing_status'],
                progress=copy.deepcopy(selection['progress']),
            )
            row['final_attempt'] = copy.deepcopy(selection['attempt'])
            row['locations'] = locations
            row['artifacts']['outcome'] = None if selection['outcome'] is None else selection['outcome']['path']
            score = self._selected_result(selection, 'scoring')
            if score is not None:
                row['scoring'] = copy.deepcopy(score['scoring'])
                if row['infrastructure_error'] is not None and row['infrastructure_error']['stage'] == 'score':
                    row['infrastructure_error'] = None
            price = self._selected_result(selection, 'pricing')
            if price is not None:
                row['telemetry']['api_equivalent'] = copy.deepcopy(price['api_equivalent'])
            rows.append(row)
        report['tasks'] = rows
        report['summary'] = summary(rows)
        try:
            report['costs'] = build_cost_summary(self)
        except CostEvidenceError as error:
            raise BenchmarkError(error.code, error.message) from error
        return report

    def _rebuild(self) -> dict:
        """Restore the derived snapshot without changing selection, revision or timestamps."""
        report = self.snapshot()
        validate_report(report)
        write_json(self.directory / 'report.json', report)
        return report

    def _commit(self, report: dict, *, selected_state: dict | None = None) -> dict:
        """Commit scheduling references first, then atomically publish their report view."""
        candidate = copy.deepcopy(self.state if selected_state is None else selected_state)
        candidate['revision'] += 1
        candidate['updated_at'] = utc_now()
        candidate['run'] = {
            key: report['run'][key] for key in ('status', 'finished_at', 'error', 'current_invocation_id')
        }
        candidate['resolved_runtime'] = copy.deepcopy(report['agent']['resolved_runtime'])
        candidate['metric'] = copy.deepcopy(report['metric'])
        for record, row in zip(candidate['tasks'], report['tasks'], strict=True):
            if record['id'] != row['id']:
                raise BenchmarkError('state_owner_mismatch', 'Cannot reorder or substitute a frozen task queue.')
            if record['processing_status'] == 'completed' and row['processing_status'] != 'completed':
                raise BenchmarkError(
                    'terminal_task_closed', 'Completed native tasks cannot be reopened by report edits.'
                )
            record['processing_status'] = row['processing_status']
            if record['processing_status'] in {'completed', 'interrupted', 'retry_pending'}:
                phase = record['processing_status'].replace('_', ' ')
                if record['progress']['phase'] != phase:
                    record['progress'] = {'phase': phase, 'updated_at': candidate['updated_at']}
            previous, current = record['attempt'], row['final_attempt']
            if (previous is None) != (current is None) or (
                previous is not None and any(previous[key] != current[key] for key in previous if key != 'finished_at')
            ):
                raise BenchmarkError('attempt_owner_mismatch', 'Only reservation may change native attempt ownership.')
            record['attempt'] = copy.deepcopy(current)
        proposed = RunStore(self.directory, self.registry, candidate)
        proposed._facts_cache = self._facts_cache
        proposed.validate()
        projected = proposed.snapshot()
        validate_report(projected)
        write_json(self.directory / '_meta/state.json', candidate)
        self.state = candidate
        self._disk_digest = bytes_sha256(document_bytes(candidate))
        write_json(self.directory / 'report.json', projected)
        return projected

    @contextmanager
    def _writer(self) -> Iterator[None]:
        """Reuse an active coordinator lease or acquire a standalone operation lock."""
        if self.lease is not None:
            if not self.lease.active or self.lease.directory != self.directory.resolve():
                raise BenchmarkError(
                    'writer_lease_expired', 'Run writer lease is expired or belongs to another directory.'
                )
            self._check_revision()
            yield
        else:
            with run_lock(self.directory):
                self._check_revision()
                yield

    def _check_revision(self) -> None:
        """Prevent a stale independently opened store from losing another writer's update."""
        content = artifact_path(self.directory, '_meta/state.json', must_exist=True).read_bytes()
        if bytes_sha256(content) != self._disk_digest:
            raise BenchmarkError('stale_run_state', 'Run state changed since this writer opened it; reopen the run.')

    def commit(self, report: dict) -> dict:
        """Commit under one writer lock, rejecting independently stale state snapshots."""
        with self._writer():
            return self._commit(report)

    def rebuild(self) -> dict:
        """Write the deterministic report under the same state-owner boundary."""
        with self._writer():
            return self._rebuild()

    def reserve(self, task_id: str, attempt: dict) -> dict:
        """Persist unique start ownership and sequence before an executor can spawn."""
        with self._writer():
            state = copy.deepcopy(self.state)
            selected = next(row for row in state['tasks'] if row['id'] == task_id)
            if selected['processing_status'] not in ('pending', 'retry_pending', 'interrupted'):
                raise BenchmarkError(
                    'attempt_not_reservable', 'Active or completed tasks cannot start a new native attempt.'
                )
            if selected['seal'] is not None and selected['outcome'] is None:
                raise BenchmarkError(
                    'host_recovery_required', 'Sealed evidence requires host recovery, not native replay.'
                )
            if attempt['sequence'] != selected['next_sequence']:
                raise BenchmarkError('attempt_sequence_conflict', 'Native attempt sequence was already reserved.')
            if any(row['attempt'] is not None and row['attempt']['id'] == attempt['id'] for row in self.state['tasks']):
                raise BenchmarkError('attempt_owner_mismatch', 'Native attempt UUID was already registered.')
            directory = self.attempt_directory(task_id, attempt['sequence'])
            target = self.directory / directory
            artifact_path(self.directory, (directory / 'start.json').as_posix())
            if target.exists():
                raise BenchmarkError(
                    'uncommitted_reservation', 'Resume must recover the existing reservation before new work.'
                )
            baseline = self.registry['baseline']
            start = {
                'schema_family': 'pr_similarity_os.start',
                'schema_version': 1,
                'run_id': self.registry['id'],
                'task_id': task_id,
                'attempt': copy.deepcopy(attempt),
                'basket_sha256': baseline['basket']['sha256'],
                'config_identity_sha256': baseline['config']['identity_sha256'],
            }
            self._write_start(target, start)
            self._reserve_selection(
                selected, attempt, reference(self.directory, (directory / 'start.json').as_posix()), 'running'
            )
            return self._publish_selection(state)

    def _write_start(self, target: Path, start: dict) -> None:
        """Publish a complete start directory so a crash cannot strand an empty reservation."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.reservation-', dir=target.parent) as temporary:
            staged = Path(temporary)
            write_json(staged / 'start.json', start)
            os.rename(staged, target)
            descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _reserve_selection(self, selected: dict, attempt: dict, start: ArtifactReference, status: str) -> None:
        """Consume a reserved sequence while preserving every previous native owner."""
        if selected['attempt'] is not None:
            selected['history'].append(
                {
                    key: copy.deepcopy(selected[key])
                    for key in ('attempt', 'start', 'seal', 'outcome', 'scoring', 'pricing')
                }
            )
        selected.update(
            attempt=copy.deepcopy(attempt),
            next_sequence=attempt['sequence'] + 1,
            retry_index=attempt['retry_index'],
            processing_status=status,
            progress={'phase': 'preparing', 'updated_at': utc_now()},
            start=start,
            seal=None,
            outcome=None,
            scoring=None,
            pricing=None,
        )

    def _publish_selection(self, state: dict) -> dict:
        """Validate a copied selection before replacing the authoritative state."""
        proposed = RunStore(self.directory, self.registry, state)
        proposed._facts_cache = self._facts_cache
        proposed.validate()
        return self._commit(proposed.snapshot(), selected_state=state)

    def prepare_invocation(self, invocation_id: str, *, resumed: bool) -> dict:
        """Record a new command and reset only unfinished native budgets in a new epoch."""
        with self._writer():
            state = copy.deepcopy(self.state)
            state['run']['current_invocation_id'] = operation_identifier(invocation_id)
            if resumed:
                if state['run']['status'] in ('running', 'stopping'):
                    state['run']['status'] = 'interrupted'
                for row in state['tasks']:
                    if row['processing_status'] != 'completed':
                        row['retry_epoch'] += 1
                        row['retry_index'] = 0
            return self._publish_selection(state)

    def recover_reservations(self) -> list[dict]:
        """Consume intact uncommitted starts without issuing native work or guessing results.

        Only the next registered sequence may be adopted. A missing or corrupt
        authoritative state must be restored separately; this is not state reconstruction.

        Returns:
            Native owners recovered as interrupted reservations, for coordinator notices.

        Raises:
            BenchmarkError: If start ownership is corrupt or its state cannot commit.

        """
        with self._writer():
            state = copy.deepcopy(self.state)
            recovered = []
            for selected in state['tasks']:
                relative = self.attempt_directory(selected['id'], selected['next_sequence']) / 'start.json'
                path = artifact_path(self.directory, relative.as_posix())
                if not path.is_file():
                    continue
                if selected['processing_status'] not in ('pending', 'retry_pending', 'interrupted'):
                    raise BenchmarkError(
                        'unexpected_reservation', 'An active or completed task has an uncommitted start.'
                    )
                start = read_json(path)
                attempt = start['attempt']
                if attempt['sequence'] != selected['next_sequence'] or attempt['finished_at'] is not None:
                    raise BenchmarkError(
                        'attempt_start_mismatch', 'Uncommitted start has an unexpected native sequence.'
                    )
                self._reserve_selection(
                    selected, attempt, reference(self.directory, relative.as_posix()), 'interrupted'
                )
                recovered.append(
                    {'task_id': selected['id'], 'attempt_id': attempt['id'], 'sequence': attempt['sequence']}
                )
            if recovered:
                self._publish_selection(state)
            return recovered

    def accept_fact(
        self, task_id: str, kind: str, receipt: ArtifactReference | dict, *, processing_status: str | None = None
    ) -> dict:
        """Select an explicitly closed native fact, never discover one from file existence.

        Args:
            task_id: Frozen task owning the reserved attempt.
            kind: Seal or final outcome written by its executor.
            receipt: Exact path, byte size and digest of that immutable fact.
            processing_status: Coordinator decision after accepting a terminal outcome.

        Returns:
            The derived report after the state selection has committed.

        Raises:
            BenchmarkError: If fact ownership, ordering or immutable selection fails.

        """
        with self._writer():
            if kind not in ('seal', 'outcome'):
                raise BenchmarkError('invalid_fact_kind', 'Only native seals and outcomes can be accepted.')
            state = copy.deepcopy(self.state)
            selected = next(row for row in state['tasks'] if row['id'] == task_id)
            if selected['attempt'] is None:
                raise BenchmarkError('attempt_start_missing', 'Cannot select evidence before native reservation.')
            if selected[kind] is not None and selected[kind] != receipt:
                raise BenchmarkError('immutable_fact_conflict', 'An accepted native fact cannot be replaced.')
            if kind == 'outcome' and selected['seal'] is None:
                raise BenchmarkError('attempt_seal_missing', 'Final outcome requires an accepted native seal.')
            if selected['processing_status'] == 'completed' and processing_status not in (None, 'completed'):
                raise BenchmarkError('terminal_task_closed', 'Accepted native facts cannot reopen a completed task.')
            selected[kind] = copy.deepcopy(receipt)
            proposed = RunStore(self.directory, self.registry, state)
            proposed._facts_cache = self._facts_cache
            fact = read_reference(self.directory, receipt)
            if kind == 'outcome' and fact.get('seal') != selected['seal']:
                raise BenchmarkError('attempt_seal_mismatch', 'Final outcome references a different native seal.')
            selected['attempt'] = copy.deepcopy(fact['task']['final_attempt'])
            if processing_status is not None:
                selected['processing_status'] = processing_status
            proposed.validate()
            return self._commit(proposed.snapshot(), selected_state=state)

    def observe(self, task_id: str, phase: str, updated_at: str) -> dict:
        """Publish a bounded phase observation without treating it as an execution result."""
        with self._writer():
            state = copy.deepcopy(self.state)
            selected = next(row for row in state['tasks'] if row['id'] == task_id)
            if selected['attempt'] is None:
                raise BenchmarkError('phase_owner_mismatch', 'Cannot observe a task before its attempt is reserved.')
            selected['progress'] = {'phase': phase, 'updated_at': updated_at}
            proposed = RunStore(self.directory, self.registry, state)
            proposed._facts_cache = self._facts_cache
            return self._commit(proposed.snapshot(), selected_state=state)

    def _validate_start(self, selection: dict) -> None:
        """Require a persisted start receipt before accepting any active native ownership."""
        if selection['start'] is None:
            raise BenchmarkError('attempt_start_missing', 'Reserved attempt has no durable start receipt.')
        start = read_reference(self.directory, selection['start'])
        attempt = selection['attempt']
        expected = (self.attempt_directory(selection['id'], attempt['sequence']) / 'start.json').as_posix()
        baseline = self.registry['baseline']
        if (
            selection['start']['path'] != expected
            or start.get('schema_family') != 'pr_similarity_os.start'
            or type(start.get('schema_version')) is not int
            or start['schema_version'] != 1
            or start['run_id'] != self.registry['id']
            or start['task_id'] != selection['id']
            or start['basket_sha256'] != baseline['basket']['sha256']
            or start['config_identity_sha256'] != baseline['config']['identity_sha256']
        ):
            raise BenchmarkError('attempt_start_mismatch', 'Start receipt has incompatible task/run inputs.')
        if any(
            start['attempt'][key] != attempt[key]
            for key in ('id', 'invocation_id', 'sequence', 'retry_index', 'started_at')
        ):
            raise BenchmarkError('attempt_start_mismatch', 'Start receipt differs from the registered native attempt.')

    def prepare_operation(
        self,
        kind: str,
        items: list[dict],
        *,
        invocation_id: str,
        metric: dict | None = None,
        operation_id: str | None = None,
        extra: dict | None = None,
        artifacts: dict[str, bytes] | None = None,
        preallocated: bool = False,
        status: str = 'completed',
    ) -> dict:
        """Stage immutable offline results without changing any accepted selection."""
        with self._writer():
            if kind not in ('rescore', 'reprice') or self.state['run']['status'] in ('running', 'stopping'):
                raise BenchmarkError(
                    'operation_not_allowed', 'Offline results require a stopped run and a supported kind.'
                )
            if status not in ('completed', 'completed_with_gaps', 'no_op', 'failed'):
                raise BenchmarkError(
                    'invalid_operation_status', 'Operation receipt has an unsupported completion status.'
                )
            identifier = operation_identifier(str(uuid.uuid4()) if operation_id is None else operation_id)
            relative = Path('operations') / kind / identifier
            artifact_path(self.directory, (relative / 'result.json').as_posix())
            directory = self.directory / relative
            self._allocate_operation(directory, identifier, kind, preallocated=preallocated)
            source = artifact_path(self.directory, '_meta/state.json', must_exist=True).read_bytes()
            atomic_write(directory / 'input_state.json', source)
            write_json(directory / 'input_report.json', self.snapshot())
            baseline = self.registry['baseline']
            owner = {
                'schema_version': 1,
                'id': identifier,
                'kind': kind,
                'run_id': self.registry['id'],
                'basket_sha256': baseline['basket']['sha256'],
                'config_identity_sha256': baseline['config']['identity_sha256'],
            }
            result = {
                **owner,
                'schema_family': 'pr_similarity_os.operation_result',
                'metric': metric,
                ('tasks' if kind == 'rescore' else 'attempts'): items,
            }
            for name, content in (artifacts or {}).items():
                if name in ('input_state.json', 'input_report.json', 'result.json', 'receipt.json'):
                    raise BenchmarkError(
                        'operation_artifact_conflict', 'Additional evidence cannot replace operation facts.'
                    )
                target = artifact_path(directory, name)
                atomic_write(target, content)
            write_json(directory / 'result.json', result)
            result_ref = reference(self.directory, (relative / 'result.json').as_posix())
            now = utc_now()
            receipt = {
                **owner,
                'schema_family': 'pr_similarity_os.operation_receipt',
                'status': status,
                'invocation_id': invocation_id,
                'finished_at': now,
                'result': result_ref,
                'input_state': reference(self.directory, (relative / 'input_state.json').as_posix()),
                'input_report': reference(self.directory, (relative / 'input_report.json').as_posix()),
                'details': extra or {},
            }
            write_json(directory / 'receipt.json', receipt)
            selection = {
                'id': identifier,
                'kind': kind,
                'accepted_revision': self.state['revision'] + 1,
                'result': result_ref,
                'receipt': reference(self.directory, (relative / 'receipt.json').as_posix()),
            }
            if status in ('completed', 'completed_with_gaps'):
                validate_operation(self.directory, self.registry, selection, kind)
            return selection

    def _allocate_operation(self, directory: Path, identifier: str, kind: str, *, preallocated: bool) -> None:
        """Complete only a declared owned pricing input directory, never reuse finished facts."""
        if not preallocated:
            directory.mkdir(parents=True, exist_ok=False)
            return
        if kind != 'reprice':
            raise BenchmarkError('invalid_operation_kind', 'Only frozen pricing inputs may preallocate an operation.')
        request = read_json(artifact_path(directory, 'request.json', must_exist=True))
        baseline = self.registry['baseline']
        if (
            request.get('schema_family') != 'pr_similarity_os.pricing_request'
            or type(request.get('schema_version')) is not int
            or request['schema_version'] != 1
            or request.get('id') != identifier
            or request.get('kind') != kind
            or request.get('run_id') != self.registry['id']
            or request.get('basket_sha256') != baseline['basket']['sha256']
            or request.get('config_identity_sha256') != baseline['config']['identity_sha256']
        ):
            raise BenchmarkError('operation_owner_mismatch', 'Preallocated pricing inputs have a foreign owner.')
        for name in ('input_state.json', 'input_report.json', 'result.json', 'receipt.json'):
            path = artifact_path(directory, name)
            if path.exists():
                raise BenchmarkError('immutable_operation_conflict', 'Operation facts already exist; preserve them.')

    def accept_operation(self, selection: dict) -> dict:
        """Commit only a validated result computed from this exact current state."""
        with self._writer():
            kind = selection['kind']
            operation = validate_operation(self.directory, self.registry, selection, kind)
            if operation.receipt['input_state']['sha256'] != self._disk_digest:
                raise BenchmarkError(
                    'operation_inputs_stale', 'Offline result was computed from a different state revision.'
                )
            original = self.snapshot()
            candidate = RunStore(
                self.directory,
                self.registry,
                copy.deepcopy(self.state),
                lease=self.lease,
                state_digest=self._disk_digest,
            )
            candidate._facts_cache = self._facts_cache
            name, field = ('tasks', 'scoring') if kind == 'rescore' else ('attempts', 'pricing')
            keyed = {(row['task_id'], row['attempt_id']) for row in operation.result[name]}
            for row in candidate.state['tasks']:
                attempts = [*row['history'], row] if kind == 'reprice' else [row]
                for item in attempts:
                    attempt = item['attempt']
                    if attempt is not None and (row['id'], attempt['id']) in keyed:
                        item[field] = copy.deepcopy(selection)
            candidate.state['selected_operations'][kind] = copy.deepcopy(selection)
            if kind == 'rescore':
                original['metric'] = copy.deepcopy(operation.result['metric'])
            original['run']['current_invocation_id'] = operation.receipt['invocation_id']
            previous_digest = candidate._disk_digest
            try:
                return candidate._commit(original)
            finally:
                if candidate._disk_digest != previous_digest:
                    # State already committed even if writing its report was interrupted.
                    self.state, self._disk_digest = candidate.state, candidate._disk_digest
