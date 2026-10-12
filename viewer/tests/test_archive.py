"""Real tar bytes at the untrusted upload boundary."""

import io
import tarfile
from dataclasses import replace

import pytest
from helpers import archive_fixture
from viewer.archive import validate_archive
from viewer.config import Limits
from viewer.errors import ViewerError


def test_accepts_frozen_producer_schema_archive(tmp_path):
    path=tmp_path/'run.tar.gz'; report, manifest=archive_fixture(path)
    result=validate_archive(path,tmp_path/'data',Limits())
    assert result.report==report
    assert result.manifest==manifest
    assert result.archive_sha256


@pytest.mark.parametrize('kind',['traversal','absolute','link','duplicate','sparse'])
def test_rejects_unsafe_members(tmp_path,kind):
    name={'traversal':'../escape','absolute':'/escape','link':'link','duplicate':'report.json','sparse':'sparse'}[kind]
    member=tarfile.TarInfo(name); member.size=1
    if kind=='link': member.type=tarfile.SYMTYPE; member.linkname='/etc/passwd'
    if kind=='sparse': member.type=tarfile.GNUTYPE_SPARSE
    path=tmp_path/'run.tar.gz'; archive_fixture(path,extra=(member,io.BytesIO(b'x')))
    with pytest.raises(ViewerError): validate_archive(path,tmp_path/'data',Limits())
    assert not (tmp_path/'escape').exists()
    assert not (tmp_path/'data').exists()


@pytest.mark.parametrize('limit',['archive_bytes','unpacked_bytes','file_bytes','json_bytes','files','path_bytes','expansion_ratio'])
def test_resource_limits(tmp_path,limit):
    path=tmp_path/'run.tar.gz'; archive_fixture(path)
    with pytest.raises(ViewerError): validate_archive(path,tmp_path/'data',replace(Limits(),**{limit:1}))
    assert not (tmp_path/'data').exists()


@pytest.mark.parametrize('kind',['score','revision','basket','reference','summary'])
def test_semantic_validation(tmp_path,kind):
    def change(report,basket,files):
        if kind=='score': report['tasks'][0]['scoring']['Mf_mlp']=.4
        if kind=='revision': report['schema_version']=9
        if kind=='basket': report['basket']['sha256']='0'*64
        if kind=='reference': del files[report['tasks'][0]['scoring']['files'][0]['human']]
        if kind=='summary': report['summary']['scored_tasks']=0
    path=tmp_path/'run.tar.gz';archive_fixture(path,change=change)
    with pytest.raises(ViewerError): validate_archive(path,tmp_path/'data',Limits())


def test_oversized_pax_header_fails_before_tarfile_allocates_payload(tmp_path):
    member=tarfile.TarInfo('extra');member.pax_headers={'comment':'x'*(128<<10)}
    path=tmp_path/'run.tar.gz';archive_fixture(path,extra=(member,io.BytesIO(b'')))
    with pytest.raises(ViewerError) as error:validate_archive(path,tmp_path/'data',Limits())
    assert error.value.status==413
    assert not (tmp_path/'data').exists()


def test_public_diff_cannot_alias_logs_or_previous_attempt(tmp_path):
    for target in ['logs_20260930T120000000000Z/33333333-3333-4333-8333-333333333333/agent.log','attempts/old/old/triplets/000001/human']:
        def change(report,basket,files):
            row=report['tasks'][0]['scoring']['files'][0]
            files[target]=b'private log';row['human']=target
        path=tmp_path/'run.tar.gz';archive_fixture(path,change=change)
        with pytest.raises(ViewerError) as error:validate_archive(path,tmp_path/'data',Limits())
        assert error.value.code=='invalid_public_artifact'


def test_triplet_owner_and_state_receipts_are_verified(tmp_path):
    from viewer.util import parse_json,json_bytes
    def change(report,basket,files):
        key=report['tasks'][0]['artifacts']['triplets_manifest'];index=parse_json(files[key])
        index['attempt_id']='00000000-0000-4000-8000-000000000000';files[key]=json_bytes(index)
    path=tmp_path/'run.tar.gz';archive_fixture(path,change=change)
    with pytest.raises(ViewerError) as error:validate_archive(path,tmp_path/'data',Limits())
    assert error.value.code=='invalid_triplets'
