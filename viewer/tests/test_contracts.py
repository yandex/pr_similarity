"""Malformed uploads fail safely even when their manifest is rebuilt."""

import gzip
import io
import json
import tarfile
from dataclasses import replace

import pytest
from helpers import archive_fixture
from viewer.archive import validate_archive
from viewer.config import Config, Limits
from viewer.errors import ViewerError
from viewer.util import parse_json, safe_path


@pytest.mark.parametrize('raw',[b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":Infinity}',b'{"a":1e999}',b'{"a":-1e999}',b'\xff'])
def test_strict_json(raw):
    with pytest.raises(ViewerError): parse_json(raw)


@pytest.mark.parametrize('path',['../foo','/foo','a/../b','a\\b','a//b','a/./b','a\x00b'])
def test_path_normalization_is_not_implicit(path):
    with pytest.raises(ViewerError): safe_path(path)


def test_hash_damage_detected(tmp_path):
    path=tmp_path/'run.tar.gz'; archive_fixture(path)
    changed=tmp_path/'changed.tar.gz'
    with tarfile.open(path,'r:gz') as source, tarfile.open(changed,'w:gz') as target:
        for member in source:
            data=source.extractfile(member).read()
            if member.name=='report.json': data=data.replace(b'baseline',b'badagent')
            member.size=len(data);target.addfile(member,io.BytesIO(data))
    with pytest.raises(ViewerError,match='inconsistent'): validate_archive(changed,tmp_path/'data',Limits())


def test_gzip_trailing_bomb_is_counted(tmp_path):
    path=tmp_path/'run.tar.gz';archive_fixture(path)
    with path.open('ab') as stream: stream.write(gzip.compress(b'X'*(2<<20)))
    with pytest.raises(ViewerError): validate_archive(path,tmp_path/'data',replace(Limits(),unpacked_bytes=1<<20))


def test_duplicate_json_rejected_with_valid_inventory(tmp_path):
    path=tmp_path/'run.tar.gz'
    archive_fixture(path, change=lambda report,basket,files:files.update({'config_identity.json':b'{"x":1,"x":2}'}))
    with pytest.raises(ViewerError): validate_archive(path,tmp_path/'data',Limits())


def test_config_rejects_unknown_fields_and_credentials_in_url(tmp_path):
    p=tmp_path/'config.json';p.write_text(json.dumps({'storage':'mock','bogus':1}))
    with pytest.raises(ViewerError): Config.load(p)
    with pytest.raises(ViewerError): Config(storage='s3',bucket='bucket',endpoint='https://a:b@host')
    with pytest.raises(ViewerError): Limits(files=-1)
