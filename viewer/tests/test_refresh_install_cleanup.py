"""Refresh retains ownership of installed copies until their database binding is known."""

import json
from pathlib import Path
from typing import NoReturn

import pytest
import viewer.local_files as files_module
from test_local_service import require_record, service
from test_service import publish
from viewer.errors import ViewerError
from viewer.local_repository import LocalRecord


@pytest.mark.parametrize('committed', [False, True])
def test_refresh_merge_failure_cleans_only_unbound_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    """An error before or after commit never leaks the new copy or deletes bound bytes."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    svc.activate_backup(rid)
    assert svc.backups.run_once()
    original = require_record(svc, rid)
    merge = svc._merge_refreshed_files

    def fail(verified: LocalRecord, expected: LocalRecord | None) -> NoReturn:
        """Inject an ambiguous error on either side of the real commit."""
        if committed:
            assert merge(verified, expected)
        raise OSError('commit response lost')

    monkeypatch.setattr(svc, '_merge_refreshed_files', fail)
    try:
        assert svc.refresh()['status'] == 'partial'
        svc._cleanup_orphan_paths()
        current = require_record(svc, rid)
        assert current.local_path is not None and Path(current.local_path).is_dir()
        paths = {path.parent for path in svc.files.root.glob('*/*/*/archive.tar.gz')}
        if committed:
            assert current.local_path != original.local_path
            assert Path(current.local_path) in paths
        else:
            assert paths == {Path(current.local_path)}
        assert not list((svc.config.runtime / 'refresh-installs').glob('*.json'))
    finally:
        svc.close()
        svc.db.close()


def test_refresh_cleanup_intent_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A temporary database read failure preserves cleanup ownership across restart."""
    svc = service(tmp_path)
    remote = svc.remote
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    svc.activate_backup(rid)
    assert svc.backups.run_once()

    def unavailable() -> NoReturn:
        """Prevent proving whether the new copy belongs to a committed row."""
        raise OSError('database unavailable')

    def fail(verified: LocalRecord, expected: LocalRecord | None) -> NoReturn:
        """Fail commit and its immediate binding check."""
        monkeypatch.setattr(svc.repository, 'versions', unavailable)
        raise OSError('database unavailable')

    monkeypatch.setattr(svc, '_merge_refreshed_files', fail)
    assert svc.refresh()['status'] == 'partial'
    assert len(list((svc.config.runtime / 'refresh-installs').glob('*.json'))) == 1
    svc.close()
    svc.db.close()
    restarted = service(tmp_path, remote)
    try:
        restarted._cleanup_orphan_paths()
        current = require_record(restarted, rid)
        assert current.local_path is not None
        assert {path.parent for path in restarted.files.root.glob('*/*/*/archive.tar.gz')} == {Path(current.local_path)}
        assert not list((restarted.config.runtime / 'refresh-installs').glob('*.json'))
    finally:
        restarted.close()
        restarted.db.close()


def test_refresh_install_failure_after_rename_removes_unbound_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable cleanup intent exists before install can move bytes into the cache."""
    svc = service(tmp_path)
    report, _, _ = publish(svc, tmp_path)
    rid = report['run']['id']
    svc.activate_backup(rid)
    assert svc.backups.run_once()

    def fail_marker(path: Path, value: dict) -> None:
        """Fail marker persistence after install has already renamed its tree."""
        raise OSError('marker write failed after rename')

    monkeypatch.setattr(files_module, 'atomic_json', fail_marker)
    try:
        assert svc.refresh()['status'] == 'partial'
        svc._cleanup_orphan_paths()
        current = require_record(svc, rid)
        assert current.local_path is not None
        assert {path.parent for path in svc.files.root.glob('*/*/*/archive.tar.gz')} == {Path(current.local_path)}
    finally:
        svc.close()
        svc.db.close()


@pytest.mark.parametrize('intent', [None, {'run_id': 7, 'paths': []}, {'run_id': 'alice', 'paths': [None]}])
def test_invalid_refresh_cleanup_journal_preserves_files(tmp_path: Path, intent: dict | None) -> None:
    """Malformed cleanup state cannot authorize deletion and remains available for diagnosis."""
    svc = service(tmp_path)
    journal = svc.config.runtime / 'refresh-installs' / 'invalid.json'
    journal.parent.mkdir()
    journal.write_text(json.dumps(intent))
    try:
        with pytest.raises(ViewerError, match='Invalid refresh cleanup'):
            svc._cleanup_orphan_paths()
        assert journal.exists()
    finally:
        svc.close()
        svc.db.close()
