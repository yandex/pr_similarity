'use strict';
(() => {
  const create = (tag, text) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n;};
  const control = (text, callback) => {const n = create('button', text); n.type = 'button'; n.onclick = callback; return n;};
  let version = 0, pollTimer = null, resourceTimer = null;
  window.addEventListener('pagehide', () => {++version; clearTimeout(pollTimer); clearTimeout(resourceTimer);});
  window.viewerRenderSettings = async (refreshResources = true) => {
    clearTimeout(pollTimer); clearTimeout(resourceTimer);
    const current = ++version, content = document.getElementById('content'); content.className = 'admin-page settings-page'; content.replaceChildren();
    document.getElementById('page-kind').textContent = 'Settings';
    if (window.viewerAccount.actor?.role !== 'admin') {content.append(create('p', 'Administrator access required.')); return;}
    try {
      const [layout, stats] = await Promise.all([window.viewerRequest('/admin/baskets/layout'), window.viewerRequest('/admin/stats')]);
      if (current !== version || window.viewerAccount.actor?.role !== 'admin') return;
      content.append(create('h1', 'Settings'));
      const totals = create('p');totals.className='settings-totals';content.append(totals);
      const message = create('p');message.className='settings-message';message.setAttribute('role','status');
      const operations = create('section');operations.className='settings-operations';
      const activityHeader=create('div');activityHeader.className='settings-section-heading';
      const activityTitle=create('div');activityTitle.append(create('h2','Activity'),create('p','Uploads, archive backups and catalogue refresh.'));activityHeader.append(activityTitle);
      const progress=create('div');progress.id='refresh-progress';
      const summary=create('div');summary.className='activity-summary';summary.setAttribute('role','status');
      const activityTable=create('table');activityTable.className='admin-table activity-table';
      const activityFooter=create('p');activityFooter.className='activity-footer';
      progress.append(summary,activityTable,activityFooter);operations.append(activityHeader,progress);
      const isCurrent=()=>current===version&&progress.isConnected&&window.viewerAccount.actor?.role==='admin';
      const humanTime=value=>value?new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'Never';
      const bytes=value=>{if(value==null)return '—';const units=['B','KiB','MiB','GiB','TiB'];let i=0;while(value>=1024&&i<units.length-1){value/=1024;i++;}return value.toLocaleString(undefined,{maximumFractionDigits:1})+' '+units[i];};
      const duration=value=>value==null?'—':value<60?Math.floor(value)+'s':Math.floor(value/60)+'m '+Math.floor(value%60)+'s';
      function realRows(value){
        const labels={receiving:'Receiving',extracting:'Extracting',validating:'Validating',committing:'Saving',pending:'Queued',uploading:'Backing up',retry_wait:'Retry scheduled',blocked:'Needs attention',running:'Running',retrying:'Retrying',waiting_recovery:'Waiting for recovery',partial:'Needs attention',failed:'Failed',cancelled:'Interrupted'};
        return (value.activity?.items||[]).map(job=>{
          let detail=job.phase?.replaceAll('_',' ')||'',percent=null,extra=job.error_code||'';
          if(job.kind==='upload'){
            detail=job.status==='receiving'?bytes(job.received_bytes)+(job.total_bytes!=null?' of '+bytes(job.total_bytes):' received'):detail;
            if(job.status==='receiving'&&job.total_bytes>0)percent=Math.min(100,100*job.received_bytes/job.total_bytes);
          }else if(job.kind==='backup'){
            detail=job.status==='retry_wait'?'Retry in '+duration(Math.max(0,job.retry_at-Date.now()/1000)):job.archive_bytes!=null?'Archive · '+bytes(job.archive_bytes):detail;
            if(job.backup_kind==='tombstone')detail='Deletion backup · '+detail;
            if(job.status==='blocked')extra+=' Retry requires operator attention.';
          }else if(job.kind==='refresh'){
            detail=job.status==='partial'?'Catalogue refresh completed with errors or deferred runs.':job.total?`${job.checked||0} of ${job.total} runs checked · ${job.errors||0} errors`:'Discovering archive backups';
            if(job.total>0)percent=Math.min(100,100*(job.checked||0)/job.total);
            if(['partial','failed','cancelled'].includes(job.status))extra+=' Use Refresh caches to retry.';
            for(const active of job.active||[])extra+=' '+Object.entries(active).map(([key,value])=>key.replaceAll('_',' ')+': '+value).join(' · ');
          }
          return {key:job.kind+':'+(job.id||job.run_id||'refresh'),kind:{upload:'Upload',backup:'Backup',refresh:'Refresh'}[job.kind]||job.kind,owner:job.actor||job.owner||'',name:job.name,context:job.basket||'',status:labels[job.status]||job.status,tone:['blocked','failed','partial'].includes(job.status)?'error':['pending','retry_wait','waiting_recovery','cancelled'].includes(job.status)?'waiting':'active',detail,percent,time:duration(job.elapsed_seconds),id:job.run_id,link:job.run_available!==false,extra};
        });
      }
      function showStats(value){
        totals.textContent=`${value.baskets} baskets · ${value.runs} runs · ${value.users} users`;
        const openDetails=new Set(Array.from(activityTable.querySelectorAll('details[open]'),node=>node.dataset.key));
        const rows=realRows(value);summary.replaceChildren();activityTable.replaceChildren();
        for(const [kind,label] of [['Upload','uploads'],['Backup','backups'],['Refresh','refresh']]){
          const count=rows.filter(r=>r.kind===kind).length;if(count){const chip=create('span',`${count} ${label}`);chip.className='activity-chip';summary.append(chip);}
        }
        if(['failed','retrying'].includes(value.recovery?.status)){
          summary.append(create('span','Interrupted write: '+value.recovery.status+(value.recovery.error_code?' · '+value.recovery.error_code:'')+'. Use Refresh caches to retry.'));
        }
        if(!rows.length){if(!summary.childElementCount)summary.append(create('span','No active operations'));activityTable.hidden=true;}
        else{
          activityTable.hidden=false;const thead=create('thead'),head=create('tr');
          for(const label of ['Operation','Run / scope','Status','Progress','Elapsed']){const cell=create('th',label);cell.scope='col';head.append(cell);}thead.append(head);activityTable.append(thead);
          const body=create('tbody');activityTable.append(body);
          for(const item of rows){
            const row=create('tr');row.className='activity-'+item.tone;
            const kind=create('td');kind.dataset.label='Operation';kind.append(create('strong',item.kind));const owner=create('small',item.owner||'');kind.append(owner);
            const identity=create('td');identity.dataset.label='Run / scope';const title=create(item.id&&item.link?'a':'strong',item.name);if(item.id&&item.link)title.href='/run?run_id='+encodeURIComponent(item.id);identity.append(title,create('small',item.context||''));
            const status=create('td');status.dataset.label='Status';const badge=create('span',item.status);badge.className='activity-status '+item.tone;status.append(badge);
            const detail=create('td');detail.dataset.label='Progress';detail.append(create('span',item.detail));
            if(item.percent!=null){const meter=create('progress');meter.max=100;meter.value=item.percent;meter.setAttribute('aria-label',item.kind+' progress');detail.append(meter);}
            if(item.extra||item.id){const disclosure=create('details');disclosure.className='activity-details';disclosure.dataset.key=item.key;disclosure.open=openDetails.has(item.key);disclosure.append(create('summary','Details'));if(item.extra)disclosure.append(create('p',item.extra));if(item.id)disclosure.append(create('code',item.id));detail.append(disclosure);}
            const elapsed=create('td',item.time);elapsed.className='activity-elapsed';elapsed.dataset.label='Elapsed';row.append(kind,identity,status,detail,elapsed);body.append(row);
          }
        }
        activityFooter.textContent=`${value.backup_jobs?.backed_up||0} backups complete · Last successful refresh: ${humanTime(value.last_refresh_at)}`;
        if(value.activity?.total>rows.length)activityFooter.textContent+=' · Showing '+rows.length+' of '+value.activity.total+' operations';
        clearTimeout(pollTimer);pollTimer=setTimeout(pollStats,2000);
      }
      async function pollStats(){if(!isCurrent())return;try{const value=await window.viewerRequest('/admin/stats');if(isCurrent())showStats(value);}catch(error){if(isCurrent()){summary.replaceChildren(create('span',error.message),control('Retry progress',pollStats));pollTimer=setTimeout(pollStats,5000);}}}
      showStats(stats);
      const resources=create('section');resources.className='settings-storage';
      const resourceDetails=create('div');resources.append(resourceDetails);
      function showResources(snapshot){
        resourceDetails.replaceChildren();
        const heading=create('div');heading.className='settings-section-heading';const label=create('div');label.append(create('h2','Server & storage'),create('p','Updated when settings are loaded or reloaded.'));heading.append(label);resourceDetails.append(heading);
        const grid=create('div');grid.className='storage-cards';resourceDetails.append(grid);
        function card(title,used,total,note,target=grid,showCapacity=true){const card=create('div');card.className='storage-card';card.append(create('h3',title));const amount=create('div');amount.className='storage-amount';amount.append(create('strong',bytes(used)));if(total)amount.append(create('span',' / '+bytes(total)));card.append(amount);if(total&&used!=null){const meter=create('progress');meter.max=total;meter.value=used;meter.setAttribute('aria-label',title+' used');card.append(meter);}else if(showCapacity){const absent=create('div',total?'':'Capacity not reported');absent.className='storage-no-limit';card.append(absent);}if(note)card.append(create('p',note));target.append(card);}
        const {cache,disk,memory,cpu,object_storage:s3}=snapshot;
        if(cache)card('Local run cache',cache.used_bytes,cache.limit_bytes,cache.resident_versions+' resident versions · '+(cache.idle_ttl_seconds/3600).toLocaleString(undefined,{maximumFractionDigits:1})+'h idle retention');
        if(disk)card('Server disk',disk.used_bytes,disk.total_bytes,bytes(disk.free_bytes)+' free');
        if(s3){
          const state=s3.status==='collecting'?'Collecting usage…':s3.status==='unavailable'?'Usage unavailable; reload to retry.':'';
          card('S3 storage',s3.used_bytes,s3.capacity_bytes,[s3.object_count!=null?s3.object_count.toLocaleString()+' current objects':'',state,s3.sampled_at?'As of '+humanTime(s3.sampled_at):''].filter(Boolean).join(' · '));
          if(Object.keys(s3.groups||{}).length){
            const breakdown=create('section');breakdown.className='storage-breakdown';breakdown.setAttribute('aria-label','S3 storage breakdown');
            breakdown.append(create('h3','S3 storage breakdown'));
            const categories=create('div');categories.className='storage-cards storage-category-cards';breakdown.append(categories);
            const labels={publications:'Publications',archives:'Run backups',backups:'Backup copies',trash:'Trash',indexes:'Checkpoints and indexes',other:'Other'};
            for(const [key,size] of Object.entries(s3.groups))card(labels[key]||key,size,null,null,categories,false);
            resourceDetails.append(breakdown);
          }
        }
        if(memory)card('Server memory',memory.used_bytes,memory.total_bytes,cpu?'CPU load '+cpu.load_1m.toFixed(2)+(cpu.cores?' · '+cpu.cores+' cores':''):'');
        else if(cpu)resourceDetails.append(create('p','CPU load '+cpu.load_1m.toFixed(2)));
        if(snapshot.sampled_at){const stamp=create('p','Server snapshot: '+humanTime(snapshot.sampled_at));stamp.className='storage-timestamp';resourceDetails.append(stamp);}
      }
      async function loadResources(refresh=false){
        if(!isCurrent())return;
        clearTimeout(resourceTimer);
        try{const value=await window.viewerRequest('/admin/resources'+(refresh?'?refresh=1':''));if(!isCurrent())return;showResources(value);if(value.object_storage?.status==='collecting')resourceTimer=setTimeout(()=>loadResources(),1000);}
        catch(error){if(isCurrent()){showResources({});resourceDetails.append(create('p',error.message));}}
      }
      const items = layout.items.map(item => ({...item}));
      let busy = false, drag = null, frame = null;
      const baskets=create('section');baskets.className='settings-baskets';baskets.append(create('h2','Baskets'));
      const hint=create('p','Drag baskets to reorder, or use ↑ / ↓ on a handle. Save to apply.');hint.id='basket-reorder-help';hint.className='admin-hint';baskets.append(hint);
      const table = create('table');table.className='admin-table basket-layout-table';table.setAttribute('aria-describedby',hint.id);table.setAttribute('aria-live','off');baskets.append(table);
      const save = control('Save basket layout', async () => {
        busy = true; cancelDrag(); save.disabled = true; reload.disabled = true; refresh.disabled = true; message.textContent = 'Saving…';
        for (const input of table.querySelectorAll('input,button')) input.disabled = true;
        try {
          await window.viewerRequest('/admin/baskets/layout', 'PUT', {expected_revision: layout.revision, items: items.map(({hash, hidden, collapsed}) => ({hash, hidden, collapsed}))});
          if (current === version) await window.viewerRenderSettings(false);
        } catch (error) {if (current === version) {message.textContent = error.message; reload.disabled = false;}}
      });
      const reload = control('Reload settings', () => window.viewerRenderSettings());
      const refresh = control('Refresh caches', async () => {
        refresh.disabled = true;
        message.textContent = 'Requesting a background catalogue refresh…';
        try {
          await window.viewerRequest('/admin/refresh', 'POST', {});
          if (isCurrent()) {message.textContent = 'Refresh requested.'; await pollStats();}
        } catch (error) {if (isCurrent()) message.textContent = error.message;}
        finally {if (isCurrent()) refresh.disabled = layoutDirty;}
      });
      let layoutDirty = false;
      function dirty() {layoutDirty = true; refresh.disabled = true; message.textContent = 'Unsaved layout. Save or reload before refreshing caches.';}
      function cancelDrag() {
        if(frame!=null)cancelAnimationFrame(frame);frame=null;
        for(const row of table.querySelectorAll('tr'))row.classList.remove('dragging','drop-before','drop-after');
        if(drag?.handle.hasPointerCapture(drag.pointerId))drag.handle.releasePointerCapture(drag.pointerId);
        drag=null;
      }
      function move(hash,target) {
        if(busy)return;
        const index=items.findIndex(item=>item.hash===hash);
        if(index<0||target<0||target>=items.length||index===target)return;
        const [item]=items.splice(index,1);items.splice(target,0,item);dirty();draw();
        message.textContent=item.name+' moved to position '+(target+1)+'. Save to apply.';
        table.querySelector('[data-basket-hash="'+hash+'"] .basket-drag-handle')?.focus({preventScroll:true});
      }
      function targetAtPointer() {
        if(!drag)return;
        for(const row of table.querySelectorAll('tr'))row.classList.remove('drop-before','drop-after');
        const target=document.elementFromPoint(drag.x,drag.y)?.closest('tr[data-basket-hash]');
        drag.target=target&&table.contains(target)?target.dataset.basketHash:null;
        if(drag.target&&drag.target!==drag.hash){
          const from=items.findIndex(v=>v.hash===drag.hash),to=items.findIndex(v=>v.hash===drag.target);
          target.classList.add(to<from?'drop-before':'drop-after');
        }
      }
      function autoScroll() {
        if(!drag?.active||busy||!table.isConnected||current!==version){cancelDrag();return;}
        const speed=drag.y<72?-12:drag.y>innerHeight-64?12:0;
        if(speed){window.scrollBy(0,speed);targetAtPointer();}
        frame=requestAnimationFrame(autoScroll);
      }
      function draw() {
        table.replaceChildren();const head=create('tr');head.className='admin-table-head';
        for(const title of ['Basket','Initially collapsed','Hide from home'])head.append(create('th',title));table.append(head);
        items.forEach(item=>{
          const row=create('tr');row.dataset.basketHash=item.hash;
          const name=create('td'),identity=create('div');identity.className='basket-layout-identity';
          const handle=control('⠿',()=>{});handle.className='basket-drag-handle';handle.setAttribute('aria-label','Reorder '+item.name);handle.setAttribute('aria-describedby',hint.id);handle.disabled=busy;
          handle.onkeydown=event=>{
            if(event.key==='Escape'){cancelDrag();return;}
            if(busy||drag)return;
            const index=items.findIndex(v=>v.hash===item.hash);
            const targets={ArrowUp:index-1,ArrowDown:index+1,Home:0,End:items.length-1};
            if(event.key in targets){event.preventDefault();move(item.hash,targets[event.key]);}
          };
          handle.onpointerdown=event=>{
            if(busy||drag||event.button!==0)return;
            event.preventDefault();handle.focus();drag={hash:item.hash,handle,pointerId:event.pointerId,startX:event.clientX,startY:event.clientY,x:event.clientX,y:event.clientY,active:false,target:null};handle.setPointerCapture(event.pointerId);
          };
          handle.onpointermove=event=>{
            if(!drag||drag.pointerId!==event.pointerId)return;
            drag.x=event.clientX;drag.y=event.clientY;
            if(!drag.active&&Math.hypot(drag.x-drag.startX,drag.y-drag.startY)>=5){drag.active=true;row.classList.add('dragging');frame=requestAnimationFrame(autoScroll);}
            if(drag.active){event.preventDefault();targetAtPointer();}
          };
          handle.onpointerup=event=>{
            if(!drag||drag.pointerId!==event.pointerId)return;
            if(drag.active)targetAtPointer();
            const source=drag.hash,target=drag.active?items.findIndex(v=>v.hash===drag.target):-1;cancelDrag();if(target>=0)move(source,target);
          };
          handle.onpointercancel=cancelDrag;
          const text=create('div');text.className='basket-layout-text';const title=create('strong',item.name);title.className='basket-layout-name';const description=create('p',item.description||'No description');description.className='basket-layout-description';text.append(title,description);
          identity.append(handle,text);name.append(identity);row.append(name);
          for(const key of ['collapsed','hidden']){
            const cell=create('td'),check=create('input');check.type='checkbox';check.checked=item[key];check.disabled=busy;check.setAttribute('aria-label',(key==='hidden'?'Hide ':'Collapse ')+item.name);
            check.onchange=()=>{if(!busy){item[key]=check.checked;dirty();}};cell.append(check);row.append(cell);
          }
          table.append(row);
        });
      }
      draw();const actions=create('div');actions.className='admin-actions';actions.append(save);baskets.append(actions,message);
      const resourceActions=create('div');resourceActions.className='admin-actions';resourceActions.append(reload);resources.append(resourceActions);
      content.append(resources,operations,baskets);activityHeader.append(refresh);loadResources(refreshResources);
    } catch (error) {if (current === version) content.append(create('p', error.message), control('Retry', () => window.viewerRenderSettings()));}
  };
})();
