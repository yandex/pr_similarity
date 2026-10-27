"""State-owned stopped-run fixtures exercise production execution without model access."""

import uuid
from pathlib import Path
from typing import NoReturn

import pytest
import yaml
from python_fixtures import freeze_python
from test_attempt_executor import controlled_native
from test_basket import payload

from benchmark.config import RunConfig, load_config
from benchmark.errors import BenchmarkError
from benchmark.execution.provisioning import Runtime
from benchmark.models.evidence import NativeIdentity
from benchmark.models.report import ReportAgentResolvedRuntime, ReportAttempt
from benchmark.models.runtime import ProvisioningReceipt
from benchmark.persistence.artifact_refs import reference
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import run_lock, sha256, utc_now, write_json
from benchmark.runtime import attempt as executor
from benchmark.runtime.commands import Command
from benchmark.runtime.coordinator import initial_report
from benchmark.runtime.lifecycle import Stop
from benchmark.validation.basket import Basket, load_basket


def _inputs(tmp_path: Path, attempts: int) -> tuple[RunConfig, Basket, Path]:
    """Freeze operator config and a complete basket independently of execution setup."""
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'agent': {
                    'name': 'fixture',
                    'runtime': 'opencode:v1.18.33',
                    'config': {'opencode_json': {'model': 'fixture/model'}},
                    'retries': {'max_retries': max(0, attempts - 1), 'categories': []},
                },
            }
        )
    )
    config = load_config(config_path)
    dataset = tmp_path / 'basket.json'
    write_json(dataset, payload())
    basket = load_basket(dataset)
    return config, basket, dataset


def _fail_score(*_args: object, **_kwargs: object) -> NoReturn:
    """Close native facts before a reproducible host-only failure."""
    raise BenchmarkError('scoring_failed', 'Fixture host scoring failed.')


def stopped_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, score_error: bool = False, attempts: int = 1
) -> tuple[Path, RunConfig, Path]:
    """Create accepted native seal/outcome and a stopped authoritative state."""
    config, basket, dataset = _inputs(tmp_path, attempts)
    directory = tmp_path / 'run'
    directory.mkdir()
    invocation_id = str(uuid.uuid4())
    initial = initial_report(config, basket, '11111111-1111-4111-8111-111111111111', invocation_id)
    runtime = Runtime(
        Path('/usr/bin/true'),
        ProvisioningReceipt.model_validate(
            {
                'selector': 'opencode:v1.18.33',
                'actual_version': '1.18.33',
                'asset_name': 'fixture.tar.gz',
                'platform': 'linux-x64-glibc-baseline',
                'archive_sha256': 'a' * 64,
                'binary_sha256': 'b' * 64,
            }
        ),
    )
    with run_lock(directory) as lease:
        store = RunStore.create(directory, initial, basket, config, lease=lease)
        write_json(directory / '_meta/runtime.json', runtime.receipt)
        current = store.snapshot()
        current.agent.python_runtime = freeze_python(directory)
        current.agent.resolved_runtime = ReportAgentResolvedRuntime.model_validate(
            {
                'version': '1.18.33',
                'asset_name': 'fixture.tar.gz',
                'platform': 'linux-x64-glibc-baseline',
                'archive_sha256': 'a' * 64,
                'binary_sha256': 'b' * 64,
                'receipt': '_meta/runtime.json',
            }
        )
        store.commit(current)
        command = Command(
            directory, 'run', invocation_id=invocation_id, config=config.data, progress_total=1, level='ERROR'
        )
        registration = store.registry.tasks[0]
        errors = [{'name': 'APIError', 'data': {'statusCode': 503}}] if attempts > 1 else []
        controlled_native(monkeypatch, registration, errors=errors)
        identity = NativeIdentity.model_validate(
            {
                'run_id': store.registry.id,
                'basket_sha256': basket.sha256,
                'config_identity_sha256': sha256(config.identity()),
                'runtime_binary_sha256': 'b' * 64,
                'python_runtime': store.state.python_runtime,
                'metric': initial.metric,
            }
        )
        try:
            for sequence in range(1, attempts + 1):
                attempt = ReportAttempt.model_validate(
                    {
                        'id': str(uuid.uuid4()),
                        'invocation_id': invocation_id,
                        'sequence': sequence,
                        'retry_index': sequence - 1,
                        'started_at': utc_now(),
                        'finished_at': None,
                    }
                )
                store.reserve(registration.id, attempt)
                with monkeypatch.context() as local:
                    if score_error:
                        local.setattr(executor, 'score_saved', _fail_score)
                    outcome = executor.run_attempt(
                        config,
                        registration.basket_row,
                        directory,
                        identity,
                        runtime,
                        Path('/usr/bin/bwrap'),
                        command,
                        attempt,
                        Stop(),
                        registration=registration,
                    )
                store.accept_fact(registration.id, 'seal', outcome.seal)
                assert outcome.task.artifacts.outcome is not None
                outcome_ref = reference(directory, outcome.task.artifacts.outcome)
                status = 'retry_pending' if sequence < attempts else ('interrupted' if score_error else 'completed')
                store.accept_fact(registration.id, 'outcome', outcome_ref, processing_status=status)
                errors.clear()
            report = store.snapshot()
            report.run.status = 'interrupted' if score_error else 'completed'
            report.run.finished_at = utc_now()
            store.commit(report)
            command.finish('interrupted' if score_error else 'completed')
        finally:
            command.close()
    return directory, config, dataset


def empty_run(tmp_path: Path) -> Path:
    """Close a valid registry before any attempt reservation or native provisioning."""
    config, basket, _dataset = _inputs(tmp_path, 1)
    directory = tmp_path / 'run'
    directory.mkdir()
    invocation_id = str(uuid.uuid4())
    initial = initial_report(config, basket, str(uuid.uuid4()), invocation_id)
    with run_lock(directory) as lease:
        store = RunStore.create(directory, initial, basket, config, lease=lease)
        command = Command(directory, 'run', invocation_id=invocation_id, level='ERROR')
        try:
            report = store.snapshot()
            report.run.status = 'interrupted'
            store.commit(report)
            command.finish('interrupted')
        finally:
            command.close()
    return directory
