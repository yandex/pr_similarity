"""Make-backed daemon lifecycle and private HTTP administration."""

import fcntl
import getpass
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from viewer.api import admin_token, create_app
from viewer.config import Config
from viewer.database import Database
from viewer.errors import ViewerError
from viewer.util import json_bytes, parse_json


def _url(config):
    host='127.0.0.1' if config.host=='0.0.0.0' else '::1' if config.host=='::' else config.host
    host='['+host+']' if ':' in host and not host.startswith('[') else host
    value=os.environ.get('PRSIM_VIEWER_URL') or ('https' if config.tls_cert else 'http')+'://'+host+':'+str(config.port)
    parsed=urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/') or parsed.scheme not in ('https','http') or not parsed.hostname:
        raise ViewerError('invalid_url','Invalid administrative URL.')
    if parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
        raise ViewerError('insecure_url','HTTP administration is restricted to loopback.')
    return value.rstrip('/')


def _request(config,method,path,body=None,admin=False):
    headers={}
    if admin:headers['Authorization']='Bearer '+admin_token(config.admin_token_file)
    data=json_bytes(body) if body is not None else None
    if data is not None:headers['Content-Type']='application/json'
    request=Request(_url(config)+path,data=data,headers=headers,method=method)
    try:
        with urlopen(request,timeout=10) as response:return parse_json(response.read(1<<20))
    except HTTPError as e:
        raise ViewerError('admin_request_failed','Administrative API rejected the operation (HTTP '+str(e.code)+').',e.code) from None
    except (URLError,TimeoutError):raise ViewerError('server_unavailable','Viewer is unavailable.',503) from None


def _identity(pid):
    try:
        # Linux field 22, accounting for a parenthesized command name.
        fields=Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
        return fields[19] if fields[0]!='Z' else None
    except (OSError,IndexError):return None


def _running(config):
    path=config.runtime/'viewer.pid.json'
    if not path.exists():return None
    value=parse_json(path.read_bytes())
    return value if _identity(value['pid'])==value['start_ticks'] else None


def _create_token(config):
    config.runtime.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(config.runtime,0o700)
    try:
        fd=os.open(config.admin_token_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
    except FileExistsError:
        admin_token(config.admin_token_file);return
    with os.fdopen(fd,'w') as stream:
        stream.write(secrets.token_urlsafe(48)+'\n');stream.flush();os.fsync(stream.fileno())


def lifecycle(command,config,config_path):
    running=_running(config)
    if command=='status':
        return {'running':bool(running),'pid':running['pid'] if running else None}
    if command=='logs':
        log=config.runtime/'viewer.log'
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-(1<<20)))
                print(stream.read().decode(errors='replace'))
        return None
    if command=='stop':
        if not running:return {'running':False}
        os.kill(running['pid'],signal.SIGTERM)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline and _identity(running['pid'])==running['start_ticks']:
            time.sleep(.1)
        if _identity(running['pid'])==running['start_ticks']:raise ViewerError('stop_timeout','Viewer has not exited; inspect its log.',503)
        (config.runtime/'viewer.pid.json').unlink(missing_ok=True)
        return {'running':False}
    if command=='start':
        if running:return {'running':True,'pid':running['pid']}
        _create_token(config)
        env=dict(os.environ);env['PRSIM_VIEWER_CONFIG']=str(config_path.resolve())
        log=config.runtime/'viewer.log'
        fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        with os.fdopen(fd,'ab') as output:
            process=subprocess.Popen([sys.executable,'-m','viewer','serve'],stdin=subprocess.DEVNULL,stdout=output,stderr=output,env=env,start_new_session=True)
        identity={'pid':process.pid,'start_ticks':_identity(process.pid)}
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if process.poll() is not None:raise ViewerError('start_failed','Viewer exited during startup; inspect its log.',503)
            try:
                _request(config,'GET','/healthz')
                (config.runtime/'viewer.pid.json').write_bytes(json_bytes(identity));os.chmod(config.runtime/'viewer.pid.json',0o600)
                return {'running':True,'pid':process.pid}
            except ViewerError:time.sleep(.1)
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        raise ViewerError('start_timeout','Viewer did not become healthy; inspect its log.',503)
    raise ViewerError('invalid_command','Unknown lifecycle command.')


def administer(command,config):
    def login():return os.environ.get('PRSIM_VIEWER_LOGIN') or input('Login: ').strip()
    if command in ('admin-profile-create','admin-role-set','admin-profile-password-set'):
        # Local provisioning deliberately ignores PRSIM_VIEWER_URL. Check the same
        # owner-only capability used by the daemon, then use SQLite transactions.
        admin_token(config.admin_token_file)
        if config.runtime.stat().st_uid != os.getuid():
            raise ViewerError('unsafe_runtime','Local provisioning requires the runtime owner.')
        db=Database(config.runtime,ttl=config.grant_ttl_seconds)
        try:
            if command=='admin-profile-create':
                return db.create_user(login(),getpass.getpass('Password: '),role='admin')
            if command=='admin-profile-password-set':
                return db.set_password(login(),getpass.getpass('New password: '))
            role=os.environ.get('PRSIM_VIEWER_ROLE') or input('Role (user/admin): ').strip()
            return db.set_role(login(),role)
        finally:db.close()
    base='/api/v1/admin'
    if command=='admin-user-create':return _request(config,'POST',base+'/users',{'login':login(),'password':getpass.getpass('Password: ')},True)
    if command in ('admin-user-enable','admin-user-disable'):
        return _request(config,'PATCH',base+'/users/'+quote(login(),safe=''),{'enabled':command.endswith('enable')},True)
    if command=='admin-password-set':return _request(config,'PUT',base+'/users/'+quote(login(),safe='')+'/password',{'password':getpass.getpass('New password: ')},True)
    if command=='admin-basket-order':
        raw=os.environ.get('PRSIM_VIEWER_ORDER_JSON') or input('Basket hashes as JSON array: ')
        return _request(config,'PUT',base+'/baskets/order',{'items':parse_json(raw.encode())},True)
    if command=='admin-run-delete':
        rid=os.environ.get('PRSIM_VIEWER_RUN_ID') or input('Run ID: ').strip()
        return _request(config,'DELETE',base+'/runs/'+quote(rid,safe=''),admin=True)
    if command=='admin-refresh':return _request(config,'POST',base+'/refresh',{},True)
    raise ViewerError('invalid_command','Unknown administrative command.')


def main():
    command=sys.argv[1] if len(sys.argv)==2 else ''
    if command not in ('serve','start','stop','status','logs','admin-profile-create','admin-role-set','admin-profile-password-set','admin-user-create','admin-user-enable','admin-user-disable','admin-password-set','admin-basket-order','admin-run-delete','admin-refresh'):
        print('Use make setup/start/stop/status/logs/test or an admin-* target.',file=sys.stderr);return 2
    try:
        path=Path(os.environ.get('PRSIM_VIEWER_CONFIG','config.local.json'))
        config=Config.load(path)
        if command=='serve':
            import uvicorn
            config.runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
            with (config.runtime/'service.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ViewerError('already_running','A viewer process already owns this runtime.',409) from None
                _create_token(config)
                uvicorn.run(create_app(config),host=config.host,port=config.port,ssl_certfile=config.tls_cert,ssl_keyfile=config.tls_key,workers=1,access_log=False,timeout_graceful_shutdown=15)
            return 0
        result=lifecycle(command,config,path) if command in ('start','stop','status','logs') else administer(command,config)
        if result is not None:print(json.dumps(result))
        return 0
    except (ViewerError,OSError) as e:
        print(e.message if isinstance(e,ViewerError) else 'Local operation failed; check configuration and file permissions.',file=sys.stderr)
        return 1
