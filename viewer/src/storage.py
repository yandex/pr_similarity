"""Small S3 object contract and explicit offline mocks; no provider APIs."""

import io
import shutil
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from viewer.errors import StorageError, ViewerError
from viewer.util import safe_path


class Store:
    def get(self, key, limit=64<<20):
        with self.open(key) as body:
            data = body.read(limit+1)
        if len(data)>limit: raise ViewerError('object_limit','Stored object exceeds configured limit.',413)
        return data

    def put(self, key, data):
        raise NotImplementedError

    def open(self, key):
        raise NotImplementedError

    def list(self, prefix):
        raise NotImplementedError

    def put_file(self, key, path, *, deadline=None):
        raise NotImplementedError

    def copy(self, source, target):
        raise NotImplementedError

    def delete(self, key):
        raise NotImplementedError


class TimedReader:
    def __init__(self,stream,deadline=None):self.stream,self.deadline=stream,deadline
    def read(self,size=-1):
        if size<0:raise StorageError()
        if self.deadline is not None and time.monotonic()>self.deadline:raise StorageError()
        return self.stream.read(size)
    def __getattr__(self,name):return getattr(self.stream,name)


class S3Body:
    def __init__(self,body):self.body=body
    def read(self,size=-1):
        try:return self.body.read(size)
        except (BotoCoreError,OSError):raise StorageError() from None
    def close(self):self.body.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class S3Store(Store):
    def __init__(self, config, client=None):
        self.bucket, self.prefix = config.bucket, config.prefix.strip('/')
        self.client = client or boto3.client('s3',endpoint_url=config.endpoint,region_name=config.region,
            config=BotoConfig(connect_timeout=10,read_timeout=30,retries={'max_attempts':3,'mode':'standard'}))

    def _key(self,key):
        safe_path(key.rstrip('/'))
        return self.prefix + '/' + key if self.prefix else key

    def _call(self,method,**kwargs):
        try: return getattr(self.client,method)(Bucket=self.bucket,**kwargs)
        except ClientError as e:
            if e.response['Error']['Code'] in ('NoSuchKey','404','NotFound'):
                raise ViewerError('not_found','Object not found.',404) from None
            raise StorageError() from None
        except BotoCoreError: raise StorageError() from None

    def open(self,key):
        return S3Body(self._call('get_object',Key=self._key(key))['Body'])

    def put(self,key,data):
        self._call('put_object',Key=self._key(key),Body=data)

    def put_file(self,key,path,*,deadline=None):
        # Limits keep individual uploads <=1 GiB, below the single PUT limit.
        with Path(path).open('rb') as body: self._call('put_object',Key=self._key(key),Body=TimedReader(body,deadline),ContentLength=Path(path).stat().st_size)

    def list(self,prefix):
        continuation=None
        while True:
            kwargs={'Prefix':self._key(prefix)}
            if continuation: kwargs['ContinuationToken']=continuation
            response=self._call('list_objects_v2',**kwargs)
            for entry in response.get('Contents',[]):
                key=entry['Key'];yield key[len(self.prefix)+1:] if self.prefix else key
            if not response.get('IsTruncated'): break
            continuation=response['NextContinuationToken']

    def copy(self,source,target):
        self._call('copy_object',Key=self._key(target),CopySource={'Bucket':self.bucket,'Key':self._key(source)})

    def delete(self,key):
        self._call('delete_object',Key=self._key(key))


class MemoryStore(Store):
    """Offline test object store; failure injection subclasses use the same contract."""
    def __init__(self): self.objects={}
    def open(self,key):
        if key not in self.objects: raise ViewerError('not_found','Object not found.',404)
        return io.BytesIO(self.objects[key])
    def put(self,key,data): safe_path(key);self.objects[key]=bytes(data)
    def put_file(self,key,path,*,deadline=None):
        parts=[]
        with Path(path).open('rb') as stream:
            reader=TimedReader(stream,deadline)
            while data:=reader.read(64<<10):parts.append(data)
        self.put(key,b''.join(parts))
    def list(self,prefix): return iter(sorted(k for k in self.objects if k.startswith(prefix)))
    def copy(self,source,target): self.put(target,self.get(source,1<<40))
    def delete(self,key): self.objects.pop(key,None)


class MockFileStore(Store):
    """Persist an offline S3 mock on the current host for local HTTP/UI checks."""
    def __init__(self,root): self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
    def path(self,key): return self.root/safe_path(key)
    def open(self,key):
        try:return self.path(key).open('rb')
        except FileNotFoundError:raise ViewerError('not_found','Object not found.',404) from None
    def put(self,key,data):
        path=self.path(key);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    def put_file(self,key,path,*,deadline=None):
        target=self.path(key);target.parent.mkdir(parents=True,exist_ok=True)
        with Path(path).open('rb') as source,target.open('wb') as output:
            shutil.copyfileobj(TimedReader(source,deadline),output,64<<10)
    def list(self,prefix): return iter(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*') if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix)))
    def copy(self,source,target): self.put_file(target,self.path(source))
    def delete(self,key): self.path(key).unlink(missing_ok=True)
