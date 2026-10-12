"""Persistent auth boundaries, deterministic token clock, no secret snapshots."""

import stat

import pytest
from viewer.database import Database
from viewer.errors import ViewerError


def test_user_grants_ttl_and_consumption(tmp_path):
    now=[1000.0];db=Database(tmp_path/'runtime',clock=lambda:now[0])
    db.create_user('student','secret')
    grant=db.issue('student','secret','download','run')
    assert db.authorize(grant['token'],'download','run')=='student'
    now[0]+=60
    with pytest.raises(ViewerError) as err: db.authorize(grant['token'],'download','run')
    assert err.value.status==401
    grant=db.issue('student','secret','metadata','run')
    with pytest.raises(ViewerError): db.authorize(grant['token'],'download','run')
    db.consume(grant['token']);
    with pytest.raises(ViewerError): db.authorize(grant['token'],'metadata','run')
    assert stat.S_IMODE((tmp_path/'runtime').stat().st_mode)==0o700
    assert stat.S_IMODE(db.path.stat().st_mode)==0o600
    assert grant['token'].encode() not in db.path.read_bytes()
    db.close()


def test_password_disable_revoke_and_restart_owner(tmp_path):
    db=Database(tmp_path/'runtime')
    db.create_user('Alice','secret'); g=db.issue('Alice','secret','publish')
    db.reserve_owner('run','Alice','basket')
    db.update_user('Alice',enabled=False)
    with pytest.raises(ViewerError):db.authorize(g['token'],'publish')
    db.update_user('Alice',enabled=True)
    g=db.issue('Alice','secret','publish');db.set_password('Alice','new')
    with pytest.raises(ViewerError):db.authorize(g['token'],'publish')
    with pytest.raises(ViewerError):db.issue('alice','new','publish')
    assert db.list_users()==[{'login':'Alice','enabled':True}]
    db.close();db=Database(tmp_path/'runtime')
    assert db.owner('run')=={'login':'Alice','basket_hash':'basket'}
    with pytest.raises(ViewerError) as err:db.reserve_owner('run','Bob','basket')
    assert err.value.status==403
    db.backup(tmp_path/'backup.sqlite');db.close()


def test_journal_and_receipt_persistent(tmp_path):
    db=Database(tmp_path/'runtime');db.save_operation('op',{'kind':'publish','phase':'prepared'})
    db.close();db=Database(tmp_path/'runtime')
    assert db.operations()=={'op':{'kind':'publish','phase':'prepared'}}
    db.finish_operation('op','user','key','fingerprint',{'run_id':'run'})
    assert db.operations()=={}
    assert db.receipt('user','key')=={'fingerprint':'fingerprint','receipt':{'run_id':'run'}}
    db.close()


def test_publication_grant_owner_journal_are_one_transaction(tmp_path):
    import sqlite3
    db=Database(tmp_path/'runtime');db.create_user('alice','secret');grant=db.issue('alice','secret','publish')
    op={'owner':'alice','marker':{'run_id':'run','basket_sha256':'basket'}}
    db.connection.execute("CREATE TRIGGER fail_op BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT,'synthetic write fault'); END")
    with pytest.raises(sqlite3.IntegrityError):db.begin_publication('op',op,grant['token'])
    assert db.authorize(grant['token'],'publish')=='alice'
    assert db.owner('run') is None
    assert db.operations()=={}
    db.connection.execute('DROP TRIGGER fail_op');db.begin_publication('op',op,grant['token'])
    with pytest.raises(ViewerError):db.authorize(grant['token'],'publish')
    assert db.owner('run')['login']=='alice'
    assert 'op' in db.operations()
    db.close()
