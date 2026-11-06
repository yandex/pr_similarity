'use strict';
(() => {
  const data = JSON.parse(document.getElementById('local-data').textContent);
  const statuses = ['OK', 'no-op', 'timeout', 'fail'];
  const copy = value => JSON.parse(JSON.stringify(value));
  const identity = {report_sha256: data.run.report_sha256, publication_sha256: data.run.publication_sha256};
  const taskRow = (task, file) => ({...task, ...task.metrics[file]});
  const unavailable = () => Object.fromEntries(['human','agent'].map(role => [role, {status:'unavailable',added:null,removed:null,files:null,measured_files:0}]));
  async function read(path, options = {}) {
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (options.method && options.method !== 'GET') throw new Error('This report is read-only');
    const url = new URL(path, 'https://offline.invalid');
    const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent), query = url.searchParams;
    if (parts[0] !== 'runs' || parts[1] !== data.run.run_id) throw new Error('Run is not embedded in this report');
    const file = query.get('file') || 'LCf', pr = query.get('pr') || 'LCpr';
    if (!['Mf','LCf'].includes(file) || !['Mpr','FCpr','LCpr'].includes(pr)) throw new Error('Invalid metric selection');
    if (parts.length === 2) {
      const selected = query.has('statuses') ? query.get('statuses').split(',').filter(Boolean) : ['OK','no-op','timeout'];
      const mask = statuses.reduce((value,status,index) => value | (selected.includes(status) ? 1 << index : 0), 0);
      const metrics = copy(data.aggregates[mask]);
      metrics.score = metrics.scores[pr+'/'+file]; delete metrics.scores;
      metrics.selection = {statuses: statuses.filter(s => selected.includes(s)), pr_aggregation:pr,file_aggregation:file};
      return {...copy(data.run), ...metrics};
    }
    if (parts[2] === 'config') return copy(data.config);
    if (parts[2] !== 'tasks') throw new Error('View is not embedded in this report');
    if (parts.length === 3) {
      const sort = query.get('sort') || 'score', direction = query.get('direction') || 'desc';
      const rows = data.tasks.map(task => taskRow(task, file));
      const value = row => sort === 'total_tokens' ? (row.tokens.total.amount ?? ((row.tokens.total.known_tasks || row.tokens.total.partial_tasks) ? row.tokens.total.known_subtotal : null)) : sort === 'time' ? row.time.total_seconds : row.score;
      if (sort !== 'task_order') rows.sort((a,b) => {
        const x=value(a),y=value(b);
        if (x==null || y==null) return Number(x==null)-Number(y==null);
        return (x-y)*(direction==='asc'?1:-1);
      });
      const offset = Number(query.get('offset') || 0), limit = Number(query.get('limit') || 50);
      return {...identity,items:copy(rows.slice(offset,offset+limit)),total:rows.length,offset,limit,sort,direction};
    }
    const task = data.tasks.find(item => item.task_id === parts[3]);
    if (!task) throw new Error('Task is not embedded in this report');
    if (parts.length === 4) return query.get('view') === 'changes' ? copy(data.changes[task.task_id] || unavailable()) : copy(taskRow(task,file));
    const selected = task.files.find(item => item.file_id === parts[5]);
    const views = query.get('view') === 'full' ? data.file_views : data.file_diffs;
    const view = selected && views[task.task_id]?.[selected.path];
    if (parts[4] !== 'files' || !view) throw new Error('File contents are unavailable in this report');
    return copy(view);
  }
  const search = document.createElement('input');search.type='search';search.id='local-search';search.placeholder='PR, status, path';
  const label=document.createElement('label');label.textContent='Filter tasks';label.append(search);
  document.getElementById('task-sorting').prepend(label);
  function filter() {
    const text=search.value.trim().toLowerCase();
    for(const element of document.querySelectorAll('.task')) {
      const task=data.tasks.find(row=>row.task_id===element.dataset.taskId);
      const haystack=[task.repository,task.number,task.title,task.status,task.condition,task.diagnostic,...task.files.map(file=>file.path)].join(' ').toLowerCase();
      element.hidden=Boolean(text)&&!haystack.includes(text);
    }
  }
  search.addEventListener('input',filter);
  window.prsimLocal = {runId:data.run.run_id,read,afterRender:filter};
  window.viewerAccount = {actor:null};
  window.viewerAccountReady = Promise.resolve();
  window.viewerRequest = () => Promise.reject(new Error('This report is read-only'));
  document.querySelector('.brand').setAttribute('aria-label','PR Similarity local report');
  document.getElementById('account').textContent = 'Local report';
})();
