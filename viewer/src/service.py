"""Catalogue, UI metadata and durable single-process publication lifecycle."""

import os
import re
import shutil
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path

from viewer.basket import validate_basket_report
from viewer.contracts import require, validate_report
from viewer.database import LOGIN
from viewer.errors import StorageError, ViewerError
from viewer.inference import public_inference
from viewer.metrics import DEFAULT_STATUSES, STATUSES, aggregate, aggregate_snapshot, tag_groups
from viewer.presentation import basket_metadata, wall_time
from viewer.util import digest, json_bytes, parse_json, safe_path, utc_now, utc_timestamp


class ViewerService:
    def __init__(self,config,store,db):
        self.config,self.store,self.db=config,store,db
        self.lock=threading.RLock()
        self.runs,self.metadata,self.order,self.display={},{},[],{}
        self.last_refresh=None
        self.quarantined={}
        self._persisted_order=[]
        self._persisted_display={}
        self.staging=config.runtime/'staging';self.staging.mkdir(mode=0o700,parents=True,exist_ok=True)
        with self.lock:
            self.recover()
            self.cleanup_staging()
            self.refresh()

    def _json(self,key,default=None):
        try:return parse_json(self.store.get(key,self.config.limits.json_bytes))
        except ViewerError as e:
            if e.status==404:return deepcopy(default)
            raise

    def _validate_metadata(self,value):
        require(isinstance(value,dict),'invalid_metadata')
        for rid,row in value.items():
            require(isinstance(rid,str) and isinstance(row,dict),'invalid_metadata')
            require(set(row)=={'display_name','tags','pinned','created_at'},'invalid_metadata')
            self.validate_patch({k:row[k] for k in ('display_name','tags')})
            require(type(row['pinned']) is bool,'invalid_metadata')
            utc_timestamp(row['created_at'])

    def _marker(self,key):
        marker=self._json(key)
        require(isinstance(marker,dict),'invalid_publication')
        expected={'version','owner','run_id','basket_sha256','report_sha256','archive_sha256','archive_size','published_at','operation_id','inventory'}
        require(set(marker)==expected and type(marker['version']) is int and marker['version']==1,'invalid_publication')
        utc_timestamp(marker['published_at'])
        require(isinstance(marker['owner'],str) and LOGIN.fullmatch(marker['owner']),'invalid_publication')
        try:require(str(uuid.UUID(marker['run_id']))==marker['run_id'])
        except (ValueError,TypeError,AttributeError):raise ViewerError('invalid_publication','Invalid stored run identity.') from None
        prefix='runs/'+marker['owner']+'/'+marker['run_id']+'/'
        require(key==prefix+'publication.json','invalid_publication')
        for name in ('basket_sha256','report_sha256','archive_sha256'):
            require(isinstance(marker[name],str) and len(marker[name])==64 and all(c in '0123456789abcdef' for c in marker[name]),'invalid_publication')
        require(type(marker['archive_size']) is int and 0<=marker['archive_size']<=self.config.limits.archive_bytes)
        require(isinstance(marker['inventory'],list) and len(marker['inventory'])<=self.config.limits.files)
        for entry in marker['inventory']:
            require(isinstance(entry,dict) and set(entry)=={'path','size_bytes','sha256'})
            safe_path(entry['path'],self.config.limits.path_bytes)
            require(type(entry['size_bytes']) is int and 0<=entry['size_bytes']<=self.config.limits.file_bytes)
            require(isinstance(entry['sha256'],str) and len(entry['sha256'])==64 and all(c in '0123456789abcdef' for c in entry['sha256']))
        require(len({entry['path'] for entry in marker['inventory']})==len(marker['inventory']))
        report_bytes=self.store.get(prefix+'data/report.json',self.config.limits.json_bytes)
        basket_bytes=self.store.get(prefix+'data/basket.json',self.config.limits.json_bytes)
        require(digest(report_bytes)==marker['report_sha256'] and digest(basket_bytes)==marker['basket_sha256'],'stored_digest_mismatch')
        report,basket=parse_json(report_bytes),parse_json(basket_bytes)
        validate_report(report)
        validate_basket_report(basket, report)
        require('inference' in report,'invalid_report')
        try:inference=public_inference(report['inference'])
        except ValueError:raise ViewerError('invalid_report','Unsupported or invalid inference contract.') from None
        self._display_limits(report,basket)
        require(report['run']['id']==marker['run_id'] and report['basket']['sha256']==marker['basket_sha256'])
        require([t['id'] for t in report['tasks']]==[t['id'] for t in basket['tasks']])
        return {'marker':{k:v for k,v in marker.items() if k!='inventory'},'report':{'run':{k:report['run'][k] for k in ('id','status','created_at','tags')},'agent':{'name':report['agent']['name'],'runtime':report['agent']['runtime']},'basket':{'sha256':report['basket']['sha256']},'inference':inference},'auth_mode':self._auth_mode(prefix,report),'basket':{'meta':basket_metadata(basket)},'wall_time_seconds':wall_time(report['run']),'benchmark':self._benchmark_metadata(prefix,report),'prefix':prefix,'aggregates':aggregate_snapshot(report['tasks'])}

    def _auth_mode(self,prefix,report):
        """Read only a verified allowlisted access mode, without retaining private config."""
        ref=report['config']
        if ref['identity_version']!=1 or ref['identity_path']!='config_identity.json':return None
        try:identity=self._json(prefix+'data/config_identity.json')
        except ViewerError as error:
            if error.status==413:return None
            raise
        if identity is None:return None
        require(isinstance(identity,dict) and digest(json_bytes(identity))==ref['identity_sha256'],'stored_digest_mismatch')
        mode=identity.get('auth_mode')
        return mode if mode in ('api_key','chatgpt_oauth') else None

    def _benchmark_metadata(self,prefix,report):
        """Cache only verified frozen harness controls, without retaining raw config."""
        from viewer.config_view import projection
        identity=self._json(prefix+'data/config_identity.json')
        require(isinstance(identity,dict) and digest(json_bytes(identity))==report['config']['identity_sha256'], 'stored_digest_mismatch')
        try:return projection(identity,self.config.limits)
        except ValueError:return None

    def refresh(self,*,persist=True,_recover=True):
        with self.lock:
            # Recover pending writes first; refresh never promotes their partial prefixes.
            if _recover and self.db.operations():self.recover()
            runs={}
            quarantined={}
            protected_hashes=set()
            for key in self.store.list('runs/'):
                if not key.endswith('/publication.json'):continue
                try:
                    row=self._marker(key)
                except ViewerError as error:
                    if error.code not in ('invalid_report','invalid_basket','invalid_contract','invalid_public_artifact'):
                        raise
                    parts=key.split('/')
                    require(
                        len(parts)==4 and parts[0]=='runs' and LOGIN.fullmatch(parts[1]),'invalid_publication'
                    )
                    try:rid=str(uuid.UUID(parts[2]));require(rid==parts[2],'invalid_publication')
                    except (ValueError,TypeError,AttributeError):
                        raise ViewerError('invalid_publication','Invalid stored run identity.') from None
                    marker=self._json(key)
                    require(isinstance(marker,dict),'invalid_publication')
                    assert isinstance(marker,dict)
                    basket_hash=marker.get('basket_sha256')
                    if isinstance(basket_hash,str) and re.fullmatch(r'[0-9a-f]{64}',basket_hash):
                        protected_hashes.add(basket_hash)
                    quarantined[rid]={
                        'code':error.code,'prefix':'/'.join(parts[:3])+'/','basket_hash':basket_hash
                    }
                    continue
                rid=row['marker']['run_id']
                require(rid not in runs,'duplicate_run')
                owner=self.db.owner(rid)
                require(owner is None or owner=={'login':row['marker']['owner'],'basket_hash':row['marker']['basket_sha256']},'owner_conflict')
                runs[rid]=row
            metadata=self._json('settings/viewer-metadata.json',{})
            self._validate_metadata(metadata)
            metadata={rid:row for rid,row in metadata.items() if rid in runs or rid in quarantined}
            for rid,row in runs.items():
                metadata.setdefault(rid,{'display_name':row['report']['agent']['name'],'tags':row['report']['run']['tags'],'pinned':False,'created_at':row['report']['run']['created_at']})
            self._validate_metadata(metadata)
            order=self._json('settings/basket-order.json',[])
            require(isinstance(order,list) and all(isinstance(x,str) for x in order) and len(set(order))==len(order),'invalid_basket_order')
            assert isinstance(order,list)
            hashes={row['marker']['basket_sha256'] for row in runs.values()}
            stored_order=[h for h in order if h in hashes or h in protected_hashes]
            order=[h for h in stored_order if h in hashes]
            first={h:min(row['marker']['published_at'] for row in runs.values() if row['marker']['basket_sha256']==h) for h in hashes}
            order+=sorted(hashes-set(order),key=lambda h:(first[h],h))
            stored_display=self._json('settings/basket-display.json',{'version':1,'items':{}})
            require(isinstance(stored_display,dict) and set(stored_display)=={'version','items'} and type(stored_display['version']) is int and stored_display['version']==1,'invalid_basket_display')
            assert isinstance(stored_display,dict)
            self._validate_display(stored_display['items'])
            display={h:stored_display['items'].get(h,{'hidden':False,'collapsed':False}) for h in order}
            persisted_order=stored_order+[h for h in order if h not in stored_order]
            persisted_display={
                h:stored_display['items'].get(h,{'hidden':False,'collapsed':False}) for h in persisted_order
            }
            if persist:
                self.store.put('settings/viewer-metadata.json',json_bytes(metadata))
                self.store.put('settings/basket-order.json',json_bytes(persisted_order))
                self.store.put('settings/basket-display.json',json_bytes({'version':1,'items':persisted_display}))
            for rid,row in runs.items():self.db.reserve_owner(rid,row['marker']['owner'],row['marker']['basket_sha256'])
            self.runs,self.metadata,self.order=runs,metadata,order
            self.display=deepcopy(display);self.quarantined=quarantined;self.last_refresh=utc_now(self.db.clock())
            self._persisted_order=list(persisted_order);self._persisted_display=deepcopy(persisted_display)
            return {'runs':len(runs),'baskets':len(order),'quarantined':len(quarantined)}

    def _preserved_order(self, order: list[str]) -> list[str]:
        """Retain quarantined baskets in durable order during valid owner operations."""
        protected = {row['basket_hash'] for row in self.quarantined.values()}
        result = list(order)
        for index, basket_hash in enumerate(self._persisted_order):
            if basket_hash in protected and basket_hash not in result:
                result.insert(min(index, len(result)), basket_hash)
        return result

    def _preserved_display(self, display: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
        """Keep quarantined basket flags without exposing them as active UI settings."""
        protected = {row['basket_hash'] for row in self.quarantined.values()}
        result = {key: deepcopy(flags) for key, flags in self._persisted_display.items() if key in protected}
        result.update(deepcopy(display))
        return result

    def cleanup_staging(self):
        # Only our reserved upload/op directory names; never follow symlinks.
        referenced={op.get('staging') for op in self.db.operations().values()}
        for child in self.staging.iterdir():
            owned=bool(re.fullmatch(r'upload-[a-z0-9_]{8}',child.name))
            try:owned=owned or str(uuid.UUID(child.name))==child.name
            except ValueError:pass
            if owned and child.is_dir() and not child.is_symlink() and str(child) not in referenced:
                shutil.rmtree(child)

    def recover(self):
        deadline=time.monotonic()+self.config.limits.transfer_seconds
        for op_id,value in self.db.operations().items():self._execute(op_id,value,deadline=deadline)

    def _execute(self,op_id,op,*,deadline=None):
        def budget():
            if deadline is not None and time.monotonic()>deadline:
                raise StorageError()
        if op['kind']=='settings':
            budget();self.store.put('settings/basket-order.json',json_bytes(op['order']))
            budget();self.store.put('settings/basket-display.json',json_bytes({'version':1,'items':op['display']}))
            self.refresh(persist=False,_recover=False)
            self.db.finish_operation(op_id)
            return
        prefix=op['prefix']
        if op['phase']=='prepared':
            start=self.db.progress(op_id,'copied')
            for index,key in enumerate(op['old_keys'][start:],start+1):
                budget();self.store.copy(key,op['trash']+key[len(prefix):])
                self.db.set_progress(op_id,'copied',index)
            op['phase']='backed_up';self.db.save_operation(op_id,op)
        if op['phase'] in ('backed_up','installing') and op['kind']=='publish':
            current=self._json(prefix+'publication.json')
            if current and current.get('operation_id')==op_id:
                op['phase']='published';self.db.save_operation(op_id,op)
            elif not (Path(op['staging'])/'archive.tar.gz').is_file() or not (Path(op['staging'])/'data').is_dir():
                for key in list(self.store.list(prefix)):budget();self.store.delete(key)
                for old in op['old_keys']:budget();self.store.copy(op['trash']+old[len(prefix):],old)
                self.store.put('settings/viewer-metadata.json',json_bytes(op['previous_metadata']))
                self.store.put('settings/basket-order.json',json_bytes(op['previous_order']))
                self.refresh(persist=False,_recover=False)
                self.db.finish_operation(op_id)
                return
        if op['phase']=='backed_up':
            # Remove the marker first; partial data cannot become a publication.
            keys=[prefix+'publication.json',*[key for key in op['old_keys'] if key!=prefix+'publication.json']]
            start=self.db.progress(op_id,'removed')
            for index,key in enumerate(keys[start:],start+1):
                budget();self.store.delete(key);self.db.set_progress(op_id,'removed',index)
            op['phase']='installing';self.db.save_operation(op_id,op)
        if op['phase']=='installing':
            if op['kind']=='publish':
                root=Path(op['staging'])
                if not self.db.progress(op_id,'archive'):
                    budget();self.store.put_file(prefix+'archive.tar.gz',root/'archive.tar.gz',deadline=deadline)
                    self.db.set_progress(op_id,'archive',1)
                start=self.db.progress(op_id,'files')
                for index,entry in enumerate(op['marker']['inventory'][start:],start+1):
                    budget();self.store.put_file(prefix+'data/'+entry['path'],root/'data'/entry['path'],deadline=deadline)
                    self.db.set_progress(op_id,'files',index)
                budget();self.store.put(prefix+'publication.json',json_bytes(op['marker']))
            op['phase']='published';self.db.save_operation(op_id,op)
        if op['phase']=='published':
            budget();self.store.put('settings/viewer-metadata.json',json_bytes(op['metadata']))
            budget();self.store.put('settings/basket-order.json',json_bytes(op['order']))
            self.refresh(persist=False,_recover=False)
            # Empty baskets leave the active flags as well as the active order.
            budget();self.store.put('settings/basket-display.json',json_bytes({'version':1,'items':self._preserved_display(self.display)}))
            budget();self.db.finish_operation(op_id,op.get('owner'),op.get('key'),op.get('fingerprint'),op.get('receipt'))
            if op.get('staging'):shutil.rmtree(op['staging'],ignore_errors=True)

    def _require_run(self,rid):
        if rid not in self.runs:raise ViewerError('not_found','Run not found.',404)
        return self.runs[rid]

    def require_owner(self,rid,login):
        row=self._require_run(rid)
        if row['marker']['owner']!=login:raise ViewerError('foreign_run','Run belongs to another user.',403)
        return row

    def _display_limits(self,report,basket):
        limits=self.config.limits
        if (len(basket['meta']['name'])>limits.basket_name_chars or len(basket['meta']['description'])>limits.basket_description_chars
                or len(report['agent']['runtime'])>limits.runtime_chars or any(len(t['title'])>limits.task_title_chars for t in report['tasks'])):
            raise ViewerError('metadata_limit','Display metadata exceeds configured limits.',413)

    def publish(self,imported,login,key,*,overwrite=False,grant_token=None,deadline=None):
        with self.lock:
            require('inference' in imported.report,'invalid_report')
            try:public_inference(imported.report['inference'])
            except ValueError:raise ViewerError('invalid_report','Unsupported or invalid inference contract.') from None
            if self.db.operations():self.recover();self.refresh()
            self._display_limits(imported.report,imported.basket)
            rid=imported.report['run']['id'];basket=imported.report['basket']['sha256']
            fingerprint=digest(json_bytes({'run_id':rid,'archive_sha256':imported.archive_sha256,'report_sha256':imported.manifest['report_sha256'],'overwrite':overwrite}))
            previous=self.db.receipt(login,key)
            if previous:
                if previous['fingerprint']!=fingerprint:raise ViewerError('idempotency_conflict','Idempotency key belongs to another request.',409)
                if rid not in self.runs:
                    raise ViewerError('publication_removed','Original publication was deleted; submit a fresh publication operation.',404)
                return previous['receipt']
            reserved=self.db.owner(rid)
            if reserved and reserved['login']!=login:raise ViewerError('foreign_run','Run belongs to another user.',403)
            if reserved and reserved['basket_hash']!=basket:raise ViewerError('basket_conflict','Overwrite requires the same basket.',409)
            if rid in self.runs and not overwrite:raise ViewerError('run_exists','Run exists; use explicit overwrite.',409)
            self.validate_patch({'display_name':imported.report['agent']['name'],'tags':imported.report['run']['tags']})
            op_id=str(uuid.uuid4());root=self.staging/op_id;root.mkdir(mode=0o700)
            def link_or_copy(source,target):
                try:os.link(source,target)
                except OSError:shutil.copyfile(source,target)
                return target
            try:
                link_or_copy(imported.archive,root/'archive.tar.gz')
                shutil.copytree(imported.directory,root/'data',copy_function=link_or_copy)
            except Exception:
                shutil.rmtree(root,ignore_errors=True)
                raise
            for file in root.rglob('*'):
                if file.is_file():
                    with file.open('rb') as stream:os.fsync(stream.fileno())
            metadata=deepcopy(self.metadata)
            metadata.setdefault(rid,{'display_name':imported.report['agent']['name'],'tags':imported.report['run']['tags'],'pinned':False,'created_at':imported.report['run']['created_at']})
            order=list(self.order)
            if basket not in order:order.append(basket)
            prefix='runs/'+login+'/'+rid+'/'
            inventory=[*imported.manifest['files'], {'path':'manifest.json','size_bytes':(root/'data/manifest.json').stat().st_size,'sha256':digest((root/'data/manifest.json').read_bytes())}]
            marker={'version':1,'owner':login,'run_id':rid,'basket_sha256':basket,'report_sha256':imported.manifest['report_sha256'],'archive_sha256':imported.archive_sha256,'archive_size':imported.archive.stat().st_size,'published_at':utc_now(self.db.clock()),'operation_id':op_id,'inventory':inventory}
            receipt={'run_id':rid,'archive_sha256':imported.archive_sha256,'report_sha256':imported.manifest['report_sha256'],'idempotency_key':key,'author_login':login}
            op={'kind':'publish','phase':'prepared','prefix':prefix,'trash':'trash/'+login+'/'+rid+'/'+utc_now(self.db.clock()).replace(':','')+'-'+op_id+'/','old_keys':list(self.store.list(prefix)),'staging':str(root),'marker':marker,'metadata':metadata,'order':self._preserved_order(order),'previous_metadata':deepcopy(self.metadata),'previous_order':self._preserved_order(self.order),'owner':login,'key':key,'fingerprint':fingerprint,'receipt':receipt}
            try:
                self.db.begin_publication(op_id,op,grant_token)
            except Exception:
                try:durable=op_id in self.db.operations()
                except Exception:durable=True
                if not durable:shutil.rmtree(root,ignore_errors=True)
                raise
            self._execute(op_id,op,deadline=deadline)
            return receipt

    def delete(self,rid,*,actor=None,publication=None):
        with self.lock:
            if actor is not None:
                row=self._require_run(rid)
                if actor['role']!='admin' and row['marker']['owner']!=actor['login']:
                    raise ViewerError('foreign_run','Only the owner or an administrator can delete this run.',403)
                require(publication is not None,'invalid_query')
                self.check_report(rid,publication=publication)
            if self.db.operations():self.recover();self.refresh()
            if actor is not None:
                if rid not in self.runs:return {'run_id':rid,'deleted':True}
                self.check_report(rid,publication=publication)
            row=self._require_run(rid);owner=row['marker']['owner'];prefix=row['prefix'];op_id=str(uuid.uuid4())
            metadata=deepcopy(self.metadata);metadata.pop(rid)
            hashes={v['marker']['basket_sha256'] for k,v in self.runs.items() if k!=rid}
            op={'kind':'delete','phase':'prepared','prefix':prefix,'trash':'trash/'+owner+'/'+rid+'/'+utc_now(self.db.clock()).replace(':','')+'-'+op_id+'/','old_keys':list(self.store.list(prefix)),'metadata':metadata,'order':self._preserved_order([h for h in self.order if h in hashes])}
            self.db.save_operation(op_id,op);self._execute(op_id,op)
            return {'run_id':rid,'deleted':True}

    def validate_patch(self,patch):
        limits=self.config.limits
        require(isinstance(patch,dict) and bool(patch) and not set(patch)-{'display_name','tags'},'invalid_metadata')
        if 'display_name' in patch:require(isinstance(patch['display_name'],str) and bool(patch['display_name'].strip()) and len(patch['display_name'])<=limits.name_chars,'invalid_metadata')
        if 'tags' in patch:require(isinstance(patch['tags'],list) and len(patch['tags'])<=limits.tags and all(isinstance(t,str) and bool(t.strip()) and len(t)<=limits.tag_chars and all(ord(c)>=32 and ord(c)!=127 for c in t) for t in patch['tags']),'invalid_metadata')

    def patch_metadata(self,rid,login,patch):
        with self.lock:
            self.require_owner(rid,login);self.validate_patch(patch)
            metadata=deepcopy(self.metadata);metadata[rid].update(patch)
            self.store.put('settings/viewer-metadata.json',json_bytes(metadata));self.metadata=metadata
            return {'run_id':rid,**metadata[rid]}

    def set_pinned(self,rid,pinned):
        with self.lock:
            self._require_run(rid)
            if type(pinned) is not bool:raise ViewerError('invalid_pinned','pinned must be boolean.')
            metadata=deepcopy(self.metadata);metadata[rid]['pinned']=pinned
            self.store.put('settings/viewer-metadata.json',json_bytes(metadata));self.metadata=metadata
            return {'run_id':rid,'pinned':pinned}

    def set_order(self,order):
        with self.lock:
            self.set_settings(self.settings()['revision'],order,self.display)
            return {'items':list(order)}

    def _validate_display(self,display):
        require(isinstance(display,dict),'invalid_basket_display')
        for h,flags in display.items():
            require(isinstance(h,str) and re.fullmatch(r'[0-9a-f]{64}',h) is not None,'invalid_basket_display')
            require(isinstance(flags,dict) and set(flags)=={'hidden','collapsed'} and all(type(v) is bool for v in flags.values()),'invalid_basket_display')

    def settings(self):
        with self.lock:
            state={'order':list(self.order),'display':deepcopy(self.display)}
            items=[{'hash':h,'name':next(r['basket']['meta']['name'] for r in self.runs.values() if r['marker']['basket_sha256']==h),**self.display[h]} for h in self.order]
            return {'revision':digest(json_bytes(state)),**state,'items':items}

    def set_settings(self,revision,order,display):
        with self.lock:
            if self.db.operations():self.recover();self.refresh()
            if revision!=self.settings()['revision']:raise ViewerError('settings_changed','Settings changed; reload before saving.',409)
            require(isinstance(order,list) and all(isinstance(x,str) for x in order) and len(set(order))==len(order) and set(order)==set(self.order),'invalid_basket_order')
            self._validate_display(display)
            require(set(display)==set(order),'invalid_basket_display')
            # The published envelope remains consumable by the pre-layout v1
            # journal executor during rollback. It restores canonical order and
            # metadata; that older UI simply ignores display flags.
            op_id=str(uuid.uuid4());op={'kind':'settings','phase':'published','prefix':'settings/',
                'metadata':deepcopy(self.metadata),'order':self._preserved_order(order),'display':self._preserved_display(display)}
            self.db.save_operation(op_id,op);self._execute(op_id,op)
            return self.settings()

    def cache_stats(self):
        return {'runs':len(self.runs),'baskets':len(self.order),'users':self.db.connection.execute('SELECT COUNT(*) FROM users').fetchone()[0],'last_refresh_at':self.last_refresh,'refresh':'idle','quarantined_runs':len(self.quarantined)}

    def full_report(self,rid):
        row=self._require_run(rid)
        content=self.store.get(row['prefix']+'data/report.json',self.config.limits.json_bytes)
        require(digest(content)==row['marker']['report_sha256'],'stored_digest_mismatch')
        report=parse_json(content);validate_report(report)
        return report

    def check_report(self,rid,expected=None,publication=None):
        row=self._require_run(rid)
        for value,key in ((expected,'report_sha256'),(publication,'archive_sha256')):
            if value is not None:
                require(isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value) is not None,'invalid_query')
                if value!=row['marker'][key]:raise ViewerError('run_changed','Run changed; reload the run before continuing.',409)
        return row['marker']['report_sha256']

    def run(self,rid,**selection):
        with self.lock:
            row=self._require_run(rid);report=row['report'];metadata=deepcopy(self.metadata[rid])
            checked=aggregate([],**selection)['selection']
            mask=sum(1<<i for i,s in enumerate(STATUSES) if s in checked['statuses'])
            metrics=deepcopy(row['aggregates'][mask])
            scores=metrics.pop('scores');metrics['score']=scores[checked['pr_aggregation']+'/'+checked['file_aggregation']]
            metrics['selection']=checked
            return {'run_id':rid,'report_sha256':row['marker']['report_sha256'],'publication_sha256':row['marker']['archive_sha256'],'user':row['marker']['owner'],'auth_mode':row['auth_mode'],'basket_hash':report['basket']['sha256'],'basket':deepcopy(row['basket']['meta']),'wall_time_seconds':row['wall_time_seconds'],**metadata,'tag_groups':tag_groups(metadata['tags'],report['inference']),'inference':deepcopy(report['inference']),'runtime':report['agent']['runtime'],'status':report['run']['status'],'benchmark':deepcopy(row['benchmark']),**metrics}

    def download(self,rid,login):
        with self.lock:
            row=self.require_owner(rid,login)
            return self.store.open(row['prefix']+'archive.tar.gz')
