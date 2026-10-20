"""Real Make/CLI smoke over local Git and a process-local fake GitHub."""

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from basket_collector.contracts import (
    BasketDocument,
    CollectDocument,
    FilterDocument,
    TasksDocument,
)
from basket_collector.storage import (
    current_producer,
    read_checkpoint,
    read_document,
    stage_sha256,
)
from fixture_helpers import task_identity

PROJECT = Path(__file__).resolve().parents[1]
SITE = Path(__file__).resolve().parent / 'fixtures'


def _git(repo: Path, *args: str) -> str:
    """Run a Git command in the smoke fixture repository."""
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def make_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str], Path]:
    """Create local Git, GitHub responses, configuration, and subprocess env."""
    repository = tmp_path / 'local source'
    repository.mkdir()
    _git(repository, 'init', '-q')
    _git(repository, 'config', 'user.name', 'Smoke')
    _git(repository, 'config', 'user.email', 'smoke@example.invalid')
    (repository / 'change.py').write_text('original\n', encoding='utf-8')
    _git(repository, 'add', '.')
    _git(repository, 'commit', '-qm', 'base')
    base = _git(repository, 'rev-parse', 'HEAD')
    (repository / 'change.py').write_text('first\n', encoding='utf-8')
    _git(repository, 'commit', '-qam', 'first PR')
    first = _git(repository, 'rev-parse', 'HEAD')
    (repository / 'change.py').write_text('second\n', encoding='utf-8')
    _git(repository, 'commit', '-qam', 'second PR')
    second = _git(repository, 'rev-parse', 'HEAD')

    fixture = tmp_path / 'github fixture.json'
    fixture.write_text(
        json.dumps(
            {
                'local_repository': str(repository),
                'tasks': {
                    '1': {
                        'base': base,
                        'head': first,
                        'merged_at': '2026-01-02T00:00:00Z',
                        'title': 'First PR',
                        'body': '',
                        'issue_title': 'Implement first task',
                        'issue_body': 'Details',
                    },
                    '2': {
                        'base': first,
                        'head': second,
                        'merged_at': '2026-01-03T00:00:00Z',
                        'title': 'Second PR',
                        'body': '',
                        'issue_title': '',
                        'issue_body': 'Missing title',
                    },
                },
            }
        ),
        encoding='utf-8',
    )
    config = tmp_path / 'config with spaces.yaml'
    config.write_text(
        'schema_version: 2\ndataset: {name: smoke-basket, description: Offline Make smoke, maintainers: [smoke]}\n'
        'output_dir: ./output with spaces\ncache_dir: ./cache with spaces\n'
        'source: {provider: github, project: smoke/repo}\ncollect: {selection: change_ids, change_ids: [1, 2]}\n'
        'filter:\n  workers: 1\n  selection: {shuffle: false, limit: 2}\n'
        'generation: {source: issue, workers: 1}\n'
        'export: {include_golden_diff: true}\n',
        encoding='utf-8',
    )
    log = tmp_path / 'requests.log'
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(SITE)
    environment['BASKET_COLLECTOR_MAKE_FIXTURE'] = str(fixture)
    environment['BASKET_COLLECTOR_MAKE_LOG'] = str(log)
    return config, log, environment, tmp_path / 'output with spaces'


def _make(config: Path, environment: dict[str, str], target: str, *variables: str) -> subprocess.CompletedProcess[str]:
    """Run a Make target with the fake GitHub subprocess environment."""
    return subprocess.run(
        ['make', target, f'CONFIG={config}', *variables],
        cwd=PROJECT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
        check=False,
    )


def _make_terminal(config: Path, environment: dict[str, str], target: str, *variables: str) -> tuple[int, str]:
    """Capture a real terminal invocation while always reaping its owned process."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        ['make', target, f'CONFIG={config}', *variables],
        cwd=PROJECT,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    transcript = bytearray()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 16384)
                except OSError:
                    break
                if not data:
                    break
                transcript.extend(data)
            if process.poll() is not None and not ready:
                break
        code = process.wait(timeout=3)
        return code, transcript.decode(errors='replace')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        os.close(master)


@pytest.mark.parametrize('mode,terminal', [('plain', False), ('off', False), ('auto', True), ('off', True)])
@pytest.mark.parametrize('all_rejected', [False, True])
def test_make_history_rejection_warning_and_empty_exit(
    make_fixture: tuple[Path, Path, dict[str, str], Path],
    mode: str,
    terminal: bool,
    all_rejected: bool,
) -> None:
    """Exercise native classifier through Make, including ERROR-level and terminal output."""
    config, log, environment, output = make_fixture
    fixture = Path(environment['BASKET_COLLECTOR_MAKE_FIXTURE'])
    data = json.loads(fixture.read_text())
    data['tasks']['1']['history_unavailable'] = True
    data['tasks']['2']['history_unavailable'] = all_rejected
    data['tasks']['2']['issue_title'] = 'Healthy second task'
    fixture.write_text(json.dumps(data), encoding='utf-8')
    if terminal:
        code, text = _make_terminal(config, environment, 'basket', 'LOG_LEVEL=ERROR', f'PROGRESS={mode}')
    else:
        result = _make(config, environment, 'basket', 'LOG_LEVEL=ERROR', f'PROGRESS={mode}')
        code, text = result.returncode, result.stdout + result.stderr
    assert code == (2 if all_rejected else 0), text
    assert 'pull_request smoke/repo #1 rejected: source_history_unavailable;' in text
    assert data['tasks']['1']['head'] in text and '.work/filter.json' in text
    requests = log.read_text().splitlines()
    saved_log = next(output.glob('logs_*/collector.log')).read_text()
    assert requests.count('history_unavailable:1') == 1
    if all_rejected:
        document = read_document(output / 'filtered.json', FilterDocument)
        assert document.meta.counts.rejected == 2 and document.meta.counts.pending == 0
        assert not (output / 'tasks.json').exists() and not (output / 'basket.json').exists()
    else:
        document = read_document(output / 'filtered.json', FilterDocument)
        assert document.meta.counts.rejected == 1 and document.meta.counts.succeeded == 1
        basket = read_document(output / 'basket.json', BasketDocument)
        assert [r.change_request.number for r in basket.tasks] == [2]
        assert basket.meta.partial is False
        assert 'source_history_unavailable=1' in text
        reused = _make(config, environment, 'basket', 'LOG_LEVEL=ERROR', 'PROGRESS=off')
        assert reused.returncode == 0, reused.stdout + reused.stderr
        assert 'rejected: source_history_unavailable;' not in reused.stdout + reused.stderr
        assert 'source_history_unavailable=1' in reused.stdout
        assert log.read_text().splitlines() == requests
    assert 'rejected: source_history_unavailable;' in saved_log


def test_installed_wheel_runs_basket_stages(
    make_fixture: tuple[Path, Path, dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Build an installed wheel and run every basket stage from it."""
    config, _, environment, output = make_fixture
    python = PROJECT / '.venv' / 'bin' / 'python'
    assert (PROJECT / 'requirements.lock').read_bytes() == (PROJECT / 'src' / 'requirements.lock').read_bytes()
    wheel_source = tmp_path / 'wheel source'
    wheel_source.mkdir()
    for name in ('pyproject.toml', 'README.md', 'requirements.lock'):
        shutil.copy2(PROJECT / name, wheel_source / name)
    shutil.copytree(
        PROJECT / 'src',
        wheel_source / 'src',
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
    )
    wheel_dir = tmp_path / 'wheels'
    wheel_dir.mkdir()
    built = subprocess.run(
        [
            str(python),
            '-m',
            'pip',
            'wheel',
            '--no-index',
            '--no-build-isolation',
            '--no-deps',
            '--wheel-dir',
            str(wheel_dir),
            str(wheel_source),
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(wheel_dir.glob('*.whl'))
    assert len(wheels) == 1
    installed_dir = tmp_path / 'installed'
    installed = subprocess.run(
        [
            str(python),
            '-m',
            'pip',
            'install',
            '--no-index',
            '--no-deps',
            '--target',
            str(installed_dir),
            str(wheels[0]),
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    environment['PYTHONPATH'] = os.pathsep.join((str(SITE), str(installed_dir)))
    location = subprocess.run(
        [str(python), '-c', 'import basket_collector; print(basket_collector.__file__)'],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert location.returncode == 0, location.stderr
    assert Path(location.stdout.strip()).is_relative_to(installed_dir)
    result = subprocess.run(
        [str(python), '-m', 'basket_collector', 'basket', '--config', str(config), '--allow-partial', '1'],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = read_document(output / 'collected.json', CollectDocument)
    filtered = read_document(output / 'filtered.json', FilterDocument)
    tasks = read_document(output / 'tasks.json', TasksDocument)
    basket = read_document(output / 'basket.json', BasketDocument)
    assert collected.meta.producer == filtered.meta.producer == tasks.meta.producer == basket.meta.producer
    assert collected.meta.producer == current_producer(PROJECT)
    assert filtered.meta.input is not None
    assert tasks.meta.input is not None
    assert basket.meta.input is not None
    assert filtered.meta.input.sha256 == stage_sha256(collected)
    assert tasks.meta.input.sha256 == stage_sha256(filtered)
    assert basket.meta.input.sha256 == stage_sha256(tasks)


def test_make_targets_basket_reuse_overwrite_and_retry(make_fixture: tuple[Path, Path, dict[str, str], Path]) -> None:
    """Check Make target reuse, overwrite, and retry behavior."""
    config, log, environment, output = make_fixture
    tasks_path = output / 'tasks.json'
    assert (PROJECT / '.venv' / 'bin' / 'python').exists(), 'run make setup before the smoke test'
    before = _make(config, environment, 'export')
    assert before.returncode != 0
    assert not (output / 'collected.json').exists()

    blocked = _make(config, environment, 'basket')
    assert blocked.returncode != 0 and 'ALLOW_PARTIAL=1' in blocked.stdout
    assert not (output / 'basket.json').exists()
    preserved_requests = log.read_bytes()
    first = _make(config, environment, '-j', 'basket', 'ALLOW_PARTIAL=1')
    assert log.read_bytes() == preserved_requests
    assert first.returncode == 0, first.stdout + first.stderr
    assert 'partial=true omissions=1' in first.stdout
    assert f'generation rejection reasons: {tasks_path}' in first.stdout
    assert 'does not repeat rejected records' in first.stdout
    assert tasks_path.is_file()
    assert [line.split(':', 1)[0] for line in first.stdout.splitlines() if ': completed;' in line] == [
        'collect',
        'filter',
        'generate',
        'export',
    ]
    collected = read_document(output / 'collected.json', CollectDocument)
    filtered = read_document(output / 'filtered.json', FilterDocument)
    tasks = read_document(output / 'tasks.json', TasksDocument)
    basket = read_document(output / 'basket.json', BasketDocument)
    assert collected.meta.counts.succeeded == 2
    assert filtered.meta.counts.succeeded == 2 and not filtered.rejected
    assert tasks.meta.counts.succeeded == 1 and tasks.meta.counts.rejected == 1 and tasks.meta.counts.failed == 0
    assert basket.meta.partial and [item.id for item in basket.meta.omissions] == [task_identity(2)]
    assert basket.tasks[0].golden_diff == tasks.tasks[0].changes.diff
    assert filtered.meta.input is not None
    assert tasks.meta.input is not None
    assert basket.meta.input is not None
    assert filtered.meta.input.sha256 == stage_sha256(collected)
    assert tasks.meta.input.sha256 == stage_sha256(filtered)
    assert basket.meta.input.sha256 == stage_sha256(tasks)
    calls = log.read_text(encoding='utf-8').splitlines()

    reused = _make(config, environment, 'basket')
    assert reused.returncode == 0, reused.stdout + reused.stderr
    assert 'partial=true omissions=1' in reused.stdout
    assert f'generation rejection reasons: {tasks_path}' in reused.stdout
    assert 'does not repeat rejected records' in reused.stdout
    assert reused.stdout.count('mode=reused') == 4
    assert log.read_text(encoding='utf-8').splitlines() == calls
    for stage in ('collect', 'filter', 'export'):
        result = _make(config, environment, stage)
        assert result.returncode == 0 and 'mode=reused' in result.stdout, result.stdout + result.stderr
        if stage == 'export':
            assert 'partial=true omissions=1' in result.stdout
            assert f'generation rejection reasons: {tasks_path}' in result.stdout
            assert 'repeat your command' not in result.stdout
            assert 'does not repeat rejected records' in result.stdout
    generate = _make(config, environment, 'generate')
    assert generate.returncode != 0 and 'mode=reused' in generate.stdout
    assert log.read_text(encoding='utf-8').splitlines() == calls

    retry = _make(config, environment, 'generate', 'RETRY_FAILED=1')
    assert retry.returncode != 0, retry.stdout + retry.stderr
    retried = read_document(output / 'tasks.json', TasksDocument)
    assert retried.tasks == tasks.tasks
    assert [item.id for item in retried.rejected] == [task_identity(2)] and not retried.failed
    assert log.read_text(encoding='utf-8').splitlines() == calls

    partial_retry = _make(config, environment, 'basket', 'RETRY_FAILED=1')
    assert partial_retry.returncode == 0, partial_retry.stdout + partial_retry.stderr
    assert 'partial=true omissions=1' in partial_retry.stdout
    assert read_document(output / 'tasks.json', TasksDocument).tasks == tasks.tasks
    assert log.read_text(encoding='utf-8').splitlines() == calls

    rebuilt = _make(config, environment, 'collect', 'OVERWRITE=1')
    assert rebuilt.returncode == 0 and 'mode=new' in rebuilt.stdout, rebuilt.stdout + rebuilt.stderr
    assert log.read_text(encoding='utf-8').splitlines().count('repository') == calls.count('repository') + 1


def test_make_pty_repeated_ctrl_c_drains_and_resumes(make_fixture: tuple[Path, Path, dict[str, str], Path]) -> None:
    """Drain repeated Ctrl+C signals and resume from the saved checkpoint."""
    config, log, environment, output = make_fixture
    initial = _make(config, environment, 'basket', 'ALLOW_PARTIAL=1')
    assert initial.returncode == 0, initial.stdout + initial.stderr
    environment = {**environment, 'BASKET_COLLECTOR_MAKE_DELAY_SECONDS': '2'}
    child, master = pty.fork()
    if child == 0:
        os.chdir(PROJECT)
        os.execvpe('make', ['make', 'filter', f'CONFIG={config}', 'OVERWRITE=1'], environment)
    transcript = bytearray()
    status: int | None = None
    try:
        deadline = time.monotonic() + 15
        while b'SMOKE_ACTIVE_OPERATION' not in transcript and time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    transcript.extend(os.read(master, 8192))
                except OSError:
                    break
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited:
                break
        assert b'SMOKE_ACTIVE_OPERATION' in transcript, transcript.decode('utf-8', errors='replace')
        os.write(master, b'\x03')
        time.sleep(0.1)
        os.write(master, b'\x03')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited:
                break
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    transcript.extend(os.read(master, 8192))
                except OSError:
                    pass
        else:
            pytest.fail('Make process did not drain within 15 seconds')
        assert status is not None and (
            (os.WIFEXITED(status) and os.WEXITSTATUS(status) != 0)
            or (os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGINT)
        ), transcript.decode('utf-8', errors='replace')
    finally:
        try:
            waited, _ = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            waited = child
        if not waited:
            os.killpg(child, signal.SIGTERM)
            os.waitpid(child, 0)
        os.close(master)

    checkpoint, state = read_checkpoint(output / '.work' / 'filter.json', FilterDocument)
    assert checkpoint.meta.status == 'interrupted'
    assert state['phase'] == 'local'
    assert checkpoint.meta.counts.succeeded == 0 and checkpoint.meta.counts.pending == 2
    assert read_document(output / 'filtered.json', FilterDocument).meta.status == 'completed'
    calls_before_resume = log.read_text(encoding='utf-8').splitlines()
    environment.pop('BASKET_COLLECTOR_MAKE_DELAY_SECONDS')
    resumed = _make(config, environment, 'filter')
    assert resumed.returncode == 0 and 'mode=resumed' in resumed.stdout, resumed.stdout + resumed.stderr
    calls_after_resume = log.read_text(encoding='utf-8').splitlines()
    assert calls_after_resume.count('issues:1') == calls_before_resume.count('issues:1') + 1
    assert read_document(output / 'filtered.json', FilterDocument).meta.counts.succeeded == 2


def test_make_basket_rejects_empty_selection_and_reuses_completed_stages(
    make_fixture: tuple[Path, Path, dict[str, str], Path],
) -> None:
    """Keep an empty filtered population explicit across a repeated CLI run."""
    config, log, environment, output = make_fixture
    text = config.read_text(encoding='utf-8')
    text = text.replace(
        'filter:\n  workers: 1\n',
        '''filter:\n  workers: 1\n  files: {enabled: true, allowed_path_patterns: ['^src/']}\n''',
    )
    config.write_text(text, encoding='utf-8')
    first = _make(config, environment, 'basket')
    assert first.returncode == 2, first.stdout + first.stderr
    assert 'selection is empty' in first.stdout
    assert not (output / 'basket.json').exists()
    collected = read_document(output / 'collected.json', CollectDocument)
    filtered = read_document(output / 'filtered.json', FilterDocument)
    assert collected.meta.status == 'completed'
    assert filtered.meta.status == 'completed' and filtered.meta.error is None
    assert len(collected.tasks) == len(filtered.rejected) == 2
    assert not filtered.tasks
    assert not (output / 'tasks.json').exists() and not (output / '.work/generate.json').exists()
    saved = {name: (output / name).read_bytes() for name in ('collected.json', 'filtered.json')}
    requests = log.read_bytes()
    repeated = _make(config, environment, 'basket')
    assert repeated.returncode == 2, repeated.stdout + repeated.stderr
    assert 'selection is empty' in repeated.stdout
    assert repeated.stdout.count('mode=reused') == 1
    assert not (output / 'basket.json').exists()
    assert log.read_bytes() == requests
    assert all((output / name).read_bytes() == content for name, content in saved.items())


@pytest.mark.parametrize('mode', ['auto', 'plain', 'off'])
def test_make_progress_modes_on_redirected_output(
    make_fixture: tuple[Path, Path, dict[str, str], Path], mode: str
) -> None:
    """Make passes the mode safely and leaves a partial result below the requested limit."""
    config, _log, environment, output = make_fixture
    result = _make(config, environment, 'basket', 'ALLOW_PARTIAL=1', f'PROGRESS={mode}')
    assert result.returncode == 0, result.stdout + result.stderr
    basket = read_document(output / 'basket.json', BasketDocument)
    assert len(basket.tasks) == 1 and basket.meta.partial
    assert '\x1b' not in result.stderr and '\r' not in result.stderr
    if mode == 'off':
        assert 'basket completed tasks=' not in result.stderr
    else:
        assert 'basket completed tasks=1/2' in result.stderr
        assert 'candidate_shortfall=0' in result.stderr and 'omissions=1' in result.stderr


def test_make_tty_progress_retains_incomplete_basket(
    make_fixture: tuple[Path, Path, dict[str, str], Path],
) -> None:
    """Exercise actual multiline panel rendering in a PTY over fully offline Git/API fixtures."""
    config, _log, environment, output = make_fixture
    master, slave = pty.openpty()
    process = subprocess.Popen(
        ['make', 'basket', f'CONFIG={config}', 'PROGRESS=auto', 'ALLOW_PARTIAL=1'],
        cwd=PROJECT,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    transcript = bytearray()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 16384)
                except OSError:
                    break
                if not data:
                    break
                transcript.extend(data)
            if process.poll() is not None and not ready:
                break
        assert process.wait(timeout=3) == 0, transcript.decode(errors='replace')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        os.close(master)
    text = transcript.decode(errors='replace')
    assert '\r' in text and 'basket' in text and '1/2' in text
    assert 'candidate_shortfall=0' in text and 'omissions=1' in text
    assert len(read_document(output / 'basket.json', BasketDocument).tasks) == 1
