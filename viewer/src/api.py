"""HTTP ownership/auth boundaries and bounded transfer admission."""

import asyncio
import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from viewer.archive import validate_archive
from viewer.contracts import require
from viewer.database import Database
from viewer.config_view import public_config
from viewer.auth import BrowserAuth, SESSION_COOKIE
from viewer.errors import ViewerError
from viewer.middleware import SecurityHeaders
from viewer.presentation import Query, TaskQuery, basket_runs, catalogue, file_diff, find_task, public_files, public_task
from viewer.service import ViewerService
from viewer.storage import MockFileStore, S3Store
from viewer.util import digest, parse_json

HEX = re.compile(r'[0-9a-f]{64}\Z')


def admin_token(path):
    try:
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
            mode=os.fstat(stream.fileno())
            require(stat.S_ISREG(mode.st_mode) and stat.S_IMODE(mode.st_mode)==0o400 and mode.st_uid==os.getuid(),'unsafe_admin_token')
            value=stream.read(4097).decode('ascii').strip()
        require(32<=len(value)<=4096 and not any(c.isspace() for c in value),'unsafe_admin_token')
        return value
    except (OSError,UnicodeError):raise ViewerError('unsafe_admin_token','Admin token needs an owner-only regular file with mode 0400.') from None


def bearer(request):
    value=request.headers.get('authorization','')
    return value[7:] if value.startswith('Bearer ') else ''


def create_app(config,store=None,clock=None):
    token=admin_token(config.admin_token_file)
    db=Database(config.runtime,ttl=config.grant_ttl_seconds,**({'clock':clock} if clock else {}))
    store=store or (MockFileStore(config.runtime/'mock-storage') if config.storage=='mock' else S3Store(config))
    try:service=ViewerService(config,store,db)
    except Exception:db.close();raise
    writes=asyncio.Lock()
    browser=BrowserAuth(db,config)
    password_checks=asyncio.Semaphore(config.password_checks)

    async def authenticate_perform(function):
        # Bound admission rather than accumulating an unbounded hash-work queue.
        if password_checks.locked():raise ViewerError('login_busy','Password verification is busy; retry.',429)
        async with password_checks:return await perform(function)

    async def perform(function):
        def invoke():
            with service.lock:
                if db.operations():service.recover();service.refresh()
                return function()
        return await run_in_threadpool(invoke)

    async def body(request,allowed,required=None,optional=False):
        raw=bytearray()
        async with asyncio.timeout(config.limits.transfer_seconds):
            async for data in request.stream():
                if len(raw)+len(data)>config.limits.api_body_bytes:raise ViewerError('body_limit','JSON request exceeds limit.',413)
                raw.extend(data)
        if not raw and optional:return {}
        value=parse_json(bytes(raw))
        require(isinstance(value,dict) and not set(value)-set(allowed) and set(allowed if required is None else required)<=set(value),'invalid_body')
        return value

    def require_admin(request):
        if 'authorization' in request.headers:
            if hmac.compare_digest(bearer(request),token):return
            raise ViewerError('admin_denied','Administrative authorization required.',403)
        actor=browser.session(request)
        if actor is None:raise ViewerError('auth_required','Authentication required.',401)
        if actor['role']!='admin':raise ViewerError('admin_denied','Administrative authorization required.',403)
        if request.method not in ('GET','HEAD'):browser.check_csrf(request)

    def archive_actor(request,rid):
        if 'authorization' in request.headers:
            if hmac.compare_digest(bearer(request),token):return {'login':None,'role':'admin'},False
            login=db.authorize(bearer(request),'download',rid)
            actor=db.actor(login)
            if actor is None:raise ViewerError('invalid_grant','Missing or expired grant.',401)
            return actor,True
        actor=browser.session(request)
        if actor is None:raise ViewerError('invalid_grant','Missing or expired grant.',401)
        if actor['role']!='admin':raise ViewerError('foreign_run','Only the owner grant or administrator can download.',403)
        return actor,False

    async def session(request):
        if 'authorization' in request.headers:raise ViewerError('invalid_grant','This route requires browser credentials.',401)
        if request.method=='POST':
            value=await body(request,{'login','password'})
            session_token,result=await authenticate_perform(lambda:browser.login(request,value['login'],value['password']))
        elif request.method=='DELETE':
            result=await perform(lambda:browser.logout(request))
        else:result=await perform(lambda:browser.info(request))
        response=JSONResponse(result)
        browser.csrf_cookie(response,result['csrf_token'],request)
        if request.method=='POST':
            response.set_cookie(SESSION_COOKIE,session_token,max_age=config.session_ttl_seconds,httponly=True,
                                samesite='lax',secure=browser.secure(request),path='/')
        elif request.method=='DELETE':response.delete_cookie(SESSION_COOKIE,path='/')
        return response

    async def own_password(request):
        if 'authorization' in request.headers:raise ViewerError('invalid_grant','This route requires browser credentials.',401)
        value=await body(request,{'current_password','new_password'})
        def change():
            browser.check_csrf(request)
            actor=browser.session(request)
            if actor is None:raise ViewerError('auth_required','Authentication required.',401)
            browser.authenticate(request,actor['login'],value['current_password'])
            db.set_password(actor['login'],value['new_password'])
            return {'changed':True}
        response=JSONResponse(await authenticate_perform(change))
        response.delete_cookie(SESSION_COOKIE,path='/')
        return response

    async def health(request):return JSONResponse({'status':'ok'})

    async def auth(request):
        operation=request.path_params['operation']
        if operation not in ('publish','download','metadata'):raise ViewerError('not_found','Resource not found.',404)
        fields={'login','password'} | ({'run_id'} if operation!='publish' else set())
        value=await body(request,fields)
        if operation!='publish':require(isinstance(value['run_id'],str),'invalid_run_id')
        def issue():
            # Authenticate first, before revealing any owner/run information.
            grant=db.issue(value['login'],value['password'],operation,value.get('run_id'),verify=lambda login,password:browser.authenticate(request,login,password))
            if operation!='publish':
                try:
                    actor=db.actor(value['login'])
                    if actor is None:raise ViewerError('auth_denied','Invalid credentials.',401)
                    if operation=='download' and actor['role']=='admin':service._require_run(value['run_id'])
                    else:service.require_owner(value['run_id'],value['login'])
                except ViewerError:
                    db.consume(grant['token']);raise
            return grant
        return JSONResponse(await authenticate_perform(issue))

    async def get_baskets(request):
        query=Query(request.query_params)
        return JSONResponse(await perform(lambda:catalogue(service,query)))

    async def get_basket_runs(request):
        query=Query(request.query_params)
        return JSONResponse(await perform(lambda:basket_runs(service,request.path_params['hash'],query)))

    async def get_run(request):
        query=Query(request.query_params)
        return JSONResponse(await perform(lambda:service.run(request.path_params['run_id'],**query.selection)))

    async def get_tasks(request):
        query=TaskQuery(request.query_params);rid=request.path_params['run_id']
        def read():
            sha=service.check_report(rid,request.query_params.get('expected_report_sha256'),request.query_params.get('expected_publication_sha256'))
            rows=[public_task(t,query.file) for t in service.full_report(rid)['tasks']]
            return {**query.page(query.sorted(rows)),'sort':query.sort,'direction':query.direction,'report_sha256':sha,'publication_sha256':service._require_run(rid)['marker']['archive_sha256']}
        return JSONResponse(await perform(read))

    async def get_config(request):
        return JSONResponse(await perform(lambda:public_config(service,request.path_params['run_id'],request.query_params.get('expected_report_sha256'),request.query_params.get('expected_publication_sha256'))))

    async def get_task(request):
        query=Query(request.query_params)
        def read():
            service.check_report(request.path_params['run_id'],request.query_params.get('expected_report_sha256'),request.query_params.get('expected_publication_sha256'))
            _,task=find_task(service,request.path_params['run_id'],request.path_params['task_id'])
            return {**public_task(task,query.file),'condition':task['agent_task'],'files':public_files(task)}
        return JSONResponse(await perform(read))

    async def get_file(request):
        def read():
            service.check_report(request.path_params['run_id'],request.query_params.get('expected_report_sha256'),request.query_params.get('expected_publication_sha256'))
            return file_diff(service,request.path_params['run_id'],request.path_params['task_id'],request.path_params['file_id'])
        return JSONResponse(await perform(read))

    async def pinned(request):
        value=await body(request,{'pinned'})
        def change():
            require_admin(request)
            return service.set_pinned(request.path_params['run_id'],value['pinned'])
        return JSONResponse(await perform(change))

    async def metadata(request):
        rid=request.path_params['run_id'];value=await body(request,{'display_name','tags'},required=set())
        def update():
            login=db.authorize(bearer(request),'metadata',rid)
            service.require_owner(rid,login);service.validate_patch(value)
            db.consume(bearer(request))
            return service.patch_metadata(rid,login,value)
        return JSONResponse(await perform(update))

    async def publish(request):
        rid=request.path_params['run_id']
        try:require(str(uuid.UUID(rid))==rid,'invalid_run_id')
        except ValueError:raise ViewerError('invalid_run_id','Invalid run ID.') from None
        overwrite=request.query_params.get('overwrite','0')
        require(overwrite in ('0','1'),'invalid_overwrite')
        key=request.headers.get('idempotency-key','')
        require(bool(key) and len(key)<=128 and all(32<=ord(c)<127 for c in key),'invalid_idempotency_key')
        archive_hash=request.headers.get('x-archive-sha256','');report_hash=request.headers.get('x-report-sha256','')
        require(bool(HEX.fullmatch(archive_hash)) and bool(HEX.fullmatch(report_hash)),'invalid_digest')
        require(request.headers.get('content-type','').split(';')[0]=='application/gzip','invalid_content_type')
        # TTL is checked once at admission, not after a long upload.
        login=await perform(lambda:db.authorize(bearer(request),'publish'))
        deadline=time.monotonic()+config.limits.transfer_seconds
        async with writes:
            if shutil.disk_usage(service.staging).free<config.limits.staging_free_bytes:
                raise ViewerError('staging_space','Insufficient staging space.',503)
            root=Path(tempfile.mkdtemp(prefix='upload-',dir=service.staging))
            try:
                path=root/'archive.tar.gz';hasher=hashlib.sha256();size=0
                async with asyncio.timeout(max(.001,deadline-time.monotonic())):
                    with path.open('xb') as output:
                        os.chmod(path,0o600)
                        async for data in request.stream():
                            size+=len(data)
                            if size>config.limits.archive_bytes:raise ViewerError('archive_limit','Compressed archive exceeds limit.',413)
                            output.write(data);hasher.update(data)
                        output.flush();os.fsync(output.fileno())
                require(hasher.hexdigest()==archive_hash,'archive_digest_mismatch')
                imported=await run_in_threadpool(validate_archive,path,root/'data',config.limits,deadline=deadline)
                require(imported.report['run']['id']==rid and imported.manifest['report_sha256']==report_hash,'publish_identity_mismatch')
                existed=await perform(lambda:rid in service.runs or db.receipt(login,key) is not None)
                result=await perform(lambda:service.publish(imported,login,key,overwrite=overwrite=='1',grant_token=bearer(request),deadline=deadline))
                return JSONResponse(result,status_code=200 if existed else 201)
            finally:shutil.rmtree(root,ignore_errors=True)

    async def download(request):
        rid=request.path_params['run_id']
        def admit():
            actor,consume=archive_actor(request,rid)
            row=service._require_run(rid) if actor['role']=='admin' else service.require_owner(rid,actor['login'])
            stream=service.store.open(row['prefix']+'archive.tar.gz')
            if consume:db.consume(bearer(request))
            return stream,dict(row['marker'])
        stream,marker=await perform(admit)
        deadline=time.monotonic()+config.limits.transfer_seconds
        async def chunks():
            hasher=hashlib.sha256();size=0
            try:
                while True:
                    if time.monotonic()>deadline:raise TimeoutError('Download deadline exceeded')
                    data=await run_in_threadpool(stream.read,64<<10)
                    if not data:break
                    size+=len(data);hasher.update(data)
                    if size>marker['archive_size']:raise OSError('Stored archive size differs')
                    yield data
                if size!=marker['archive_size'] or hasher.hexdigest()!=marker['archive_sha256']:raise OSError('Stored archive digest differs')
            finally:await run_in_threadpool(stream.close)
        headers={'Content-Length':str(marker['archive_size']),'X-Archive-SHA256':marker['archive_sha256'],'Content-Disposition':'attachment; filename="'+rid+'.tar.gz"','Cache-Control':'no-store'}
        return StreamingResponse(chunks(),media_type='application/gzip',headers=headers)

    async def users(request):
        if request.method=='GET':
            query=Query(request.query_params)
            details=request.query_params.get('details','0')
            if details not in ('0','1'):raise ViewerError('invalid_query','Invalid account detail selection.')
            def read():
                require_admin(request)
                return query.page(db.list_users(details=='1'))
            return JSONResponse(await perform(read))
        value=await body(request,{'login','password'})
        def create():
            require_admin(request)
            return db.create_user(value['login'],value['password'])
        return JSONResponse(await perform(create),status_code=201)

    async def user_access(request):
        value=await body(request,{'enabled'})
        def update():
            require_admin(request)
            return db.update_user(request.path_params['login'],enabled=value['enabled'],users_only=True)
        return JSONResponse(await perform(update))

    async def user_password(request):
        value=await body(request,{'password'})
        def update():
            require_admin(request)
            return db.set_password(request.path_params['login'],value['password'],users_only=True)
        return JSONResponse(await perform(update))

    async def basket_order(request):
        value=await body(request,{'items'})
        def update():
            require_admin(request)
            return service.set_order(value['items'])
        return JSONResponse(await perform(update))

    async def delete_run(request):
        def remove():
            require_admin(request)
            return service.delete(request.path_params['run_id'])
        return JSONResponse(await perform(remove))

    async def delete_owned_run(request):
        def remove():
            # Authorize the browser action before triggering any durable recovery.
            with service.lock:
                actor=browser.session(request)
                if actor is None:raise ViewerError('auth_required','Authentication required.',401)
                browser.check_csrf(request)
                return service.delete(request.path_params['run_id'],actor=actor,
                    publication=request.query_params.get('expected_publication_sha256'))
        return JSONResponse(await run_in_threadpool(remove))

    async def layout(request):
        value=await body(request,{'expected_revision','items'}) if request.method=='PUT' else None
        def read_or_write():
            require_admin(request)
            if value is None:return service.settings()
            require(isinstance(value['items'],list),'invalid_basket_display')
            order=[];display={}
            for item in value['items']:
                require(isinstance(item,dict) and set(item)=={'hash','hidden','collapsed'} and isinstance(item['hash'],str),'invalid_basket_display')
                order.append(item['hash']);display[item['hash']]={k:item[k] for k in ('hidden','collapsed')}
            return service.set_settings(value['expected_revision'],order,display)
        return JSONResponse(await perform(read_or_write))

    async def cache_stats(request):
        def read():
            require_admin(request)
            return service.cache_stats()
        return JSONResponse(await perform(read))

    async def refresh(request):
        await body(request,set(),optional=True)
        def update():
            require_admin(request)
            return service.refresh()
        return JSONResponse(await perform(update))

    async def asset(request):
        name=request.path_params.get('name','index.html')
        allowed={'index.html':'text/html','app.js':'text/javascript','account.js':'text/javascript','settings.js':'text/javascript','config.js':'text/javascript','style.css':'text/css','favicon.svg':'image/svg+xml'}
        if name not in allowed:raise ViewerError('not_found','Resource not found.',404)
        try:content=files('viewer').joinpath('static',name).read_bytes()
        except FileNotFoundError:return Response('UI implementation in progress',media_type='text/plain',status_code=503)
        return Response(content,media_type=allowed[name])

    async def error(request,exc):
        if isinstance(exc,ViewerError):status,code,message=exc.status,exc.code,exc.message
        elif isinstance(exc,HTTPException):status,code,message=exc.status_code,'not_found' if exc.status_code==404 else 'method_not_allowed','Resource or method is unavailable.'
        elif isinstance(exc,TimeoutError):status,code,message=413,'transfer_timeout','Request deadline exceeded.'
        else:status,code,message=503,'operation_failed','Operation could not be completed; retry.'
        return JSONResponse({'error':{'code':code,'message':message}},status_code=status,headers={'Retry-After':'1'} if status==503 else None)

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.close()

    routes=[Route('/healthz',health),Route('/',asset),Route('/run',asset),Route('/settings',asset),Route('/admin/users',asset),Route('/assets/{name}',asset),
        Route('/api/v1/auth/session',session,methods=['GET','POST','DELETE']),
        Route('/api/v1/auth/password',own_password,methods=['PUT']),
        Route('/api/v1/auth/{operation}-token',auth,methods=['POST']),
        Route('/api/v1/baskets',get_baskets),Route('/api/v1/baskets/{hash}/runs',get_basket_runs),
        Route('/api/v1/runs/{run_id}/config',get_config),
        Route('/api/v1/runs/{run_id}',get_run,methods=['GET']),Route('/api/v1/runs/{run_id}',publish,methods=['PUT']),Route('/api/v1/runs/{run_id}',delete_owned_run,methods=['DELETE']),
        Route('/api/v1/runs/{run_id}/tasks',get_tasks),Route('/api/v1/runs/{run_id}/tasks/{task_id}',get_task),
        Route('/api/v1/runs/{run_id}/tasks/{task_id}/files/{file_id}',get_file),
        Route('/api/v1/runs/{run_id}/pinned',pinned,methods=['PUT']),Route('/api/v1/runs/{run_id}/metadata',metadata,methods=['PATCH']),
        Route('/api/v1/runs/{run_id}/archive',download),Route('/api/v1/admin/users',users,methods=['GET','POST']),
        Route('/api/v1/admin/users/{login}',user_access,methods=['PATCH']),Route('/api/v1/admin/users/{login}/password',user_password,methods=['PUT']),
        Route('/api/v1/admin/baskets/order',basket_order,methods=['PUT']),Route('/api/v1/admin/runs/{run_id}',delete_run,methods=['DELETE']),
        Route('/api/v1/admin/baskets/layout',layout,methods=['GET','PUT']),Route('/api/v1/admin/stats',cache_stats),
        Route('/api/v1/admin/refresh',refresh,methods=['POST'])]
    app=Starlette(routes=routes,lifespan=lifespan,middleware=[Middleware(SecurityHeaders)],exception_handlers={ViewerError:error,HTTPException:error,TimeoutError:error,OSError:error,sqlite3.Error:error})
    app.state.service=service
    return app
