"""Public runner, real Git/bubblewrap/native SQLite and host operations roundtrip offline."""

import json
import lzma
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import pytest
import yaml
from benchmark import coordinator
from benchmark.archive import create_archive, verify_archive
from benchmark.config import RunConfig, load_config
from benchmark.contracts import validate_document
from benchmark.git import GitCache
from benchmark.provisioning import Runtime
from benchmark.repricing import reprice
from benchmark.rescore import rescore
from benchmark.run_store import RunStore
from benchmark.runner import run
from benchmark.storage import bytes_sha256, canonical_bytes, read_json, write_json
from benchmark.triplet_paths import triplet_path
from benchmark.triplets import read_triplets
from test_basket import payload
from test_basket_v2 import payload as payload_v2
from test_pricing import policy
from test_workspace import git


@dataclass(frozen=True)
class Workflow:
    """Own local immutable inputs and the only replaced network provisioning adapter."""

    config: RunConfig
    dataset: Path
    runtime: Runtime
    pricing: Path


def _history(root: Path) -> tuple[GitCache, str, str]:
    """Create real source commits and prefill the exact HTTPS-keyed bare cache offline."""
    repository = root / 'repository'
    repository.mkdir()
    git(repository, 'init', '-q')
    (repository / 'src').mkdir()
    before_files = {
        'value.py': 'value = 1\n',
        'deleted.py': 'deleted = True\n',
        'empty.py': '',
        'shape.py': 'shape = 1\n',
        'naïve file.py': 'greeting = "Hello"\n',
    }
    for name, content in before_files.items():
        (repository / 'src' / name).write_text(content, encoding='utf-8')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'before-only source')
    before = git(repository, 'rev-parse', 'HEAD')
    (repository / 'src/value.py').write_text('value = 2\n')
    (repository / 'src/deleted.py').unlink()
    (repository / 'src/added.py').write_text('added = True\n')
    (repository / 'src/human-only.py').write_text('human_only = True\n')
    (repository / 'src/empty.py').chmod(0o755)
    (repository / 'src/shape.py').unlink()
    (repository / 'src/shape.py').mkdir()
    (repository / 'src/shape.py/leaf.py').write_text('leaf = 2\n')
    (repository / 'src/naïve file.py').write_text('greeting = "Привет"\n', encoding='utf-8')
    git(repository, 'add', '.')
    git(repository, 'commit', '-qm', 'human reference')
    after = git(repository, 'rev-parse', 'HEAD')
    cache = GitCache(root / 'cache/git', 'https://github.com/owner/repo.git')
    cache.path.parent.mkdir(parents=True)
    subprocess.run(['git', 'clone', '--bare', '-q', str(repository), str(cache.path)], check=True)
    return cache, before, after


def _basket(cache: GitCache, before: str, after: str) -> dict:
    """Bind every changed source blob/mode to real endpoint trees in a frozen basket."""
    data = payload()
    row = data['tasks'][0]
    trees = {role: cache.tree(revision) for role, revision in [('before', before), ('after', after)]}
    row['workspace'] = '.'
    row['revisions'].update(before=before, after=after)
    for role, revision in [('before', before), ('after', after)]:
        row['revisions'][role + '_tree'] = cache.run('rev-parse', revision + '^{tree}').decode().strip()
    row['provenance']['retrieval'].update({key: value for key, value in row['revisions'].items() if key != 'policy'})
    row['provenance']['source_revisions'].update(base_sha=before, head_sha=after)
    files = []
    for path in sorted(trees['before'].keys() | trees['after'].keys()):
        base, human = trees['before'].get(path), trees['after'].get(path)
        if base == human:
            continue
        item = {
            'path': path,
            'status': 'added' if base is None else ('deleted' if human is None else 'modified'),
            'additions': 0 if human is None else len(cache.blob(human).splitlines()),
            'deletions': 0 if base is None else len(cache.blob(base).splitlines()),
        }
        for role, entry in [('before', base), ('after', human)]:
            item[role + '_blob'], item[role + '_mode'] = (None, None) if entry is None else (entry.oid, entry.mode)
        files.append(item)
    row['changes']['files'] = files
    additions, deletions = (sum(item[key] for item in files) for key in ('additions', 'deletions'))
    row['changes']['stats'] = {
        'files_count': len(files),
        'additions': additions,
        'deletions': deletions,
        'changed_lines': additions + deletions,
        'patch_chars': 0,
    }
    cache.verify_reference(row)
    return data


def _inputs(root: Path, basket_version: int = 1) -> Workflow:
    """Freeze UTF-8 operator config, untouched compact basket bytes and synthetic tariff inputs."""
    cache, before, after = _history(root)
    dataset = root / 'basket.json'
    data = _basket(cache, before, after)
    if basket_version == 2:
        contract = payload_v2()
        data['meta'].pop('omitted_failed_ids')
        data['meta']['schema_version'] = 2
        for field in ('coverage', 'selection', 'omissions'):
            data['meta'][field] = contract['meta'][field]
        data['tasks'][0]['provenance']['generation']['input_sha256'] = contract['tasks'][0]['provenance']['generation'][
            'input_sha256'
        ]
        data['tasks'][0]['provenance']['generation']['reused_from'] = None
    dataset.write_bytes(canonical_bytes(data) + b'\n')
    executable = root / 'controlled-opencode'
    executable.write_bytes((Path(__file__).parent / 'fixtures/native-workflow.py').read_bytes())
    executable.chmod(0o755)
    native = {
        'model': 'provider/model',
        'provider': {'provider': {'models': {'model': {'options': {'reasoningEffort': 'medium'}}}}},
    }
    config_path = root / 'config.yaml'
    config_path.write_text(
        yaml.safe_dump(
            {
                'schema_version': 1,
                'workers': 1,
                'tags': ['model:user-label', 'Setup:Офлайн', 'Setup:Офлайн'],
                'agent': {
                    'name': 'Offline artifact workflow',
                    'runtime': 'opencode:v1.18.33',
                    'config': {'opencode_json': native},
                    'timeout_seconds': 10,
                },
                'provisioning': {'cache_dir': str(root / 'cache')},
                'sandbox': {'executable': '/usr/bin/bwrap', 'auto_install': False},
            },
            allow_unicode=True,
        ),
        encoding='utf-8',
    )
    runtime = Runtime(
        executable,
        {
            'actual_version': '1.18.33',
            'asset_name': 'offline-native-fixture.py',
            'platform': 'linux-x64-offline-fixture',
            'archive_sha256': bytes_sha256(executable.read_bytes()),
            'binary_sha256': bytes_sha256(executable.read_bytes()),
        },
    )
    tariffs = policy()
    tariffs['models'][0].update(provider='provider', model='model', long_context=None)
    pricing = root / 'pricing.yaml'
    pricing.write_text(yaml.safe_dump(tariffs))
    return Workflow(load_config(config_path), dataset, runtime, pricing)


def _serialization(directory: Path, frozen_basket: bytes) -> None:
    """Check complete JSON serialization separately from frozen inputs and compact JSONL."""
    assert (directory / 'basket.json').read_bytes() == frozen_basket
    for path in directory.rglob('*.json'):
        if path == directory / 'basket.json':
            continue
        content = path.read_bytes()
        data = json.loads(content)
        assert content == (json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8'), path
        assert b'\r\n' not in content and content.endswith(b'\n')
    events = list(directory.rglob('*.jsonl'))
    assert events
    for path in events:
        for line in path.read_text(encoding='utf-8').splitlines():
            assert line == json.dumps(json.loads(line), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    assert not list(directory.glob('*.html')) and not list(directory.glob('*.md'))
    assert not (directory / 'checkpoint.json').exists()


def _roles(directory: Path, report: dict) -> None:
    """Prove original paths/extensions and all role-pair availability without fabricating absent files."""
    row = report['tasks'][0]
    index_path = directory / row['artifacts']['triplets_manifest']
    bundle = read_triplets(index_path.parent, owner=row, run_id=report['run']['id'])
    entries = {entry['path']: entry for entry in bundle.index['entries']}
    assert {'src/value.py', 'src/empty.py', 'src/shape.py', 'src/shape.py/leaf.py'} <= entries.keys()
    for path, entry in entries.items():
        parents = {Path(value['path']).parent for value in entry['files'].values() if value is not None}
        assert len(parents) <= 1
        for role in ('base', 'human', 'ai'):
            receipt = entry['files'][role]
            if entry['exists'][role]:
                assert receipt['path'] == triplet_path(entry['number'], path, role)
                content = (index_path.parent / receipt['path']).read_bytes()
                assert receipt['size_bytes'] == len(content) and receipt['sha256'] == bytes_sha256(content)
            else:
                assert receipt is None and entry['modes'][role] is None
    for first, second in [('base', 'human'), ('base', 'ai'), ('human', 'ai')]:
        assert entries['src/value.py']['exists'][first] and entries['src/value.py']['exists'][second]
        assert entries['src/empty.py']['files'][first]['size_bytes'] == 0
        assert entries['src/empty.py']['files'][second]['size_bytes'] == 0
    assert entries['src/deleted.py']['exists'] == {'base': True, 'human': False, 'ai': False}
    assert entries['src/added.py']['exists'] == {'base': False, 'human': True, 'ai': True}
    assert entries['src/human-only.py']['exists'] == {'base': False, 'human': True, 'ai': False}
    assert entries['src/agent-only.py']['exists'] == {'base': False, 'human': False, 'ai': True}
    assert entries['src/shape.py']['exists'] == {'base': True, 'human': False, 'ai': False}
    assert entries['src/shape.py/leaf.py']['exists'] == {'base': False, 'human': True, 'ai': True}


def _export(directory: Path, archive: Path) -> None:
    """Export actual producer evidence only when the bounded fixture output is explicitly requested."""
    destination = os.environ.get('PRSIM_ARTIFACT_FIXTURE_DIR')
    if destination is None:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    saved = root / directory.name
    shutil.copytree(directory, saved)
    shutil.copyfile(archive, root / 'producer.tar.gz')
    for name in ('report.json', 'basket.json', 'config_identity.json', 'manifest.json', 'run.json'):
        shutil.copyfile(directory / name, root / name)
    report = read_json(directory / 'report.json')
    write_json(
        root / 'current.json',
        {
            'run_directory': saved.name,
            'archive': 'producer.tar.gz',
            'run_id': report['run']['id'],
            'adapter': 'provider-free executable fixture',
            'sandbox': read_json(directory / '_meta/sandbox.json'),
            'native_attempt': report['tasks'][0]['final_attempt'],
            'role_pairs': ['base-human', 'base-ai', 'human-ai'],
            'source_cases': [
                'modified',
                'added',
                'deleted',
                'human-only',
                'agent-only',
                'existing-empty',
                'file-to-directory',
                'unicode-space-path',
            ],
        },
    )


@pytest.mark.parametrize('basket_version', [1, 2])
def test_public_runner_native_artifact_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, basket_version: int
) -> None:
    """Execute the public runner and every host operation using real current producer artifacts."""
    inputs = _inputs(tmp_path, basket_version)
    frozen = inputs.dataset.read_bytes()
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: inputs.runtime)
    code, directory = run(inputs.config, inputs.dataset, output=tmp_path / 'runs', progress='off')
    assert code == 0
    report = read_json(directory / 'report.json')
    validate_document('report', report)
    assert re.fullmatch(r'\d{8}T\d{6}Z-offline-artifact-workflow-[0-9a-f]{8}', directory.name)
    stamp = datetime.fromisoformat(report['run']['created_at']).strftime('%Y%m%dT%H%M%SZ')
    assert directory.name.startswith(stamp + '-offline-artifact-workflow-')
    assert report['run']['tags'] == inputs.config.data['tags']
    assert report['inference']['configured']['effort']['value'] == 'medium'
    assert report['inference']['effective']['model']['value'] == 'provider/model'
    row = report['tasks'][0]
    assert row['execution']['status'] == 'completed' and row['scoring']['status'] == 'available'
    assert row['progress']['phase'] == 'completed'
    assert datetime.fromisoformat(row['progress']['updated_at']) >= datetime.fromisoformat(
        row['final_attempt']['finished_at']
    )
    original_progress = row['progress'].copy()
    assert row['telemetry']['tokens']['total'] == 16 and row['telemetry']['cost']['amount'] == 0.5
    _roles(directory, report)
    outcome = directory / row['artifacts']['outcome']
    original_outcome, original_attempt = outcome.read_bytes(), row['final_attempt'].copy()
    seal = directory / Path(row['artifacts']['outcome']).with_name('seal.json')
    original_seal = seal.read_bytes()
    assert read_json(seal)['native_timing']['finished_at'] == original_attempt['finished_at']
    assert 'Offline fixture: Завершено' in (directory / row['artifacts']['agent_log']).read_text(encoding='utf-8')
    assert b'OFFLINE_PRIVATE_SENTINEL' not in lzma.decompress(
        (directory / row['telemetry']['session_db']['path']).read_bytes()
    )
    human_log = (directory / 'run.log').read_text(encoding='utf-8')
    assert 'Task 001/1 · attempt 1' in human_log
    assert 'Final: processed 1/1; tasks complete 1/1; native completed 1' in human_log
    assert 'Final states: {' not in human_log
    for log in directory.rglob('*.log'):
        assert '"sessionID"' not in log.read_text(encoding='utf-8')
    moved = tmp_path / 'relocated' / directory.name
    moved.parent.mkdir()
    directory.rename(moved)
    (moved / 'report.json').unlink()

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        """Any provisioning or native replay after a completed move is a regression."""
        raise AssertionError('completed resume must use saved native facts')

    monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    assert run(inputs.config, inputs.dataset, resume=moved, progress='off')[0] == 0
    assert rescore(moved, level='ERROR') == 0
    assert reprice(moved, inputs.pricing, level='ERROR') == 0
    current = read_json(moved / 'report.json')
    assert current['tasks'][0]['final_attempt'] == original_attempt
    assert current['tasks'][0]['progress'] == original_progress
    resumed_log = (moved / 'run.log').read_text(encoding='utf-8')
    assert 'Resume plan: reused 1; host-only recovery 0; new attempts 0; blocked 0.' in resumed_log
    assert 'All-attempt native reported: known subtotal USD 0.5' in resumed_log
    assert (moved / row['artifacts']['outcome']).read_bytes() == original_outcome
    assert (moved / seal.relative_to(directory)).read_bytes() == original_seal
    assert current['last_rescore'] is not None and current['last_reprice'] is not None
    assert len(list(moved.glob('tasks/*/attempts/*/start.json'))) == 1
    assert current['tasks'][0]['telemetry']['api_equivalent']['amount'] > 0
    archive = create_archive(moved, tmp_path / 'producer.tar.gz')
    assert verify_archive(archive.path)['complete']
    _serialization(moved, frozen)
    _export(moved, archive.path)


def _process_identity(pid: int) -> tuple[str, int] | None:
    """Inspect only an owned PID's state/start identity without reading process environments."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except OSError:
        return None
    return fields[0], int(fields[19])


def _living(descendants: list[dict]) -> list[int]:
    """Return still-running descendants while protecting against PID reuse and zombies."""
    result = []
    for item in descendants:
        identity = _process_identity(item['pid'])
        if identity is not None and identity[1] == item['start_time'] and identity[0] != 'Z':
            result.append(item['pid'])
    return result


def _kill_owned(process: subprocess.Popen, descendants: list[dict]) -> None:
    """SIGKILL the owned driver group and require namespaced descendants to die with their parent."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while _living(descendants) and time.monotonic() < deadline:
        time.sleep(0.02)
    remaining = _living(descendants)
    try:
        assert not remaining, f'bubblewrap descendants survived parent SIGKILL: {remaining}'
    finally:
        for pid in _living(descendants):
            os.kill(pid, signal.SIGKILL)


def _crash(root: Path, boundary: str) -> tuple[Path, dict]:
    """Pause a real subprocess at one durable boundary, kill it and retain exact evidence."""
    driver = Path(__file__).parent / 'fixtures/artifact-crash-driver.py'
    environment = os.environ.copy()
    environment['PRSIM_CRASH_NATIVE'] = 'hold-native-process-for-crash-test'
    paused = root / 'paused.json'
    with (root / 'driver.log').open('wb') as log:
        process = subprocess.Popen(
            [sys.executable, str(driver), str(root), boundary],
            cwd=Path(__file__).parents[1],
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        notice = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not paused.is_file() and process.poll() is None:
                time.sleep(0.02)
            assert paused.is_file(), (root / 'driver.log').read_text(encoding='utf-8')
            notice = read_json(paused)
            assert notice['boundary'] == boundary and notice['pid'] == process.pid
        finally:
            _kill_owned(process, [] if notice is None else notice['descendants'])
        assert process.returncode == -signal.SIGKILL
    assert notice is not None
    return Path(notice['run_directory']), notice


@pytest.mark.parametrize('basket_version', [1, 2])
@pytest.mark.parametrize('boundary', ['reserved', 'native', 'seal', 'outcome', 'state'])
def test_sigkill_recovers_only_state_owned_native_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, basket_version: int
) -> None:
    """Real process crashes never replay a complete seal/outcome or double-account saved usage."""
    inputs = _inputs(tmp_path, basket_version)
    config = inputs.config
    if boundary == 'native':
        path = tmp_path / 'config.yaml'
        data = yaml.safe_load(path.read_text())
        data['agent']['credentials'] = {'OFFLINE_HOLD': 'PRSIM_CRASH_NATIVE'}
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
        config = load_config(path)
        monkeypatch.setenv('PRSIM_CRASH_NATIVE', 'continue-native-process-after-crash-test')
    directory, notice = _crash(tmp_path, boundary)
    if boundary == 'native':
        assert notice['descendants'], 'live-native boundary must observe the actual sandbox child'
    first = next(directory.glob('tasks/*/attempts/001'))
    start = (first / 'start.json').read_bytes()
    initial = read_json(first / 'start.json')['attempt']
    original_seal = (first / 'seal.json').read_bytes() if (first / 'seal.json').exists() else None
    original_outcome = (first / 'outcome.json').read_bytes() if (first / 'outcome.json').exists() else None
    selected = read_json(directory / '_meta/state.json')['tasks'][0]
    assert selected['attempt']['id'] == initial['id'] and selected['next_sequence'] == 2
    if boundary == 'state':
        assert selected['processing_status'] == 'completed'
        assert selected['progress']['phase'] == 'completed'
        assert read_json(directory / 'report.json')['tasks'][0]['processing_status'] != 'completed'
    if boundary in ('reserved', 'native'):
        assert original_seal is None and original_outcome is None
        snapshot = RunStore.open(directory).snapshot()['tasks'][0]
        assert snapshot['scoring']['status'] == 'pending' and snapshot['telemetry']['tokens']['total'] is None
        monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: inputs.runtime)
    else:
        assert original_seal is not None
        assert selected['outcome'] is not None if boundary == 'state' else selected['outcome'] is None

        def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
            """Reject native replay when complete sealed evidence permits host recovery."""
            raise AssertionError('sealed crash recovery must not provision native runtime')

        monkeypatch.setattr(coordinator, 'provision_opencode', forbidden)
    assert run(config, inputs.dataset, resume=directory, progress='off')[0] == 0
    report = RunStore.open(directory).snapshot()
    row = report['tasks'][0]
    expected_sequence = 2 if boundary in ('reserved', 'native') else 1
    assert row['final_attempt']['sequence'] == expected_sequence
    assert len(list(directory.glob('tasks/*/attempts/*/start.json'))) == expected_sequence
    assert (first / 'start.json').read_bytes() == start
    if original_seal is not None:
        assert (first / 'seal.json').read_bytes() == original_seal
        native = json.loads(original_seal)
        assert row['final_attempt'] == native['task']['final_attempt']
    else:
        assert row['final_attempt']['id'] != initial['id']
        assert read_json(directory / '_meta/state.json')['tasks'][0]['history'][0]['attempt']['id'] == initial['id']
    if original_outcome is not None:
        assert (first / 'outcome.json').read_bytes() == original_outcome
    elif original_seal is not None:
        assert not (first / 'outcome.json').exists(), 'host recovery must not invent a native outcome'
    assert row['telemetry']['tokens']['total'] == 16
    native_cost = report['costs']['all_attempts']['native_reported']
    assert native_cost['known_subtotal'] == '0.5' and native_cost['coverage']['known_requests'] == 1
    assert len(list(directory.glob('tasks/*/attempts/*/telemetry/usage.json'))) == 1
    if boundary in ('reserved', 'native'):
        assert native_cost['amount'] is None and native_cost['coverage']['total_attempts'] == 2
        assert read_json(directory / '_meta/state.json')['tasks'][0]['history'][0]['seal'] is None
    else:
        assert native_cost['amount'] == '0.5' and native_cost['coverage']['total_attempts'] == 1
