'use strict';
(() => {
 const dialog=document.getElementById('config-dialog'),content=document.getElementById('config-content');
 const create=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
 const cache=new Map(); let version=0,opener=null;
 function load(row) {
  if(window.prsimLocal)return window.prsimLocal.read('/runs/'+encodeURIComponent(row.run_id)+'/config');
  const query=new URLSearchParams(); if(row.report_sha256)query.set('expected_report_sha256',row.report_sha256);if(row.publication_sha256)query.set('expected_publication_sha256',row.publication_sha256);
  const url='/api/v1/runs/'+encodeURIComponent(row.run_id)+'/config?'+query;
  if(!cache.has(url))cache.set(url,fetch(url).then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error?.code==='run_changed'?'Run changed; reload the page':data.error?.message||'Config request failed');return data;}).catch(e=>{cache.delete(url);throw e;}));
  return cache.get(url);
 }
 document.getElementById('config-close').onclick=()=>dialog.close();
 dialog.addEventListener('close',()=>{++version;content.replaceChildren();dialog.removeAttribute('aria-busy');const target=opener?.isConnected?opener:opener?.dataset.runId?document.querySelector('.config-button[data-run-id="'+CSS.escape(opener.dataset.runId)+'"]'):null;if(target)target.focus({preventScroll:true});opener=null;});
 function outside(e){const b=dialog.getBoundingClientRect();return e.target===dialog&&(e.clientX<b.left||e.clientX>b.right||e.clientY<b.top||e.clientY>b.bottom);}
 let backdrop=false;dialog.addEventListener('pointerdown',e=>backdrop=outside(e));dialog.addEventListener('pointerup',e=>{if(backdrop&&outside(e))dialog.close();backdrop=false;});
 window.viewerOpenConfig=async(row,button)=>{
  const current=++version;opener=button;const filename='opencode.json';
  document.getElementById('config-title').textContent=filename+' · '+row.display_name;
  content.replaceChildren(create('p','Loading config…'));dialog.setAttribute('aria-busy','true');if(!dialog.open)dialog.showModal();
  try {
   const data=await load(row);if(current!==version||!dialog.open)return;content.replaceChildren();
   if(!data.supported){content.append(create('p','Input config unavailable'));return;}
   const text=JSON.stringify(data.config,null,2)+'\n';
   const actions=create('div',undefined,'config-toolbar');const copy=create('button','Copy');copy.type='button';copy.onclick=async()=>{try{await navigator.clipboard.writeText(text);copy.textContent='Copied';}catch{copy.textContent='Copy failed';}};
   const download=create('button','Download');download.type='button';download.onclick=()=>{const url=URL.createObjectURL(new Blob([text],{type:'application/json'}));const a=create('a');a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);};
   actions.append(copy,download);content.append(actions,create('pre',text));
  }catch(error){if(current!==version||!dialog.open)return;const retry=create('button','Retry');retry.onclick=()=>window.viewerOpenConfig(row,button);content.replaceChildren(create('p',error.message),retry);}
  finally{if(current===version)dialog.removeAttribute('aria-busy');}
 };
 window.viewerLoadInputConfig=load;
 window.viewerShowInstructions=(text,button)=>{++version;opener=button;document.getElementById('config-title').textContent='Default task instructions';content.replaceChildren(create('pre',text));dialog.showModal();};
 window.viewerConfigButtons=(target,row)=>{
  const buttons=create('div',undefined,'config-toolbar');const effective=create('button','opencode.json','config-button');effective.type='button';effective.disabled=true;effective.dataset.runId=row.run_id;
  const status=create('span','Loading configs…','config-availability');buttons.append(effective,status);target.append(buttons);
  effective.onclick=()=>window.viewerOpenConfig(row,effective);
  async function prepare(){
   try{const data=await load(row);if(!buttons.isConnected)return;effective.disabled=!data.supported;status.textContent=data.supported?'':'Input config unavailable';
   }catch{if(!buttons.isConnected)return;status.textContent='Could not load configs';const retry=create('button','Retry');retry.onclick=()=>{retry.remove();status.textContent='Loading configs…';prepare();};buttons.append(retry);}
  }
  prepare();
 };
})();
