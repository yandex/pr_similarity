"""Bounded catalogue load with synthetic identities sharing one validated projection."""

import json
import resource
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_service import publish
from viewer.config import Config
from viewer.local_service import LocalViewerService
from viewer.persistence.storage import MemoryStore
from viewer.web.api import create_app


@pytest.mark.parametrize('count', [1000, 10000])
@pytest.mark.parametrize('concurrency', [30, 100])
def test_catalogue_readers_do_not_access_storage(tmp_path: Path, count: int, concurrency: int) -> None:
    """Measure concurrent local catalogue reads without introducing remote I/O."""

    class CountStore(MemoryStore):
        """Count remote reads while synthetic catalogue load runs."""

        reads = 0

        def get(self, key: str, limit: int = 64 << 20) -> bytes:
            """Count remote object reads before delegating to the in-memory store."""
            self.reads += 1
            return super().get(key, limit)

        def list(self, prefix: str = '') -> Iterator[str]:
            """Reject storage enumeration before the local-mode guard runs."""
            self.reads += 1
            return super().list(prefix)

    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    remote = CountStore()
    app = create_app(
        Config(
            runtime=tmp_path / 'runtime',
            admin_token_file=token,
            storage='mock',
            publication_storage='local',
            disk_reserve_bytes=0,
        ),
        remote,
    )
    service = app.state.service
    assert isinstance(service, LocalViewerService)
    service.db.create_user('alice', 'secret')
    report, _, _ = publish(service, tmp_path)
    original = service.snapshot
    rid = report['run']['id']
    with TestClient(app) as client:
        rows = {str(uuid.UUID(int=i + 1)): original.runs[rid] for i in range(count)}
        service.snapshot = replace(
            original, runs=rows, metadata={key: original.metadata[rid] for key in rows}, revision=count
        )
        before = remote.reads
        started = time.perf_counter()
        response = client.get('/api/v1/baskets')
        cold = time.perf_counter() - started
        assert response.status_code == 200 and response.json()['total_runs'] == count

        def request(_: int) -> tuple[float, int]:
            """Measure one catalogue response under concurrent client load."""
            started = time.perf_counter()
            response = client.get('/api/v1/baskets')
            return (time.perf_counter() - started) * 1000, response.status_code

        started, cpu = time.perf_counter(), time.process_time()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples = list(pool.map(request, range(200)))
        elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu
        times = sorted(value for value, _ in samples)
        assert all(status == 200 for _, status in samples)
        assert remote.reads == before
        print(
            json.dumps(
                dict(
                    runs=count,
                    clients=concurrency,
                    requests=200,
                    cold_ms=round(cold * 1000, 2),
                    p50_ms=round(times[99], 2),
                    p95_ms=round(times[189], 2),
                    p99_ms=round(times[197], 2),
                    errors=0,
                    s3_reads=0,
                    wall_seconds=round(elapsed, 3),
                    cpu_seconds=round(cpu, 3),
                    max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                )
            )
        )
