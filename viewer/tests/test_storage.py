"""S3 SDK contract verification with Stubber; no network or credential lookup."""

import io

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from viewer.config import Config
from viewer.errors import StorageError
from viewer.storage import S3Store, MemoryStore, MockFileStore


def test_sdk_namespace_pagination_and_copy():
    client=boto3.client('s3',region_name='us-east-1',aws_access_key_id='mock',aws_secret_access_key='mock')
    store=S3Store(Config(bucket='bucket',prefix='viewer'),client)
    with Stubber(client) as stub:
        stub.add_response('put_object',{}, {'Bucket':'bucket','Key':'viewer/settings/x','Body':b'{}'})
        store.put('settings/x',b'{}')
        stub.add_response('get_object',{'Body':StreamingBody(io.BytesIO(b'{}'),2)}, {'Bucket':'bucket','Key':'viewer/settings/x'})
        assert store.get('settings/x')==b'{}'
        stub.add_response('list_objects_v2',{'IsTruncated':True,'NextContinuationToken':'next','Contents':[{'Key':'viewer/runs/a'}]}, {'Bucket':'bucket','Prefix':'viewer/runs/'})
        stub.add_response('list_objects_v2',{'IsTruncated':False,'Contents':[{'Key':'viewer/runs/b'}]}, {'Bucket':'bucket','Prefix':'viewer/runs/','ContinuationToken':'next'})
        assert list(store.list('runs/'))==['runs/a','runs/b']
        stub.add_response('copy_object',{}, {'Bucket':'bucket','Key':'viewer/trash/a','CopySource':{'Bucket':'bucket','Key':'viewer/runs/a'}})
        store.copy('runs/a','trash/a')
        stub.add_client_error('delete_object','AccessDenied',expected_params={'Bucket':'bucket','Key':'viewer/runs/a'})
        with pytest.raises(StorageError):store.delete('runs/a')


@pytest.mark.parametrize('memory',[True,False])
def test_mock_contract_roundtrip(tmp_path,memory):
    store=MemoryStore() if memory else MockFileStore(tmp_path/'storage')
    path=tmp_path/'blob';path.write_bytes(b'archive')
    store.put_file('runs/a/archive',path);store.copy('runs/a/archive','trash/a/archive')
    store.delete('runs/a/archive')
    assert list(store.list('runs/'))==[]
    assert store.get('trash/a/archive')==b'archive'


def test_stream_failure_is_a_safe_storage_error():
    from viewer.storage import S3Body
    from botocore.exceptions import ReadTimeoutError
    class Broken:
        def read(self,size):raise ReadTimeoutError(endpoint_url='https://mock.example')
        def close(self):pass
    with pytest.raises(StorageError) as error:S3Body(Broken()).read(1)
    assert error.value.status==503
    assert 'mock.example' not in error.value.message


def test_streaming_file_put_checks_deadline_and_preserves_bytes(tmp_path):
    from viewer.storage import TimedReader
    import time
    body=TimedReader(io.BytesIO(b'123'),time.monotonic()-1)
    with pytest.raises(StorageError):body.read(1)
    path=tmp_path/'blob';path.write_bytes(b'x'*(130<<10))
    class OfflineClient:
        def put_object(self,**kwargs):
            assert kwargs['ContentLength']==path.stat().st_size
            chunks=[]
            while data:=kwargs['Body'].read(16<<10):chunks.append(data)
            assert b''.join(chunks)==path.read_bytes()
            return {}
    store=S3Store(Config(bucket='bucket'),OfflineClient())
    store.put_file('runs/a/archive.tar.gz',path,deadline=time.monotonic()+60)


def test_real_sdk_put_streams_to_local_mock_s3(tmp_path):
    import hashlib
    import threading
    import time
    from http.server import BaseHTTPRequestHandler,HTTPServer
    from botocore.config import Config as SDKConfig
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            data=self.rfile.read(int(self.headers['Content-Length']));seen.append(data)
            self.send_response(200);self.send_header('Content-Length','0');self.send_header('ETag','"'+hashlib.md5(data).hexdigest()+'"');self.end_headers()
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever);thread.start()
    try:
        client=boto3.client('s3',region_name='us-east-1',endpoint_url='http://127.0.0.1:'+str(server.server_port),aws_access_key_id='mock',aws_secret_access_key='mock',config=SDKConfig(connect_timeout=3,read_timeout=3,retries={'max_attempts':0}))
        store=S3Store(Config(bucket='bucket'),client)
        path=tmp_path/'blob';path.write_bytes(b'test payload\n'*(20<<10))
        store.put_file('runs/alice/run/archive.tar.gz',path,deadline=time.monotonic()+10)
        assert seen==[path.read_bytes()]
    finally:server.shutdown();server.server_close();thread.join(timeout=5)
