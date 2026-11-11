"""Archive bundles remain closed, immutable and independent of file count."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock

import pytest
from local_test_support import BundleFixture, build_bundle_fixture
from viewer.bundles import REGISTRY_HEAD, RESERVATIONS, BundleIdentity, BundleRepository
from viewer.errors import StorageError, ViewerError


@pytest.fixture
def bundle(tmp_path: Path) -> BundleFixture:
    """Build a closed-registry repository with validated archive inputs."""
    return build_bundle_fixture(tmp_path)


def test_roundtrip_exact_bytes_and_metadata_only(bundle: BundleFixture, tmp_path: Path) -> None:
    """Restoration preserves original bytes and metadata revisions reuse the archive."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    first = repo.publish_bundle(identity, imported, marker, metadata)
    assert len([key for key in store.objects if not key.startswith('bundle-registry/')]) == 3
    restored = repo.download_bundle(first, tmp_path / 'restored')
    assert restored.imported.archive.read_bytes() == imported.archive.read_bytes()
    assert restored.marker == marker
    assert restored.metadata == metadata
    store.writes.clear()
    second = repo.publish_bundle(
        replace(identity, metadata_revision=2),
        imported,
        marker,
        dict(metadata, display_name='Renamed'),
    )
    assert len([key for key in store.writes if not key.startswith('bundle-registry/')]) == 2
    assert repo.discover() == [second]


def test_incomplete_upload_is_not_discovered(bundle: BundleFixture) -> None:
    """An upload without its closing manifest remains absent from discovery."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    store.fail = 'metadata/'
    with pytest.raises(StorageError):
        repo.publish_bundle(identity, imported, marker, metadata)
    assert repo.discover() == []


def test_archive_corruption_is_rejected(bundle: BundleFixture, tmp_path: Path) -> None:
    """Corrupt archive bytes cannot become a restored local publication."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    descriptor = repo.publish_bundle(identity, imported, marker, metadata)
    key = next(key for key in store.objects if key.endswith('archive.tar.gz'))
    store.objects[key] = b'corrupted'
    with pytest.raises(ViewerError):
        repo.download_bundle(descriptor, tmp_path / 'bad')
    assert not (tmp_path / 'bad').exists()


def test_conflict_and_uncertain_put_reply(bundle: BundleFixture) -> None:
    """Readback resolves lost replies while conflicting immutable writes fail."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    store.lost_reply = True
    descriptor = repo.publish_bundle(identity, imported, marker, metadata)
    assert repo.discover() == [descriptor]
    with pytest.raises(ViewerError, match='conflict'):
        repo.publish_bundle(identity, imported, marker, dict(metadata, display_name='conflict'))


def test_tombstone_wins_over_older_generation(bundle: BundleFixture) -> None:
    """Discovery selects the deletion generation over an earlier publication."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    repo.publish_bundle(identity, imported, marker, metadata)
    tombstone = repo.publish_tombstone(replace(identity, generation=2), metadata)
    assert repo.discover() == [tombstone]
    assert tombstone.tombstone


@pytest.mark.parametrize('owner', ['../alice', 'alice/x', '', 'alice\\x'])
def test_unsafe_owner_rejected(owner: str) -> None:
    """Unsafe owner names cannot form storage keys."""
    with pytest.raises(ViewerError):
        BundleIdentity(owner, '00000000-0000-0000-0000-000000000000', 1, 'a' * 64)


def test_evicted_metadata_update_and_exact_read(bundle: BundleFixture) -> None:
    """Metadata updates can reuse a verified remote archive after local eviction."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    repo.publish_bundle(identity, imported, marker, metadata)
    store.writes.clear()
    revision = replace(identity, metadata_revision=2)
    descriptor = repo.publish_bundle(revision, None, marker, dict(metadata, tags=['a', 'a']))
    assert len([key for key in store.writes if not key.startswith('bundle-registry/')]) == 2
    assert repo.read_bundle(revision) == descriptor
    assert descriptor.from_wire(descriptor.to_wire()) == descriptor
    assert descriptor.manifest_key == identity.closing_key.replace('/1.json', '/2.json')
    with pytest.raises(ViewerError):
        repo.publish_bundle(replace(identity, generation=2), None, marker, metadata)


def test_missing_closure_cannot_be_downloaded(bundle: BundleFixture, tmp_path: Path) -> None:
    """A descriptor cannot restore data after its closing manifest disappears."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    descriptor = repo.publish_bundle(identity, imported, marker, metadata)
    del store.objects[descriptor.manifest_key]
    with pytest.raises(ViewerError):
        repo.download_bundle(descriptor, tmp_path / 'not-closed')
    assert not (tmp_path / 'not-closed').exists()


def test_discovery_limit_fails_without_partial_result(bundle: BundleFixture) -> None:
    """A registry exceeding the discovery budget cannot return a partial success."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    repo.publish_bundle(identity, imported, marker, metadata)
    repo.publish_tombstone(replace(identity, run_id='ffffffff-ffff-ffff-ffff-ffffffffffff'), metadata)
    with pytest.raises(ViewerError) as error:
        repo.discover(max_objects=1)
    assert error.value.code == 'bundle_discovery_limit'


def test_unknown_metadata_and_marker_fields_rejected(bundle: BundleFixture) -> None:
    """Unrecognized metadata and marker fields are rejected before publication."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    with pytest.raises(ViewerError):
        repo.publish_bundle(identity, imported, marker, dict(metadata, password='secret'))
    with pytest.raises(ViewerError):
        repo.publish_bundle(identity, imported, dict(marker, secret='secret'), metadata)
    assert all(key.startswith('bundle-registry/') for key in store.objects)


def test_metadata_corruption_is_not_readable(bundle: BundleFixture) -> None:
    """A changed metadata object fails the closing manifest digest check."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    descriptor = repo.publish_bundle(identity, imported, marker, metadata)
    store.objects[identity.metadata_key] += b' '
    with pytest.raises(ViewerError) as error:
        repo.verify_bundle(descriptor)
    assert error.value.code == 'stored_digest_mismatch'


def test_new_generation_and_partial_list_failure(bundle: BundleFixture) -> None:
    """Registry discovery selects the newest generation without relying on LIST."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    repo.publish_bundle(identity, imported, marker, metadata)
    second = repo.publish_bundle(replace(identity, generation=2), imported, marker, metadata)
    assert repo.discover() == [second]

    def partial_list(prefix: str) -> Iterator[str]:
        """Simulate a listing interrupted after its first object."""
        yield identity.closing_key
        raise StorageError()

    store.list = partial_list
    assert repo.discover() == [second]


@pytest.mark.parametrize(
    'changes',
    [
        dict(run_id='../escape'),
        dict(generation=True),
        dict(generation=0),
        dict(metadata_revision=-1),
        dict(archive_sha256='not-a-digest'),
    ],
)
def test_malformed_identity_rejected(changes: dict[str, object]) -> None:
    """Malformed publication identities fail before any object path is constructed."""
    values = dict(
        owner='alice',
        run_id='00000000-0000-0000-0000-000000000000',
        generation=1,
        archive_sha256='a' * 64,
    )
    with pytest.raises(ViewerError):
        BundleIdentity.from_wire(dict(values, **changes))


def test_evicted_metadata_update_cannot_change_report_binding(bundle: BundleFixture) -> None:
    """A metadata edit cannot bind the existing archive to another report digest."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    repo.publish_bundle(identity, imported, marker, metadata)
    revision = replace(identity, metadata_revision=2)
    with pytest.raises(ViewerError):
        repo.publish_bundle(revision, None, dict(marker, report_sha256='a' * 64), metadata)
    assert revision.closing_key not in store.objects


def test_two_repositories_cannot_overwrite_same_immutable_metadata(bundle: BundleFixture) -> None:
    """Atomic creation preserves the winning metadata under competing repositories."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    other = BundleRepository(repo.config, store)
    store.objects[identity.archive_key] = imported.archive.read_bytes()
    barrier = Barrier(2)
    original = store.put_if_absent

    def concurrent_create(key: str, data: bytes) -> bool:
        """Synchronize competing metadata creates before their atomic storage call."""
        if '/metadata/' in key:
            barrier.wait(timeout=5)
        return original(key, data)

    store.put_if_absent = concurrent_create
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(repository.publish_bundle, identity, imported, marker, dict(metadata, display_name=name))
            for repository, name in ((repo, 'first'), (other, 'second'))
        ]
        successes, failures = [], []
        for future in futures:
            try:
                successes.append(future.result())
            except ViewerError as error:
                failures.append(error)
    assert len(successes) == len(failures) == 1
    assert failures[0].code == 'bundle_conflict'
    assert repo.read_bundle(identity) == successes[0]
    repo.verify_bundle(successes[0])


def test_registry_cas_preserves_competing_runs(bundle: BundleFixture) -> None:
    """Concurrent registry updates retain both independently published run identities."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    other = BundleRepository(repo.config, store)
    barrier, lock = Barrier(2), Lock()
    original = store.compare_and_swap
    calls = 0

    def concurrent_cas(key: str, expected: bytes | None, data: bytes) -> bool:
        """Race the first head updates while allowing later CAS retries to progress."""
        nonlocal calls
        with lock:
            calls += 1
            synchronize = calls <= 2
        if synchronize:
            barrier.wait(timeout=5)
        return original(key, expected, data)

    store.compare_and_swap = concurrent_cas
    identities = [
        replace(identity, run_id=run_id)
        for run_id in ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'ffffffff-ffff-ffff-ffff-ffffffffffff')
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(repository.publish_tombstone, value, metadata)
            for repository, value in zip((repo, other), identities)
        ]
        results = [future.result() for future in futures]
    assert repo.discover() == results
    assert len(repo.registry_snapshot().entries) == 2


def test_missing_registry_cannot_be_reinitialized_over_existing_closures(bundle: BundleFixture) -> None:
    """Missing registry heads cannot silently reset a namespace containing publications."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    repo.publish_bundle(identity, imported, marker, metadata)
    del store.objects[REGISTRY_HEAD]
    store.writes.clear()
    with pytest.raises(ViewerError) as error:
        repo.initialize_registry()
    assert error.value.code == 'registry_missing'
    assert store.writes == []
    with pytest.raises(ViewerError):
        repo.publish_bundle(replace(identity, metadata_revision=2), imported, marker, metadata)
    assert store.writes == []


def test_failed_registry_commit_does_not_report_backup_success(bundle: BundleFixture) -> None:
    """A closed bundle remains undiscoverable until its registry update succeeds."""
    repo, store, identity, imported, marker, metadata, entry = bundle
    original = store.compare_and_swap

    def fail(key: str, expected: bytes | None, data: bytes) -> bool:
        """Fail the registry CAS before installing the new head."""
        if key == REGISTRY_HEAD:
            raise StorageError()
        return original(key, expected, data)

    store.compare_and_swap = fail
    with pytest.raises(StorageError):
        repo.publish_bundle(identity, imported, marker, metadata)
    assert repo.discover() == []
    assert identity.closing_key in store.objects
    store.compare_and_swap = original
    repo.publish_bundle(identity, imported, marker, metadata)
    assert len(repo.discover()) == 1


@pytest.mark.parametrize('lost_reply', [False, True])
def test_reservation_failure_precedes_immutable_writes(bundle: BundleFixture, lost_reply: bool) -> None:
    """An uncertain reservation must be confirmed before any bundle bytes are installed."""
    repo, store, identity, imported, marker, metadata, _ = bundle
    original = store.compare_and_swap

    def fail(key: str, expected: bytes | None, data: bytes) -> bool:
        """Optionally commit the reservation before losing its response."""
        if key == RESERVATIONS:
            if lost_reply:
                original(key, expected, data)
            raise StorageError()
        return original(key, expected, data)

    store.compare_and_swap = fail
    if lost_reply:
        repo.publish_bundle(identity, imported, marker, metadata)
        assert repo.reservation_floors()[1][identity.run_id] == (1, 1)
    else:
        with pytest.raises(StorageError):
            repo.publish_bundle(identity, imported, marker, metadata)
        assert all(key.startswith('bundle-registry/') for key in store.objects)


def test_reservation_floors_preserve_revisions_across_generations(bundle: BundleFixture) -> None:
    """Later generations and late old jobs cannot lower either allocation floor."""
    repo, store, identity, imported, marker, metadata, _ = bundle
    repo.publish_bundle(replace(identity, metadata_revision=5), imported, marker, metadata)
    repo.publish_bundle(replace(identity, generation=2), imported, marker, metadata)
    repo.publish_bundle(replace(identity, metadata_revision=6), imported, marker, metadata)
    assert repo.reservation_floors()[1][identity.run_id] == (2, 6)
