"""Offline config and source-diff dialogs backed only by embedded report data."""

DIALOGS = '''
<dialog id="diff-dialog" class="diff-dialog" aria-labelledby="diff-title">
<header class="diff-dialog-header"><h2 id="diff-title" tabindex="0"></h2>
<button id="diff-close" type="button" aria-label="Close diff" autofocus>×</button></header>
<div id="diff-content" class="diff-content"></div></dialog>
<dialog id="config-dialog" aria-labelledby="config-title">
<header><h2 id="config-title">opencode.json</h2>
<button id="config-close" type="button" aria-label="Close config" autofocus>×</button></header>
<div id="config-content"></div></dialog>
'''

DIALOG_STYLE = '''
body.diff-open{overflow:hidden}
.diff-dialog{color:var(--text);background:var(--panel);border:1px solid var(--border2);
border-radius:12px;padding:0;width:92%;max-width:1400px;max-height:90dvh;overflow:hidden}
.diff-dialog[open]{display:flex;flex-direction:column}
dialog::backdrop{background:rgba(0,0,0,.75);backdrop-filter:blur(3px)}
.diff-dialog-header{display:flex;gap:12px;align-items:start;padding:12px 16px;
border-bottom:1px solid var(--border2);flex-shrink:0;background:var(--panel2)}
.diff-dialog-header h2{max-height:4.8em;overflow:auto;margin:0;flex:1;min-width:0;
font:600 13px/1.6 var(--mono);overflow-wrap:anywhere}
#diff-close{border:0;background:transparent;color:var(--text2);font-size:24px;padding:0 4px}
.diff-content{min-height:0;overflow:auto}
.diff-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px}
.diff-panel{min-width:0;border:1px solid var(--border2);border-radius:8px;overflow:hidden;background:var(--bg)}
.diff-panel h4{font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin:0;padding:8px 10px;
border-bottom:1px solid var(--border2);color:var(--text2);background:var(--panel2)}
.diff-panel pre{margin:0;padding:10px 0;overflow:auto;max-height:64vh;position:relative;
white-space:pre;font:12px/1.6 var(--mono)}
.diff-lines{display:grid;min-width:100%;width:max-content}.diff-lines>span{padding:0 10px}
.diff-add{color:#3fb950;background:rgba(63,185,80,.15)}
.diff-remove{color:#f85149;background:rgba(248,81,73,.12)}
.diff-hunk{color:#94bbf5}.diff-header{color:var(--text2);font-weight:600}
.file-action{color:var(--accent);font:11px system-ui,sans-serif;white-space:nowrap}
.run-actions{margin-bottom:12px}.benchmark-metadata{margin:16px 0;color:var(--text2);font-size:12px}
#config-dialog{width:min(60rem,90vw);max-height:85vh;border:1px solid var(--border2);
border-radius:8px;background:var(--panel);color:var(--text)}
#config-dialog header{display:flex;justify-content:space-between;align-items:center;gap:1rem}
#config-content pre{white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;max-height:60vh;
padding:1rem;background:var(--bg)}
@media(max-width:650px){.diff-dialog{width:calc(100% - 20px)}.diff-dialog-header{padding:10px}
.diff-grid{grid-template-columns:1fr;gap:8px;padding:8px}.diff-panel pre{max-height:32dvh}}
'''

DIALOG_SCRIPT = '''
{
const data=JSON.parse(document.querySelector('#view-data').textContent);
const node=(tag,text,cls)=>{const value=document.createElement(tag);
  if(text!=null)value.textContent=text;if(cls)value.className=cls;return value;};
function dialogState(id){
  const dialog=document.querySelector('#'+id+'-dialog');let opener=null,position=null;
  document.querySelector('#'+id+'-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{document.body.classList.remove('diff-open');
    opener?.focus({preventScroll:true});if(position)window.scrollTo(position.x,position.y);});
  const outside=e=>{const box=dialog.getBoundingClientRect();return e.target===dialog&&
    (e.clientX<box.left||e.clientX>box.right||e.clientY<box.top||e.clientY>box.bottom);};
  let backdrop=false;dialog.addEventListener('pointerdown',e=>{backdrop=outside(e);});
  dialog.addEventListener('pointerup',e=>{if(backdrop&&outside(e))dialog.close();backdrop=false;});
  return button=>{opener=button;position={x:scrollX,y:scrollY};document.body.classList.add('diff-open');
    dialog.showModal();};
}
const showDiff=dialogState('diff'),showConfig=dialogState('config');
function diffBlock(text,title){
  const pre=node('pre');pre.tabIndex=0;pre.setAttribute('aria-label',title);
  const lines=node('span',null,'diff-lines');let inHunk=false,block=null,previous=null;
  for(const line of text.match(/[^\\n]*\\n|[^\\n]+$/g)||[]){
    if(line.startsWith('@@'))inHunk=true;
    const kind=line.startsWith('@@')?'diff-hunk':
      !inHunk&&(line.startsWith('+++')||line.startsWith('---'))?'diff-header':
      line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':'';
    if(kind!==previous||!block){block=node('span',null,kind);lines.append(block);previous=kind;}
    block.append(document.createTextNode(line));
  }
  pre.append(lines);return pre;
}
document.querySelector('#task-list').addEventListener('click',event=>{
  const button=event.target.closest('button.file');if(!button||button.disabled)return;
  const index=button.closest('.task').dataset.index,fileIndex=button.dataset.fileIndex;
  const task=report.tasks[Number(index)],file=task.scoring.files[Number(fileIndex)];
  const diffs=data.diffs?.[index]?.[fileIndex];if(!diffs)return;
  document.querySelector('#diff-title').textContent=task.repository+' #'+task.number+' · '+file.path;
  const grid=node('div',null,'diff-grid');
  for(const [key,title] of [['human_diff','Human diff'],['agent_diff','Agent diff']]){
    const panel=node('section',null,'diff-panel');panel.append(node('h4',title));
    panel.append(diffBlock(diffs[key]||'(No content diff)',title));grid.append(panel);
  }
  document.querySelector('#diff-content').replaceChildren(grid);showDiff(button);
  requestAnimationFrame(()=>{for(const pre of grid.querySelectorAll('pre')){
    const first=pre.querySelector('.diff-add,.diff-remove');
    if(first)pre.scrollTop=Math.max(0,first.offsetTop-28);
  }});
});
document.querySelector('#show-config')?.addEventListener('click',event=>{
  const content=document.querySelector('#config-content'),config=data.config;
  content.replaceChildren();
  if(config?.supported){
    content.append(node('p','OpenCode input. Resource paths are relative to the agent directory in the run archive.'));
    content.append(node('pre',JSON.stringify(config.config,null,2)));
    const source=node('details');source.append(node('summary','Source details'),
      node('p',config.source.path+' · SHA-256: '+config.source.sha256));content.append(source);
  }else content.append(node('p','Config unavailable ('+(config?.reason||'snapshot_unavailable')+').'));
  showConfig(event.currentTarget);
});
const settings=data.benchmark,section=document.querySelector('#benchmark-settings');
if(settings&&section){
  section.append(node('h2','Benchmark settings'));
  section.append(node('p','Agent timeout: '+duration(settings.timeout_seconds)+
    ' · Setup timeout: '+duration(settings.setup_timeout_seconds)));
  section.append(node('p','Retries: '+(settings.retries?.max_retries??'—')+' · Categories: '+
    (settings.retries?.categories?.join(', ')||'None')));
  section.append(node('p','Diff exclusions: '+(settings.diff_excludes?.join(', ')||'None')));
}
const source=document.querySelector('#task-source');
const context={'diff':'Diff','issue':'Issues','diff+issue':'Diff + issues'};
if(source)source.textContent='Task source: '+(data.task_source?.source==='issue'?'Issues':
  data.task_source?.source==='llm'?'LLM · '+(context[data.task_source.context]||'Mode unknown'):'—');
}
'''
