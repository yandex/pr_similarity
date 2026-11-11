"""Resource snapshots are explicit, bounded, optional and administrator-only."""

import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber
from starlette.testclient import TestClient
from test_local_service import service
from test_service import publish
from viewer import resources
from viewer.config import Config
from viewer.errors import ViewerError
from viewer.persistence.storage import MemoryStore, S3Store
from viewer.resources import ResourceSnapshots, local_resources
from viewer.web.api import create_app


def wait_ready(snapshots: ResourceSnapshots) -> dict:
    """Wait for an inventory to leave collecting without starting another scan."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = snapshots.current()
        if value.get('object_storage', {}).get('status') != 'collecting':
            return value
        time.sleep(0.01)
    raise AssertionError('Resource inventory did not finish')


def test_mock_snapshot_omits_s3_without_calling_storage(tmp_path: Path) -> None:
    """Omit S3 usage for mock storage without invoking its unsupported inventory."""

    class NoInventory(MemoryStore):
        """Reject any attempt to enumerate a mock backend."""

        def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
            """Yield fixture metadata or exercise the configured inventory failure."""
            raise AssertionError('Mock must not request an S3 inventory')

    svc = service(tmp_path)
    snapshots = ResourceSnapshots(svc.config, NoInventory())
    try:
        result = snapshots.request(local_resources(svc))
        assert result['object_storage'] is None
        assert result['cache']['limit_bytes'] == svc.config.cache_max_bytes
        assert result['disk']['total_bytes'] >= result['disk']['used_bytes']
    finally:
        assert snapshots.close()
        svc.close()
        svc.db.close()


def test_s3_request_coalesces_and_failure_keeps_last_complete_snapshot(tmp_path: Path) -> None:
    """Coalesce concurrent requests and preserve the last complete total after failure."""

    class Inventory(MemoryStore):
        """Provide controlled object metadata for the resource snapshot contract."""

        calls = 0
        block = threading.Event()
        fail = False

        def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
            """Yield fixture metadata or exercise the configured inventory failure."""
            self.calls += 1
            assert self.block.wait(2)
            yield 'bundle-data/v1/run/archive.tar.gz', 15
            if self.fail:
                raise OSError('private provider detail')
            yield 'trash/old/archive.tar.gz', 5

    store = Inventory()
    config = Config(runtime=tmp_path, storage='s3', bucket='test', storage_stats_max_objects=10)
    snapshots = ResourceSnapshots(config, store)
    try:
        first = snapshots.request({'sampled_at': 'first'})
        assert first['object_storage']['status'] == 'collecting'
        snapshots.request({'sampled_at': 'second'})
        store.block.set()
        result = wait_ready(snapshots)
        assert store.calls == 1
        assert result['object_storage']['used_bytes'] == 20
        assert result['object_storage']['object_count'] == 2
        assert result['object_storage']['capacity_bytes'] is None
        previous_time = result['object_storage']['sampled_at']
        snapshots.current()
        assert store.calls == 1
        store.fail = True
        snapshots.request({'sampled_at': 'next'})
        failed = wait_ready(snapshots)['object_storage']
        assert failed['status'] == 'unavailable'
        assert failed['used_bytes'] == 20 and failed['sampled_at'] == previous_time
        assert 'private provider detail' not in str(failed)
    finally:
        store.block.set()
        assert snapshots.close()


@pytest.mark.parametrize('limit', [1, True, 0])
def test_inventory_limits_do_not_claim_partial_totals(tmp_path: Path, limit: int) -> None:
    """Reject invalid bounds and discard totals from an incomplete inventory."""
    if type(limit) is bool or limit == 0:
        with pytest.raises(ViewerError):
            Config(storage='mock', storage_stats_max_objects=limit)
        return

    class Inventory(MemoryStore):
        """Provide controlled object metadata for the resource snapshot contract."""

        def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
            """Yield fixture metadata or exercise the configured inventory failure."""
            yield 'runs/one', 5
            yield 'runs/two', 7

    snapshots = ResourceSnapshots(
        Config(runtime=tmp_path, storage='s3', bucket='test', storage_stats_max_objects=limit), Inventory()
    )
    try:
        snapshots.request({})
        result = wait_ready(snapshots)['object_storage']
        assert result['status'] == 'unavailable'
        assert result['used_bytes'] is None and result['object_count'] is None
    finally:
        assert snapshots.close()


def test_resource_endpoint_requires_admin_and_mock_hides_storage(tmp_path: Path) -> None:
    """Authorize resource snapshots before returning the mock-aware response."""
    token = tmp_path / 'token'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock', publication_storage='local')
    with TestClient(create_app(config, MemoryStore())) as client:
        assert client.get('/api/v1/admin/resources?refresh=1').status_code == 401
        response = client.get('/api/v1/admin/resources?refresh=1', headers={'Authorization': 'Bearer ' + 'A' * 32})
        assert response.status_code == 200
        assert response.json()['object_storage'] is None


def test_cache_counts_shared_paths_once_and_omits_evicted_paths(tmp_path: Path) -> None:
    """Count a resident cache path once across revisions and exclude evicted paths."""
    svc = service(tmp_path)
    try:
        report, _, _ = publish(svc, tmp_path)
        rid = report['run']['id']
        original = local_resources(svc)['cache']
        svc.patch_metadata(rid, 'alice', {'display_name': 'Renamed'})
        assert local_resources(svc)['cache'] == original
        assert original['used_bytes'] > 0 and original['resident_versions'] == 1
        record = svc.repository.get(rid)
        assert record is not None and record.local_path is not None
        shutil.rmtree(record.local_path)
        assert local_resources(svc)['cache']['used_bytes'] == 0
    finally:
        svc.close()
        svc.db.close()


def test_inventory_close_and_timeout_discard_partial_sums(tmp_path: Path) -> None:
    """Cancel a blocked inventory and reject requests after shutdown begins."""

    class Inventory(MemoryStore):
        """Provide controlled object metadata for the resource snapshot contract."""

        entered = threading.Event()
        release = threading.Event()

        def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
            """Yield fixture metadata or exercise the configured inventory failure."""
            self.entered.set()
            self.release.wait(3)
            assert check is not None
            check()
            yield 'runs/one', 5

    store = Inventory()
    snapshots = ResourceSnapshots(Config(runtime=tmp_path, storage='s3', bucket='test'), store)
    snapshots.request({})
    assert store.entered.wait(2)
    assert not snapshots.close(timeout=0)
    store.release.set()
    assert snapshots.close(timeout=2)
    assert snapshots.current()['object_storage']['used_bytes'] is None
    with pytest.raises(ViewerError, match='stopping'):
        snapshots.request({})


def test_s3_inventory_lists_sizes_in_prefix_and_checks_between_pages() -> None:
    """Enumerate only prefix-scoped metadata and check cancellation between pages."""
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='mock', aws_secret_access_key='mock')
    store = S3Store(Config(bucket='bucket', prefix='viewer'), client)
    with Stubber(client) as stub:
        stub.add_response(
            'list_objects_v2',
            {'IsTruncated': True, 'NextContinuationToken': 'next', 'Contents': [{'Key': 'viewer/runs/a', 'Size': 7}]},
            {'Bucket': 'bucket', 'Prefix': 'viewer/'},
        )
        stub.add_response(
            'list_objects_v2',
            {'IsTruncated': False, 'Contents': [{'Key': 'viewer/trash/a', 'Size': 3}]},
            {'Bucket': 'bucket', 'Prefix': 'viewer/', 'ContinuationToken': 'next'},
        )
        checks = []
        assert list(store.iter_sizes(check=lambda: checks.append(1))) == [('runs/a', 7), ('trash/a', 3)]
        assert len(checks) == 2
        stub.assert_no_pending_responses()


def test_inventory_deadline_discards_partial_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a timed-out inventory from publishing a partial usage total."""

    class Inventory(MemoryStore):
        """Provide controlled object metadata for the resource snapshot contract."""

        def iter_sizes(self, *, check: Callable[[], None] | None = None) -> Iterator[tuple[str, int]]:
            """Yield fixture metadata or exercise the configured inventory failure."""
            yield 'runs/one', 5

    ticks = iter((0, 61))
    monkeypatch.setattr(resources, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    snapshots = ResourceSnapshots(Config(runtime=tmp_path, storage='s3', bucket='test'), Inventory())
    try:
        snapshots.request({})
        value = wait_ready(snapshots)['object_storage']
        assert value['error_code'] == 'storage_stats_timeout'
        assert value['used_bytes'] is None
    finally:
        assert snapshots.close()
