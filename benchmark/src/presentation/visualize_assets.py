"""Embedded offline run-page assets; measurements follow viewer selection semantics."""

STYLE = ''':root{
  color-scheme:dark;
--bg:#05070c;
--bg2:#080b12;
--panel:#0b0f17;
--panel2:#0e1420;
--elev:#111826;

  --border:#18222f;
--border2:#22303f;
--text:#e6f0f7;
--text2:#9fb2c2;
--muted:#5f7183;

  --green:#3fe08a;
--yellow:#f0cf4a;
--red:#ff5a6e;
--accent:#31d0f5;
--star:#f0cf4a;

  --neon-cy:#25e6d3;
--neon-vi:#a173ff;
--neon-pk:#ff5fd2;

  --mono:'SF Mono',Monaco,'Cascadia Code','JetBrains Mono',monospace;
font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,sans-serif;

}

*{box-sizing:border-box}
body{margin:0;
background:var(--bg);
color:var(--text)}

a{color:var(--accent);
text-decoration:none}
a:hover{text-decoration:underline}

button,input,select,.button-link{font:inherit;
background:var(--bg);
color:var(--text);
border:1px solid var(--border2);
border-radius:6px;
padding:6px 10px}

button{cursor:pointer}
button:hover,.button-link:hover{border-color:var(--accent)}
button:disabled{opacity:.45;
cursor:default}

:focus-visible{outline:2px solid var(--accent);
outline-offset:3px}
input[type=checkbox]{accent-color:var(--accent)}

label{display:flex;
flex-direction:column;
gap:4px;
color:var(--text2);
font-size:12px}
label input,label select{font-size:13px}

.topbar{display:flex;
flex-wrap:wrap;
gap:12px 20px;
align-items:center;
padding:12px 24px;
border-bottom:1px solid var(--border);
background:linear-gradient(180deg,var(--panel),var(--bg2));
position:sticky;
top:0;
z-index:40}

.brand{display:inline-flex;
align-items:center;
gap:9px;
font-size:20px;
font-weight:700;
color:var(--text);
letter-spacing:-.5px}

.controls,.basket,.run-header{background:linear-gradient(180deg,var(--panel),var(--bg2));
border:1px solid var(--border);
border-radius:12px;
margin-bottom:20px;
box-shadow:0 1px 0 rgba(255,255,255,.02) inset,0 10px 34px rgba(0,0,0,.25)}

.controls,.run-header{padding:16px}
.selectors,.sortbar{display:flex;
flex-wrap:wrap;
gap:14px;
align-items:end}
.selectors button{height:33px}

fieldset{margin:0;
border:0;
padding:0;
display:flex;
gap:12px}
legend{color:var(--text2);
font-size:12px;
margin-bottom:6px}

fieldset label{display:flex;
flex-direction:row;
align-items:center;
font-size:13px;
color:var(--text)}

.hint,.muted{color:var(--text2);
font-size:12px}
.hint{margin-bottom:0}
#counts{margin-left:auto;
color:var(--text2);
font-size:12px}

.tag-group{display:flex;
flex-wrap:wrap;
gap:4px;
min-width:80px;
max-width:190px}

.tag{border:1px solid var(--border2);
background:var(--panel2);
color:var(--text2);
border-radius:4px;
padding:2px 5px;
white-space:pre-wrap;
overflow-wrap:anywhere;
max-width:100%;
font-size:11px}

.run-header h1{font-size:24px;
margin:0 0 10px;
overflow-wrap:anywhere}
.run-header .metadata{display:flex;
gap:18px;
flex-wrap:wrap;
color:var(--text2);
margin-bottom:16px;
font-size:12px}

.stat-grid{display:grid;
grid-template-columns:repeat(auto-fit,minmax(145px,1fr));
gap:10px}

.stat{padding:12px;
border-radius:7px;
border:1px solid var(--border2);
background:var(--panel2)}
.stat strong{display:block;
font:600 23px/1.5 var(--mono)}
.stat .label{color:var(--text2);
font-size:11px}

.task{border:1px solid var(--border2);
background:var(--panel);
border-radius:8px;
margin:10px 0}
.task>summary{display:flex;
gap:12px;
padding:13px;
cursor:pointer;
align-items:center}
.task>summary .title{flex:1;
overflow-wrap:anywhere;
min-width:0}

.task>summary::before{content:'▸';
color:var(--text2)}
.task[open]>summary::before{content:'▾'}

.badge{border-radius:4px;
background:rgba(63,224,138,.12);
color:var(--green);
padding:3px 7px;
font-size:11px;
white-space:nowrap}
.badge.no-op,.badge.timeout{background:rgba(240,207,74,.12);
color:var(--yellow)}
.badge.fail{background:rgba(255,90,110,.12);
color:var(--red)}

.task-body{border-top:1px solid var(--border);
padding:14px}
.condition{white-space:pre-wrap;
overflow-wrap:anywhere;
font:13px/1.6 system-ui,sans-serif;
color:var(--text);
padding:14px;
background:var(--bg);
border-radius:5px}

.file{display:flex;
align-items:center;
gap:12px;
width:100%;
text-align:left;
background:var(--panel2);
border:1px solid var(--border2);
border-radius:5px;
margin:6px 0;
padding:10px;
font:12px/1.5 var(--mono)}
.file-path{flex:1;
min-width:0;
overflow-wrap:anywhere}
.file-action{color:var(--accent);
font:11px system-ui,sans-serif;
white-space:nowrap}

.run-header table{min-width:0;
margin-bottom:16px}
.score-high,.health-ok{color:var(--green)}
.score-medium,.health-warn{color:var(--yellow)}
.score-low,.health-fail{color:var(--red)}
.score-unknown{color:var(--text2)}

.stat.score-high,.stat.health-ok{border-color:rgba(63,224,138,.3)}
.stat.score-medium,.stat.health-warn{border-color:rgba(240,207,74,.3)}
.stat.score-low,.stat.health-fail{border-color:rgba(255,90,110,.3)}

.token-total{font:600 13px/1.5 var(--mono);
color:var(--text)}
.token-breakdown{font:10px/1.5 var(--mono);
color:var(--text2);
margin-top:3px}
.has-filter{border-color:var(--accent)}

.basket-info{border-left:3px solid var(--accent);
padding:4px 12px;
margin:10px 0 18px}
.basket-info h2{font-size:16px;
margin:0}
.basket-description{white-space:pre-wrap;
overflow-wrap:anywhere;
margin:6px 0}
.task-source{color:var(--text2);
font-size:12px;
margin:6px 0}

main{width:calc(100% - 64px);
padding:24px;
max-width:1600px;
margin:auto}

nav{display:flex;
gap:16px;
margin-left:auto}
.selectors,.task-controls{display:flex;
flex-wrap:wrap;
gap:14px;
align-items:end}

.task-controls{padding:16px}
.task-controls input{min-width:260px}
.hash{font:11px var(--mono);
color:var(--text2);
overflow-wrap:anywhere}

.run-tags{display:grid;
grid-template-columns:1fr 2fr;
gap:18px;
margin:16px 0}
.run-tags .tag-group{max-width:none;
margin-top:6px}

.task-metrics{font:11px var(--mono);
color:var(--text2)}
.numeric{font-variant-numeric:tabular-nums;
white-space:nowrap}

.error{color:var(--red)}
[hidden]{display:none!important}
h2{font-size:18px}
h3{font-size:14px}

@media(max-width:1000px){main{width:100%}
.stat-grid{grid-template-columns:repeat(3,minmax(0,1fr))}
}

@media(max-width:650px){main{padding:10px}
.topbar{padding:12px;
gap:12px}
.brand{font-size:17px}
.stat-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.task>summary{flex-wrap:wrap}
.task>summary .title{flex-basis:70%}
.task-metrics{width:100%}
.run-tags{grid-template-columns:1fr}
.task-controls>label{width:100%}
.task-controls input{min-width:0;
width:100%}
fieldset{flex-wrap:wrap}
.file{flex-wrap:wrap}
}

'''

SCRIPT = '''const report=JSON.parse(document.querySelector('#report-data').textContent);
const field=document.querySelector('#search');
const status=t=>t.infrastructure_error||t.processing_status!=='completed'?'fail':
  t.execution.status==='timeout'?'timeout':t.execution.status==='completed'?
  ({empty:'no-op',nonempty:'OK'}[t.changes.status]||'fail'):'fail';
const measured=values=>{const known=values.filter(v=>v!=null);return {
  amount:values.length&&known.length===values.length?known.reduce((a,b)=>a+b,0):null,
  subtotal:known.reduce((a,b)=>a+b,0),known:known.length,total:values.length};};
const scoreClass=v=>v==null?'score-unknown':v>=.75?'score-high':v>=.4?'score-medium':'score-low';
const format=(v,n=3)=>v==null?'—':Number(v).toFixed(n);
const covered=(m,n=4)=>m.amount!=null?format(m.amount,n):m.known?
  format(m.subtotal,n)+' (partial; '+m.known+'/'+m.total+')':'—';
const duration=v=>{if(v==null)return '—';const seconds=Math.round(v);
  if(v<60&&Math.round(v*10)/10<60)return format(v,1)+' s';
  const minutes=Math.floor(seconds/60),rest=seconds%60;
  return minutes<60?minutes+'m '+rest+'s':Math.floor(minutes/60)+'h '+minutes%60+'m '+rest+'s';};
const durationMeasured=m=>m.amount!=null?duration(m.amount):m.known?
  duration(m.subtotal)+' (partial; '+m.known+'/'+m.total+')':'—';
function aggregate(tasks,pr,file){
  const scored=tasks.filter(t=>t.scoring.status==='available').map(t=>t.scoring);
  const weight=s=>pr==='Mpr'?1:pr==='FCpr'?s.counts.human_files:s.counts.loc_weight;
  const denominator=scored.reduce((a,s)=>a+weight(s),0);
  const files=scored.reduce((a,s)=>a+s.counts.human_files+s.counts.agent_files,0);
  const durations=tasks.map(t=>t.execution.duration_seconds).filter(v=>v!=null).sort((a,b)=>a-b);
  const mid=Math.floor(durations.length/2);
  return {score:denominator?scored.reduce((a,s)=>a+s[file+'_mlp']*weight(s),0)/denominator:null,
    f1:!scored.length?null:files?2*scored.reduce((a,s)=>a+s.counts.matched_files,0)/files:1,
    median:!durations.length?null:durations.length%2?durations[mid]:(durations[mid-1]+durations[mid])/2,
    time:measured(tasks.map(t=>t.execution.duration_seconds)),
    cost:measured(tasks.map(t=>t.telemetry.api_equivalent?
      (t.telemetry.api_equivalent.status==='complete'?t.telemetry.api_equivalent.amount:null):
      t.telemetry.cost.amount))};
}
function card(name,value,note='',tone=''){
  const card=document.createElement('div');card.className='stat '+tone;card.title=note;
  const label=document.createElement('span');label.className='label';label.textContent=name;card.append(label);
  const amount=document.createElement('strong');amount.textContent=value;card.append(amount);
  document.querySelector('#stats').append(card);
}
function refresh(){
  const selected=[...document.querySelectorAll('#selection input:checked')].map(e=>e.value);
  const tasks=report.tasks.filter(t=>selected.includes(status(t)));
  const metric=aggregate(tasks,document.querySelector('#pr').value,document.querySelector('#file').value);
  document.querySelector('#stats').replaceChildren();
  card('Score',format(metric.score),'Weighted score over selected tasks with available scoring.',
    scoreClass(metric.score));
  card('F1 files',format(metric.f1),'File precision/recall F1',scoreClass(metric.f1));
  const timeNote='Known execution durations: '+metric.time.known+'/'+metric.time.total+
    '; setup and earlier attempts are excluded.';
  card('Median time',duration(metric.median),timeNote);
  card('Total Agent Time',durationMeasured(metric.time),timeNote);
  const wall=report.run.finished_at?(Date.parse(report.run.finished_at)-Date.parse(report.run.created_at))/1000:null;
  card('Wall Time',duration(wall!=null&&wall>=0?wall:null),
    'Elapsed creation to execution completion; unaffected by selection.');
  const repriced=tasks.filter(t=>t.telemetry.api_equivalent).length;
  const nativeZero=tasks.some(t=>!t.telemetry.api_equivalent&&t.telemetry.cost.amount===0);
  let note='Source: '+(repriced?'reprice ('+repriced+') ':'')+
    (tasks.length>repriced?'opencode ('+(tasks.length-repriced)+')':'')+'; known tasks '+
    metric.cost.known+'/'+metric.cost.total;
  if(nativeZero)note+='; Reported zero is not verified free usage or measured subscription API cost.';
  card('Cost, USD',covered(metric.cost,6),note);card('Selected tasks',tasks.length+'/'+report.tasks.length);
  document.querySelector('#tokens').textContent='Tokens (cache read / input / output / cache write / reasoning): '+
    ['cache_read','input','output','cache_write','reasoning'].map(k=>
      covered(measured(tasks.map(t=>t.telemetry.tokens[k])),0)).join(' / ')+
    ' · Total: '+covered(measured(tasks.map(t=>t.telemetry.tokens.total)),0);
  document.querySelector('#statuses').textContent=['OK','no-op','timeout','fail'].map(s=>
    s+': '+report.tasks.filter(t=>status(t)===s).length).join(' · ');
  const file=document.querySelector('#file').value;
  const order=document.querySelector('#task-sort').value;
  const directionControl=document.querySelector('#task-direction');
  directionControl.disabled=order==='ordinal';
  if(order==='ordinal')directionControl.value='asc';
  const direction=directionControl.value==='desc'?-1:1;
  const list=document.querySelector('#task-list');
  const rows=[...list.querySelectorAll('.task')];
  const value=row=>{const task=report.tasks[Number(row.dataset.index)];
    return order==='score'?task.scoring[file+'_mlp']:
      order==='time'?task.execution.duration_seconds:
      order==='total_tokens'?task.telemetry.tokens.total:Number(row.dataset.index);};
  rows.sort((a,b)=>{const x=value(a),y=value(b);
    return (x==null?(y==null?0:1):y==null?-1:direction*(x-y))||
      Number(a.dataset.index)-Number(b.dataset.index);}).forEach(row=>{
    const task=report.tasks[Number(row.dataset.index)];
    row.hidden=!row.textContent.toLowerCase().includes(field.value.toLowerCase());
    row.querySelector('.task-metrics').textContent='Total Tokens: '+
      (task.telemetry.tokens.total==null?'—':task.telemetry.tokens.total.toLocaleString())+
      ' · Agent Time: '+duration(task.execution.duration_seconds);
    const score=row.querySelector('.task-score');const amount=task.scoring[file+'_mlp'];
    score.textContent=format(amount);score.className='task-score numeric '+scoreClass(amount);
    list.append(row);
  });
  const visible=rows.filter(row=>!row.hidden).length;
  document.querySelector('#tasks').textContent='Tasks ('+report.tasks.length+')';
  document.querySelector('#task-count').textContent=visible+' shown / '+report.tasks.length+' tasks';
}
field.addEventListener('input',refresh);
document.querySelector('#selection').addEventListener('change',refresh);
document.querySelector('#task-sort').addEventListener('change',refresh);
document.querySelector('#task-direction').addEventListener('change',refresh);
document.querySelector('#task-order-reset').addEventListener('click',()=>{
  document.querySelector('#task-sort').value='ordinal';refresh();});
refresh();
'''
