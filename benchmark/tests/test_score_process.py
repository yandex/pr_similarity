"""A real score child matches fixed MLP and is terminated under cancellation."""

import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from benchmark.errors import BenchmarkError
from benchmark.runner import Stop
from benchmark.score_process import score_saved
from benchmark.scoring import score_pr
from benchmark.storage import atomic_write, bytes_sha256, write_json
from benchmark.triplet_paths import triplet_path
from benchmark.triplets import load_triplets


def triplets(tmp_path: Path) -> Path:
    """Save a complete three-state fixture with content digests and identities."""
    directory = tmp_path / 'triplets'
    directory.mkdir()
    entry = {'path': 'value.py', 'number': 1, 'modes': {}, 'exists': {}, 'files': {}}
    for role, text in [('base', 'value = 1\n'), ('human', 'value = 2\n'), ('ai', 'value = 2\n')]:
        content = text.encode()
        path = triplet_path(1, 'value.py', role)
        atomic_write(directory / path, content)
        entry['modes'][role] = '100644'
        entry['exists'][role] = True
        entry['files'][role] = {'path': path, 'size_bytes': len(content), 'sha256': bytes_sha256(content)}
    write_json(
        directory / 'index.json',
        {
            'version': 1,
            'run_id': None,
            'task_id': 'owner/repo#1',
            'attempt_id': 'fixture',
            'source': {'before': 'a' * 40, 'after': 'b' * 40},
            'entries': [entry],
        },
    )
    return directory


def test_score_child_matches_direct_scorer(tmp_path: Path) -> None:
    """Verify score child matches direct scorer."""
    directory = triplets(tmp_path)
    result = score_saved(directory, 'triplets', tmp_path / 'scoring.log', threading.Event())
    assert result['scoring'] == score_pr(load_triplets(directory))
    assert result['diagnostics'] == []
    assert '"scoring":' not in (tmp_path / 'scoring.log').read_text()


def test_invalid_machine_stdout_retains_redacted_diagnostics(tmp_path: Path) -> None:
    """Removing valid result duplicates must not erase malformed-child evidence."""
    log = tmp_path / 'scoring.log'
    with pytest.raises(BenchmarkError):
        score_saved(
            triplets(tmp_path), 'triplets', log, threading.Event(),
            secrets=['PRIVATE_VALUE'],
            argv=[sys.executable, '-c', 'print("PRIVATE_VALUE not-json")'],
        )
    assert '[invalid scoring stdout]' in log.read_text()
    assert '<redacted> not-json' in log.read_text()
    assert 'PRIVATE_VALUE' not in log.read_text()


def test_score_child_cancellation_is_bounded(tmp_path: Path) -> None:
    """Verify score child cancellation is bounded."""
    directory = triplets(tmp_path)
    stop = Stop()
    timer = threading.Timer(0.3, lambda: stop.signal(signal.SIGINT, None))
    timer.start()

    started = time.monotonic()
    try:
        with pytest.raises(BenchmarkError) as caught:
            score_saved(
                directory,
                'triplets',
                tmp_path / 'scoring.log',
                stop,
                grace=0.1,
                argv=[sys.executable, '-c', 'import time; time.sleep(30)'],
            )
        assert caught.value.code == 'operation_cancelled'
        assert time.monotonic() - started < 2
    finally:
        timer.join()
    assert (directory / 'index.json').exists()
