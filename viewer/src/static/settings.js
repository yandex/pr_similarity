'use strict';
(() => {
  const create = (tag, text) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n;};
  const control = (text, callback) => {const n = create('button', text); n.type = 'button'; n.onclick = callback; return n;};
  let version = 0, pollTimer = null;
  window.addEventListener('pagehide', () => {++version; clearTimeout(pollTimer);});
  window.viewerRenderSettings = async () => {
    clearTimeout(pollTimer);
    const current = ++version, content = document.getElementById('content'); content.className = 'admin-page settings-page'; content.replaceChildren();
    document.getElementById('page-kind').textContent = 'Settings';
    if (window.viewerAccount.actor?.role !== 'admin') {content.append(create('p', 'Administrator access required.')); return;}
    try {
      const [layout, stats] = await Promise.all([window.viewerRequest('/admin/baskets/layout'), window.viewerRequest('/admin/stats')]);
      if (current !== version || window.viewerAccount.actor?.role !== 'admin') return;
      content.append(create('h1', 'Settings'));
      const totals = create('p'), lastRefresh = create('p'), progress = create('div');
      progress.setAttribute('role', 'status'); progress.id = 'refresh-progress';
      content.append(totals, lastRefresh, progress);
      const message = create('p'); message.setAttribute('role', 'status'); content.append(message);
      const activeStatuses = ['pending', 'running', 'retrying', 'waiting_recovery'];
      const isCurrent = () => current === version && progress.isConnected && window.viewerAccount.actor?.role === 'admin';
      function showStats(value) {
        totals.textContent = value.baskets + ' baskets · ' + value.runs + ' runs · ' + value.users + ' users';
        lastRefresh.textContent = 'Last successful refresh: ' + (value.last_refresh_at ? new Date(value.last_refresh_at).toLocaleString() : '—');
        progress.replaceChildren();
        const job = value.refresh_job;
        if (job) {
          const labels = {idle: 'No refresh requested.', pending: 'Refresh queued.', running: 'Refreshing catalogue in the background.', retrying: 'Storage unavailable; refresh will retry.', waiting_recovery: 'Refresh is waiting for an interrupted write.', succeeded: 'Catalogue refresh completed.', partial: 'Catalogue refresh completed with errors or deferred runs.', failed: 'Refresh failed. Use Refresh caches to retry.', cancelled: 'Refresh was interrupted. Use Refresh caches to retry.'};
          progress.append(create('p', labels[job.status] || 'Refresh status unavailable.'));
          const fields = ['request_id', 'completed_id', 'phase', 'boundary', 'discovered', 'total', 'checked', 'updated', 'protected', 'deferred', 'errors', 'bytes', 'error_code'];
          for (const key of fields) if (job[key] != null) progress.append(create('p', key.replaceAll('_', ' ') + ': ' + job[key]));
          for (const active of Array.isArray(job.active) ? job.active : []) {
            progress.append(create('p', ['run_id', 'generation', 'object', 'stage', 'last_progress'].filter(key => active[key] != null).map(key => key.replaceAll('_', ' ') + ': ' + active[key]).join(' · ')));
          }
        }
        if (value.recovery && ['failed', 'retrying'].includes(value.recovery.status)) progress.append(create('p', 'Interrupted write: ' + value.recovery.status + '. Use Refresh caches to retry.'));
        clearTimeout(pollTimer);
        if (activeStatuses.includes(job?.status)) pollTimer = setTimeout(pollStats, 1000);
      }
      async function pollStats() {
        if (!isCurrent()) return;
        try {
          const value = await window.viewerRequest('/admin/stats');
          if (isCurrent()) showStats(value);
        } catch (error) {
          if (isCurrent()) {
            progress.replaceChildren(create('p', error.message), control('Retry progress', pollStats));
          }
        }
      }
      showStats(stats);
      const items = layout.items.map(item => ({...item}));
      let busy = false, drag = null, frame = null;
      const hint=create('p','Drag baskets to reorder, or use ↑ / ↓ on a handle. Save to apply.');hint.id='basket-reorder-help';hint.className='admin-hint';content.append(hint);
      const table = create('table');table.className='admin-table basket-layout-table';table.setAttribute('aria-describedby',hint.id);table.setAttribute('aria-live','off');content.append(table);
      const save = control('Save basket layout', async () => {
        busy = true; cancelDrag(); save.disabled = true; reload.disabled = true; refresh.disabled = true; message.textContent = 'Saving…';
        for (const input of table.querySelectorAll('input,button')) input.disabled = true;
        try {
          await window.viewerRequest('/admin/baskets/layout', 'PUT', {expected_revision: layout.revision, items: items.map(({hash, hidden, collapsed}) => ({hash, hidden, collapsed}))});
          if (current === version) await window.viewerRenderSettings();
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
      draw();const actions=create('div');actions.className='admin-actions';actions.append(save,reload,refresh);content.append(actions);
    } catch (error) {if (current === version) content.append(create('p', error.message), control('Retry', () => window.viewerRenderSettings()));}
  };
})();
