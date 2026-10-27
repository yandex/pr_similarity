"""Python preparation failures and runtime identity gates never invoke a model."""

import hashlib
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, NoReturn

import pytest
import yaml
from python_fixtures import python_receipt
from test_artifact_workflow import _inputs, _process_identity
from test_runner import config_and_basket, fake_attempt, provision_fixture

from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.execution import process as process_module
from benchmark.execution.bin_projection import BinProjection
from benchmark.execution.opencode import Invocation
from benchmark.execution.python_runtime import PythonRuntime
from benchmark.execution.sandbox import Sandbox
from benchmark.models import Report
from benchmark.models.evidence import Seal
from benchmark.models.runtime import PythonFile, PythonReceipt
from benchmark.persistence.artifact_refs import read_reference, reference
from benchmark.persistence.python_evidence import validate_python_fact
from benchmark.persistence.run_store import RunStore
from benchmark.persistence.storage import read_json, write_json
from benchmark.runtime import coordinator
from benchmark.runtime.lifecycle import Stop


def _forbidden_model(*_args: object, **_kwargs: object) -> NoReturn:
    """Fail immediately if preparation reaches a native diagnostic or model call."""
    raise AssertionError('Python preparation must finish before native execution.')


def test_setup_damage_fails_before_native_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real setup can damage its own venv, but the subsequent check stops the agent."""
    projection_roots: list[Path] = []
    prepare = BinProjection._prepare

    def observe(self: BinProjection, root: Path, venv: str, directories: tuple[Path, ...]) -> list[str]:
        """Track the real invocation projection without changing its construction."""
        projection_roots.append(root)
        return prepare(self, root, venv, directories)

    monkeypatch.setattr(BinProjection, '_prepare', observe)
    inputs = _inputs(tmp_path)
    script = tmp_path / 'setup.sh'
    script.write_text('rm "$HOME/.venv/pyvenv.cfg"\n')
    path = tmp_path / 'config.yaml'
    data = yaml.safe_load(path.read_text())
    data['agent']['setup'] = {'script': str(script), 'timeout_seconds': 30}
    path.write_text(yaml.safe_dump(data))
    config = load_config(path)
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: inputs.runtime)
    monkeypatch.setattr(Invocation, 'dump_effective_config', _forbidden_model)
    monkeypatch.setattr(Invocation, 'run', _forbidden_model)
    code, directory = coordinator.run(config, inputs.dataset, output=tmp_path / 'runs', progress='off')
    report = Report.model_validate(read_json(directory / 'report.json'))
    task = report.tasks[0]
    assert code != 0 or report.run.status == 'completed_with_errors'
    assert task.execution.status == 'not_started'
    assert task.setup.status == 'completed'
    assert task.infrastructure_error is not None and task.infrastructure_error.code == 'python_preflight_failed'
    log = next((directory / 'tasks').glob('*/attempts/*/python-check.log'))
    assert 'Attempt prefix changed' in log.read_text()
    assert not list((directory / 'tasks').glob('*/attempts/*/effective-config.receipt.json'))
    assert len(projection_roots) == 1
    assert not projection_roots[0].exists()
    assert not list(directory.rglob('bin-1'))


@pytest.mark.parametrize('change', ['patch', 'stdlib', 'binary'])
def test_resume_rejects_changed_python_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Resume compares the frozen interpreter and stdlib without replacing their evidence."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    code, directory = coordinator.run(
        config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['interrupt'])
    )
    assert code == 130
    original = (directory / '_meta/python.json').read_bytes()
    data = python_receipt().to_wire()
    if change == 'patch':
        data['version'] = '3.12.1'
    else:
        files = (
            [PythonFile(path='bin/python3.12', sha256='b' * 64, size_bytes=2)]
            if change == 'binary'
            else [*python_receipt().files, PythonFile(path='lib/python3.12/ssl.py', sha256='b' * 64, size_bytes=2)]
        )
        data['files'] = [item.to_wire() for item in files]
        data['content_sha256'] = hashlib.sha256(
            ''.join(f'{item.path}\0{item.sha256}\0{item.size_bytes}\n' for item in files).encode()
        ).hexdigest()
    changed = PythonRuntime(Path('/usr'), PythonReceipt.model_validate(data))
    monkeypatch.setattr(coordinator, 'prepare_python', lambda *_args, **_kwargs: changed)
    status, _ = coordinator.run(config, basket, resume=directory, attempt_function=_forbidden_model)
    assert status != 0
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert report.run.error is not None and report.run.error.code == 'resume_python_mismatch'
    assert (directory / '_meta/python.json').read_bytes() == original
    assert len(list((directory / 'tasks').glob('*/attempts/*/start.json'))) == 1


def test_resume_can_finish_initial_python_preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid v0.3.0 run that issued no attempt can retry its failed initial preflight."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)

    def fail_preparation(*_args: object, **_kwargs: object) -> NoReturn:
        """Simulate a base-runtime failure before any task reservation."""
        raise BenchmarkError('python_library_missing', 'Fixture Python dependency is missing.')

    with monkeypatch.context() as local:
        local.setattr(coordinator, 'prepare_python', fail_preparation)
        code, directory = coordinator.run(config, basket, output=tmp_path / 'runs', attempt_function=_forbidden_model)
    assert code != 0 and not list((directory / 'tasks').glob('*/attempts/*/start.json'))
    assert coordinator.run(config, basket, resume=directory, attempt_function=fake_attempt(['success']))[0] == 0


@pytest.mark.parametrize('phase', ['setup', 'diagnostics'])
def test_setup_without_python_probe_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """A setup-only attempt still requires its successful pre-setup Python evidence."""
    provision_fixture(monkeypatch)
    config, basket = config_and_basket(tmp_path)
    _, directory = coordinator.run(config, basket, output=tmp_path / 'runs', attempt_function=fake_attempt(['success']))
    store = RunStore.open(directory)
    selection = store.state.tasks[0]
    assert selection.seal is not None and selection.attempt is not None
    seal = read_reference(directory, selection.seal, Seal)
    prefix = store.attempt_directory(selection.id, selection.attempt.sequence).as_posix() + '/'
    seal.task.execution.status = 'not_started'
    seal.task.setup.status = 'error' if phase == 'setup' else 'skipped'
    if phase == 'diagnostics':
        write_json(directory / (prefix + 'effective-config.receipt.json'), {'status': 'warning'})
        seal.artifact_receipts.append(reference(directory, prefix + 'effective-config.receipt.json'))
    seal.artifact_receipts = [item for item in seal.artifact_receipts if item.path != prefix + 'python.json']
    with pytest.raises(BenchmarkError) as error:
        validate_python_fact(directory, seal, store.state.python_runtime, prefix)
    assert error.value.code == 'python_probe_missing'


@pytest.mark.parametrize('mode', ['cancel', 'timeout'])
@pytest.mark.parametrize('observation_delay', [0, 1.2])
def test_python_preparation_stops_children_and_removes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, observation_delay: float
) -> None:
    """Interrupt an actual sandbox bootstrap child before any model-side operation."""
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: inputs.runtime)
    monkeypatch.setattr(Invocation, 'dump_effective_config', _forbidden_model)
    monkeypatch.setattr(Invocation, 'run', _forbidden_model)
    original_probe = Sandbox._python_process
    original_start = process_module._start_process
    homes: list[Path] = []
    children: list[subprocess.Popen[bytes]] = []
    descendants: list[int] = []
    stop = threading.Event()

    def observe(child: subprocess.Popen[bytes]) -> None:
        """Record only test-owned descendants and request cancellation when selected."""
        time.sleep(observation_delay)
        deadline = time.monotonic() + 30
        while not (homes[-1] / 'bootstrap-started').exists() and child.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError('Bootstrap did not reach its startup marker.')
            time.sleep(0.02)
        assert child.poll() is None
        pending = [child.pid]
        for _ in range(4):
            following = []
            for pid in pending:
                path = Path(f'/proc/{pid}/task/{pid}/children')
                if path.exists():
                    following.extend(int(value) for value in path.read_text().split())
            descendants.extend(following)
            pending = following
        assert descendants
        if mode == 'cancel':
            assert isinstance(stop, Stop)
            stop.signal(signal.SIGINT, None)

    def start(argv: list[str], cwd: Path, env: dict[str, str], input_file: BinaryIO) -> subprocess.Popen[bytes]:
        """Observe the real delayed bwrap process without replacing process supervision."""
        child = original_start(argv, cwd, env, input_file)
        if any('bootstrap started' in value for value in argv):
            children.append(child)
            try:
                # The supervisor cannot evaluate its deadline until this hook returns.
                observe(child)
            except BaseException:
                process_module._signal_group(child, signal.SIGKILL)
                child.wait()
                raise
        return child

    def probe(
        self: Sandbox,
        argv: list[str],
        *,
        timeout: int,
        cancel: threading.Event | None,
        log: Path,
        bindings: dict[str, str] | None = None,
    ) -> list[str]:
        """Delay only the attempt's bootstrap; keep the common runtime preflight real."""
        nonlocal stop
        if self.home.parent.name.startswith('benchmark-attempt-') and 'venv' in argv:
            assert cancel is not None
            stop = cancel
            homes.append(self.home)
            argv = ['/bin/sh', '-c', 'echo "bootstrap started"; touch /sandbox-home/bootstrap-started; exec sleep 60']
            # An expired deadline is evaluated only after the live descendants are recorded.
            timeout = 60 if mode == 'cancel' else 0
        return original_probe(self, argv, timeout=timeout, cancel=cancel, log=log, bindings=bindings)

    monkeypatch.setattr(process_module, '_start_process', start)
    monkeypatch.setattr(Sandbox, '_python_process', probe)
    code, directory = coordinator.run(inputs.config, inputs.dataset, output=tmp_path / 'runs', progress='off')
    assert children and all(child.poll() is not None for child in children)
    assert descendants and all(
        (identity := _process_identity(pid)) is None or identity[0] == 'Z' for pid in descendants
    )
    assert homes and all(not home.exists() for home in homes)
    report = Report.model_validate(read_json(directory / 'report.json'))
    assert report.tasks[0].execution.status == 'not_started'
    if mode == 'cancel':
        assert code == 130
    else:
        error = report.tasks[0].infrastructure_error
        assert error is not None and error.code == 'python_preflight_failed'
    assert 'bootstrap started' in next((directory / 'tasks').glob('*/attempts/*/python-prepare.log')).read_text()
