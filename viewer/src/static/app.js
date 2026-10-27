'use strict';

const $ = id => document.getElementById(id);
const node = (tag, text, className) => {
  const value = document.createElement(tag);
  if (text !== undefined && text !== null) value.textContent = text;
  if (className) value.className = className;
  return value;
};
const categories = ['cache_read', 'input', 'output', 'cache_write', 'reasoning'];
const statusLabels = {'OK': 'OK', 'no-op': 'No-op', 'timeout': 'Timeout', 'fail': 'Fail'};
const scoreClass = value => value === null || value === undefined ? 'score-unknown' : value >= .75 ? 'score-high' : value >= .4 ? 'score-medium' : 'score-low';
const taskClass = row => row.status_counts.fail ? 'health-fail' : row.status_counts['no-op'] || row.status_counts.timeout ? 'health-warn' : 'health-ok';
const statusSummary = row => Object.entries(row.status_counts).map(([s,c]) => statusLabels[s] + ': ' + c).join(' · ');
const groups = ['user', 'model', 'setup', 'basket'];
const runId = new URLSearchParams(location.search).get('run_id');
const detail = location.pathname === '/run';
const usersPage = location.pathname === '/admin/users';
const settingsPage = location.pathname === '/settings';
const basketExpanded = new Map();
const offsets = new Map();
const expandedTags = new Set();
const expandedTasks = new Set();
const filterWidgets = new Map();
let pinnedOnly = false;
let generation = 0;
let searchTimer;
let runReportHash = null;
let runPublicationHash = null;
let runAuthMode = null;
let pendingTaskFocus = null;

const number = (value, digits = 3) => value === null || value === undefined ? '—' : Number(value).toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits});
const money = value => number(value, 6);
const time = value => {
  if (value == null) return '—';
  const seconds = Math.round(value);
  if (value < 60 && Math.round(value * 10) / 10 < 60) return number(value, 1) + ' s';
  const minutes = Math.floor(seconds / 60), rest = seconds % 60;
  return minutes < 60 ? minutes + 'm ' + rest + 's' : Math.floor(minutes / 60) + 'h ' + minutes % 60 + 'm ' + rest + 's';
};
const displayedAmount = value => value.amount ?? (value.known_tasks ? value.known_subtotal : null);
const costTitle = row => 'Source: ' + (Object.entries(row.cost.sources).map(([source, count]) => source + ' (' + count + ')').join(' + ') || '—') + '\nKnown tasks: ' + row.cost.known_tasks + '/' + row.cost.total_tasks + '\nKnown subtotal: ' + money(row.cost.known_subtotal) + ' USD';
const tokensTitle = row => [...categories, 'total'].map(c => c.replace('_', ' ') + ': ' + row.tokens[c].known_tasks + '/' + row.tokens[c].total_tasks + ' known; subtotal ' + number(row.tokens[c].known_subtotal, 0)).join('\n');
const timeTitle = row => 'Known execution durations: ' + row.time.known_tasks + '/' + row.time.total_tasks + '\nExact median: ' + row.time.median_seconds + ' s; total: ' + row.time.total_seconds + ' s\nSetup and earlier attempts are excluded.';
const dateText = value => new Date(value).toLocaleString();
const dateTitle = value => value + '\nShown in ' + Intl.DateTimeFormat().resolvedOptions().timeZone;

let metricTip = null;
function metricNote(message, warning) {
  const control = button(warning ? '(!)' : 'ⓘ', event => {event.stopPropagation(); show();}, 'metric-note' + (warning ? ' warning' : ''));
  control.setAttribute('aria-label', message);
  function show() {
    if (!metricTip) {metricTip = node('div', null, 'metric-tooltip'); metricTip.id = 'metric-tooltip'; metricTip.setAttribute('role', 'tooltip'); document.body.append(metricTip);}
    metricTip.textContent = message; metricTip.hidden = false;
    control.setAttribute('aria-describedby', metricTip.id);
    const bounds = control.getBoundingClientRect();
    metricTip.style.left = Math.max(8, Math.min(bounds.left, innerWidth - metricTip.offsetWidth - 8)) + 'px';
    metricTip.style.top = Math.max(8, Math.min(bounds.bottom + 6, innerHeight - metricTip.offsetHeight - 8)) + 'px';
  }
  control.addEventListener('mouseenter', show); control.addEventListener('focus', show);
  for (const event of ['mouseleave', 'blur']) control.addEventListener(event, () => {if (metricTip) metricTip.hidden = true; control.removeAttribute('aria-describedby');});
  return control;
}
document.addEventListener('click', event => {if (metricTip && !event.target.closest('.metric-note')) metricTip.hidden = true;});
document.addEventListener('keydown', event => {if (event.key === 'Escape' && metricTip) metricTip.hidden = true;});

function measurement(value, format = v => number(v, 0), provenance = '', suspicious = false, basis = 'Known subtotal') {
  const result = node('span', format(displayedAmount(value)), 'measurement');
  const incomplete = value.total_tasks > value.known_tasks;
  const message = [incomplete ? basis + ': ' + value.known_tasks + ' of ' + value.total_tasks + ' selected tasks have known data. Measurement is incomplete.' : '', provenance].filter(Boolean).join('\n');
  if (message) result.append(metricNote(message, incomplete || suspicious));
  return result;
}
function tokensView(row) {
  const result = node('span');
  categories.forEach((category, index) => {if (index) result.append(' / '); result.append(measurement(row.tokens[category]));});
  return result;
}
function scoreView(row, field) {
  const total = row.selected_tasks ?? 1, known = row.scored_tasks ?? (row.scoring_status === 'available' ? 1 : 0);
  return measurement({amount: row[field], known_tasks: known, total_tasks: total}, v => number(v), '', false, 'Score over available tasks');
}
function durationView(row, median = false) {
  const values = row.time;
  const value = {amount: median ? values.median_seconds : values.total_seconds, known_subtotal: values.known_subtotal_seconds, known_tasks: values.known_tasks, total_tasks: values.total_tasks};
  const result = measurement(value, time, '', false, median ? 'Median over available tasks' : 'Known execution-time subtotal');
  result.title = timeTitle(row); return result;
}
function costView(row, compact = false) {
  const sources = row.cost.sources, mode = row.auth_mode ?? runAuthMode;
  const notes = [], mixed = Object.keys(sources).length > 1;
  if (sources.reprice) notes.push('API-equivalent estimate from the saved pricing policy; not a subscription bill.');
  if (sources.opencode) notes.push('Native cost reported by OpenCode.');
  if (mixed) notes.push('Mixed native and API-equivalent values; this is not a uniform pricing basis.');
  const nativeZero = row.cost.native_zero_tasks > 0;
  if (nativeZero && mode === 'chatgpt_oauth') notes.push('OpenCode reports zero for subscription access; zero is not measured API cost. API-equivalent pricing is absent for these native values.');
  else if (nativeZero && mode === 'api_key') notes.push('Reported cost is zero. Model pricing configuration may be missing or incorrect; this is not verified free usage.');
  if (sources.opencode && mode == null) notes.push('Access mode is unknown; it is not inferred from the model name.');
  const result = measurement(row.cost, v => v == null ? '—' : compact ? number(v, 2) + '$' : money(v), notes.join('\n'), mixed || (nativeZero && mode === 'api_key'));
  result.title = costTitle(row); return result;
}

function selection() {
  const query = new URLSearchParams();
  query.set('statuses', [...document.querySelectorAll('#statuses input:checked')].map(i => i.value).join(','));
  query.set('pr', $('pr').value);
  query.set('file', $('file').value);
  return query;
}
function filters() {
  const query = selection();
  for (const group of groups) for (const option of $('filter-' + group).selectedOptions) query.append(group, option.value);
  if ($('search').value) query.set('q', $('search').value);
  if (pinnedOnly) query.set('pinned', '1');
  for (const [id, key] of [['date-from', 'from'], ['date-to', 'to']]) {
    if ($(id).value) query.set(key, new Date($(id).value).toISOString());
  }
  query.set('sort', $('sort').value);
  query.set('direction', $('direction').value);
  return query;
}
async function api(path, options = {}) {
  const response = await fetch('/api/v1' + path, options);
  const data = await response.json();
  if (!response.ok) {const error = new Error(data.error?.message || 'Request failed (' + response.status + ')'); error.code = data.error?.code; throw error;}
  return data;
}
function showError(error) {
  $('error').textContent = error.message;
  $('error').hidden = false;
  if (detail && error.code === 'run_changed') $('error').append(button('Reload run', () => reload()));
  $('activity').textContent = 'Request failed';
}
function button(text, callback, className) {
  const value = node('button', text, className);
  value.type = 'button';
  value.addEventListener('click', callback);
  return value;
}
function pinButton(row) {
  const value = button(row.pinned ? '★' : '☆', async event => {
    event.stopPropagation();
    value.disabled = true;
    try {
      await window.viewerRequest('/runs/' + encodeURIComponent(row.run_id) + '/pinned', 'PUT', {pinned: !row.pinned});
      await reload();
    } catch (error) { showError(error); value.disabled = false; }
  }, 'pin');
  value.setAttribute('aria-label', (row.pinned ? 'Unpin ' : 'Pin ') + row.display_name);
  value.setAttribute('aria-pressed', String(row.pinned));
  value.disabled = window.viewerAccount.actor?.role !== 'admin';
  if (value.disabled) value.title = 'Only administrators can change pinned runs';
  return value;
}
function configButton(row) {
  const control = button('See Run Config', event => {event.stopPropagation(); window.viewerOpenConfig(row, control);}, 'config-button');
  control.setAttribute('aria-label', 'See Run Config for ' + row.display_name); control.dataset.runId = row.run_id; return control;
}
function tagsCell(row, group, previewLimit = 5) {
  const cell = node('td');
  const values = row.tag_groups[group];
  if (group !== 'setup') {
    const names = group === 'model' ? ['model', 'effort'] : [group];
    cell.title = names.map(name => {
      const fact = row.inference?.configured?.[name];
      return fact ? 'Configured ' + name + ': ' + fact.status + (fact.reason ? ' (' + fact.reason + ')' : '') + (fact.source ? '\nSource: ' + fact.source.ref + ' · ' + fact.source.field : '') : '';
    }).filter(Boolean).join('\n');
  }
  if (!values.length) { cell.textContent = group !== 'setup' && row.inference?.configured?.[group]?.status || '—'; return cell; }
  const key = row.run_id + ':' + group;
  const container = node('div', null, 'tag-group');
  function render() {
    container.replaceChildren();
    const expanded = expandedTags.has(key);
    for (const tag of values.slice(0, expanded ? values.length : previewLimit)) container.append(node('span', tag, 'tag'));
    if (values.length > previewLimit) {
      const control = button(expanded ? 'Collapse' : '+' + (values.length - previewLimit), event => {
        event.stopPropagation();
        if (expandedTags.has(key)) expandedTags.delete(key); else expandedTags.add(key);
        render();
      }, 'tag-more');
      control.setAttribute('aria-expanded', String(expanded));
      control.setAttribute('aria-label', (expanded ? 'Collapse' : 'Expand') + ' ' + group + ' tags for ' + row.display_name);
      container.append(control);
    }
  }
  render(); cell.append(container); return cell;
}
function numericCell(text, title, color = '') {
  const cell = node('td', text instanceof Node ? null : text, 'numeric ' + color);
  if (text instanceof Node) cell.append(text);
  if (title) cell.title = title;
  return cell;
}
function runRow(row, position) {
  const tr = node('tr', null, 'run-row');
  tr.dataset.runId = row.run_id;
  tr.tabIndex = 0;
  const destination = '/run?run_id=' + encodeURIComponent(row.run_id);
  tr.addEventListener('click', event => {
    if (!event.target.closest('button,a,input,select')) location.assign(destination);
  });
  tr.addEventListener('keydown', event => { if (event.key === 'Enter' && event.target === tr) location.assign(destination); });
  tr.append(numericCell(position));
  const pinned = node('td'); pinned.append(pinButton(row)); tr.append(pinned);
  const name = node('td');
  const link = node('a', row.display_name, 'run-name'); link.href = destination; link.title = row.run_id;
  name.append(link, node('div', row.user + ' · ' + row.runtime, 'run-meta'), node('div', row.status, 'run-meta')); tr.append(name);
  for (const group of ['model', 'setup']) tr.append(tagsCell(row, group));
  const date = node('td', dateText(row.created_at)); date.title = dateTitle(row.created_at); tr.append(date);
  tr.append(numericCell(scoreView(row, 'score'), 'Selected task score', scoreClass(row.score)), numericCell(scoreView(row, 'f1_files'), 'File precision/recall F1', scoreClass(row.f1_files)));
  tr.append(numericCell(row.selected_tasks + '/' + row.total_tasks, row.scored_tasks + ' selected tasks have a score\n' + statusSummary(row), taskClass(row)));
  tr.append(numericCell(durationView(row, true), timeTitle(row)));
  const tokens = numericCell('', tokensTitle(row));
  const total = node('div', 'Total: ', 'token-total'); total.append(measurement(row.tokens.total));
  const breakdown = node('div', null, 'token-breakdown'); breakdown.append(tokensView(row));
  tokens.append(total, breakdown); tr.append(tokens);
  tr.append(numericCell(costView(row, true), costTitle(row), 'run-cost'));
  return tr;
}
function createFilter(group) {
  const select = $('filter-' + group);
  const label = select.parentElement.firstChild.textContent.trim();
  const root = node('div', null, 'filter-control'); root.setAttribute('role', 'group');
  root.setAttribute('aria-labelledby', select.id + '-label');
  const caption = node('span', label, 'filter-label'); caption.id = select.id + '-label';
  const field = node('div', null, 'filter-field');
  const chips = node('div', null, 'filter-chips');
  const menu = node('div', null, 'filter-menu'); menu.id = select.id + '-menu'; menu.hidden = true;
  const search = node('input'); search.type = 'search'; search.placeholder = 'Search…';
  search.setAttribute('aria-label', 'Search ' + label + ' options');
  const options = node('div', null, 'filter-options');
  const toggle = button('Filter…', () => menu.hidden ? open() : close(false), 'filter-toggle');
  toggle.setAttribute('aria-label', 'Choose ' + label); toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', menu.id);
  function close(restoreFocus) {
    menu.hidden = true; toggle.setAttribute('aria-expanded', 'false');
    if (restoreFocus) toggle.focus();
  }
  function open() {
    for (const widget of filterWidgets.values()) widget.close(false);
    menu.hidden = false; toggle.setAttribute('aria-expanded', 'true'); search.focus();
  }
  function update(option, checked) {
    option.selected = checked;
    select.dispatchEvent(new Event('change', {bubbles: true}));
    render();
  }
  function renderOptions() {
    const focused = menu.contains(document.activeElement) ? document.activeElement.dataset.value : null;
    options.replaceChildren();
    const query = search.value.toLocaleLowerCase();
    for (const option of select.options) {
      if (!option.textContent.toLocaleLowerCase().includes(query)) continue;
      const row = node('label', null, 'filter-option');
      const check = node('input'); check.type = 'checkbox'; check.checked = option.selected;
      check.dataset.value = option.value;
      check.addEventListener('change', () => update(option, check.checked));
      row.append(check, node('span', option.textContent)); options.append(row);
    }
    if (!options.children.length) options.append(node('p', 'No matching values.', 'muted'));
    if (focused !== null) [...options.querySelectorAll('input')].find(i => i.dataset.value === focused)?.focus();
  }
  function render() {
    const focusWasChip = chips.contains(document.activeElement);
    chips.replaceChildren();
    for (const option of select.selectedOptions) {
      const chip = button(option.textContent + ' ×', () => update(option, false), 'filter-chip');
      chip.title = option.textContent; chip.setAttribute('aria-label', 'Remove ' + option.textContent + ' from ' + label);
      chips.append(chip);
    }
    toggle.textContent = select.selectedOptions.length ? '+' : 'Filter…';
    renderOptions();
    if (focusWasChip) toggle.focus();
  }
  search.addEventListener('input', renderOptions);
  root.addEventListener('keydown', event => {
    if (menu.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      const checks = [...options.querySelectorAll('input')];
      if (!checks.length) return;
      event.preventDefault();
      const index = checks.indexOf(document.activeElement);
      const next = index < 0 ? (event.key === 'ArrowDown' ? 0 : checks.length - 1) : (index + (event.key === 'ArrowDown' ? 1 : -1) + checks.length) % checks.length;
      checks[next].focus();
    }
  });
  document.addEventListener('pointerdown', event => { if (!root.contains(event.target)) close(false); });
  document.addEventListener('focusin', event => { if (!root.contains(event.target)) close(false); });
  field.append(chips, toggle); menu.append(search, options);
  select.parentElement.replaceWith(root);
  select.hidden = true; select.tabIndex = -1; select.setAttribute('aria-hidden', 'true');
  root.append(caption, select, field, menu);
  return {render, close, reset() {search.value = ''; close(false); render();}};
}
function populateFilters(options) {
  for (const group of groups) {
    const select = $('filter-' + group);
    const selected = new Set([...select.selectedOptions].map(v => v.value));
    select.replaceChildren();
    for (const item of options[group]) {
      const option = node('option', typeof item === 'string' ? item : item.name + ' [' + item.hash.slice(0, 8) + ']');
      option.value = typeof item === 'string' ? item : item.hash;
      option.selected = selected.has(option.value);
      select.append(option);
    }
    filterWidgets.get(group).render();
  }
}
async function allPages(path, query) {
  const result = [];
  let offset = 0;
  while (true) {
    const params = new URLSearchParams(query); params.set('limit', '200'); params.set('offset', String(offset));
    const page = await api(path + '?' + params);
    result.push(...page.items); offset += page.items.length;
    if (offset >= page.total || !page.items.length) return {...page, items: result};
  }
}
function basketTable(basket, page) {
  const section = node('section', null, 'basket'); section.dataset.basket = basket.hash;
  section.style.setProperty('--basket-hue', String(parseInt(basket.hash.slice(0, 6), 16) % 360));
  const heading = node('header', null, 'basket-heading');
  const title = node('h2', basket.name); title.tabIndex = 0;
  const hash = node('span', basket.hash.slice(0, 8), 'hash'); hash.title = basket.hash;
  heading.append(title, hash, node('span', basket.run_count + ' shown / ' + basket.total_run_count + ' runs', 'basket-count'));
  if (basket.description) {
    const tip = node('span', basket.description, 'basket-description-tip');
    tip.id = 'basket-description-' + basket.hash; tip.setAttribute('role', 'tooltip');
    title.setAttribute('aria-describedby', tip.id); heading.append(tip);
  }
  section.append(heading);
  const scroll = node('div', null, 'table-scroll');
  const table = node('table');
  const head = node('thead'); const headings = node('tr');
  for (const text of ['№', 'Pinned', 'Run', 'Model', 'Setup', 'Date', 'Score', 'F1 files', 'Tasks', 'Median time', 'Tokens', 'Cost']) headings.append(node('th', text));
  head.append(headings); table.append(head);
  const body = node('tbody');
  for (const [index, row] of page.items.entries()) body.append(runRow(row, page.offset + index + 1));
  table.append(body); scroll.append(table); section.append(scroll);
  if (page.total <= page.limit) return section;
  const pagination = node('div', null, 'pagination');
  const previous = button('Previous', () => { offsets.set(basket.hash, Math.max(0, page.offset - page.limit)); reload(); });
  const next = button('Next', () => { offsets.set(basket.hash, page.offset + page.limit); reload(); });
  previous.disabled = page.offset === 0; next.disabled = page.offset + page.items.length >= page.total;
  pagination.append(previous, node('span', page.total ? (page.offset + 1) + '–' + (page.offset + page.items.length) + ' / ' + page.total : '0 / 0'), next);
  section.append(pagination); return section;
}
function basketSection(basket, page) {
  const section = basketTable(basket, page);
  const opened = basketExpanded.has(basket.hash) ? basketExpanded.get(basket.hash) : !basket.collapsed;
  const children = [...section.children].slice(1);
  const toggle = button(opened ? 'Collapse basket' : 'Expand basket', () => {
    const next = toggle.getAttribute('aria-expanded') !== 'true'; basketExpanded.set(basket.hash, next);
    toggle.setAttribute('aria-expanded', String(next)); toggle.textContent = next ? 'Collapse basket' : 'Expand basket';
    for (const child of children) child.hidden = !next;
  });
  toggle.setAttribute('aria-expanded', String(opened));
  section.firstElementChild.append(toggle);
  for (const child of children) child.hidden = !opened;
  return section;
}
async function renderCatalogue(version) {
  const query = filters();
  const data = await allPages('/baskets', query);
  if (version !== generation) return;
  populateFilters(data.filter_options);
  if (query.toString() !== filters().toString()) {offsets.clear(); return renderCatalogue(version);}
  $('counts').textContent = data.shown_runs + ' shown / ' + data.total_runs + ' runs';
  const sections = await Promise.all(data.items.map(async basket => {
    const params = new URLSearchParams(query); params.set('limit', '25'); params.set('offset', String(offsets.get(basket.hash) || 0));
    let page = await api('/baskets/' + basket.hash + '/runs?' + params);
    if (version !== generation) return null;
    if (page.offset > 0 && page.offset >= page.total) {
      offsets.delete(basket.hash); params.set('offset', '0');
      page = await api('/baskets/' + basket.hash + '/runs?' + params);
    }
    return basketSection(basket, page);
  }));
  if (version !== generation) return;
  $('content').replaceChildren(...sections);
  if (!sections.length) $('content').append(node('p', data.total_runs ? 'No runs match the filters.' : 'No published runs.', 'empty'));
}
function stat(label, value, title, color = '') {
  const card = node('div', null, 'stat ' + color);
  const strong = node('strong', value instanceof Node ? null : value); if (value instanceof Node) strong.append(value);
  card.append(node('span', label, 'label'), strong);
  if (title) card.title = title;
  return card;
}
function diffBlock(text) {
  const pre = node('pre');
  const lines = node('span', null, 'diff-lines');
  let inHunk = false, block = null, previousKind = null;
  for (const line of text.match(/[^\n]*\n|[^\n]+$/g) || []) {
    if (line.startsWith('@@')) inHunk = true;
    const kind = line.startsWith('@@') ? 'diff-hunk' : !inHunk && (line.startsWith('+++') || line.startsWith('---')) ? 'diff-header' : line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : '';
    if (kind !== previousKind || !block) {
      block = node('span', null, kind);
      lines.append(block);
      previousKind = kind;
    }
    block.append(document.createTextNode(line));
  }
  pre.append(lines);
  return pre;
}
let diffVersion = 0;
let diffAbort = null;
let diffOpener = null;
let diffPosition = null;
const diffDialog = $('diff-dialog');
function invalidateDiff() {
  ++diffVersion; diffAbort?.abort(); diffAbort = null;
}
function finishDiffClose() {
  invalidateDiff();
  document.body.classList.remove('diff-open');
  $('diff-content').replaceChildren(); diffDialog.removeAttribute('aria-busy');
  const opener = diffOpener?.isConnected ? diffOpener : document.querySelector('.task>summary');
  opener?.focus({preventScroll: true});
  if (diffPosition) window.scrollTo(diffPosition.x, diffPosition.y);
  diffPosition = null; diffOpener = null;
}
function closeDiff() {
  if (!diffDialog.open) return;
  diffDialog.close(); finishDiffClose();
}
async function openDiff(task, file, opener) {
  invalidateDiff();
  const version = diffVersion;
  diffAbort = new AbortController();
  const signal = diffAbort.signal;
  diffOpener = opener;
  for (const widget of filterWidgets.values()) widget.close(false);
  $('diff-title').textContent = task.repository + ' #' + task.number + ' · ' + file.path;
  $('diff-content').replaceChildren(node('p', 'Loading diff…', 'diff-message'));
  diffDialog.setAttribute('aria-busy', 'true');
  if (!diffDialog.open) {
    diffPosition = {x: window.scrollX, y: window.scrollY};
    document.body.classList.add('diff-open'); diffDialog.showModal();
  }
  try {
    const data = await api('/runs/' + encodeURIComponent(runId) + '/tasks/' + encodeURIComponent(task.task_id) + '/files/' + encodeURIComponent(file.file_id) + '?' + taskSelection(), {signal});
    if (version !== diffVersion || !diffDialog.open) return;
    const grid = node('div', null, 'diff-grid');
    for (const [key, title] of [['human_diff', 'Human diff'], ['agent_diff', 'Agent diff']]) {
      const panel = node('section', null, 'diff-panel'); panel.append(node('h4', title));
      if (data[key].truncated) panel.append(node('p', 'Diff truncated by display limits.', 'truncated'));
      const pre = diffBlock(data[key].text || '(No content diff)');
      pre.tabIndex = 0; pre.setAttribute('aria-label', title);
      panel.append(pre); grid.append(panel);
    }
    $('diff-content').replaceChildren(grid); diffDialog.removeAttribute('aria-busy');
    requestAnimationFrame(() => {
      if (version !== diffVersion || !diffDialog.open) return;
      for (const pre of grid.querySelectorAll('pre')) {
        const first = pre.querySelector('.diff-add,.diff-remove');
        if (first) pre.scrollTop = Math.max(0, first.offsetTop - 28);
      }
    });
  } catch (error) {
    if (version !== diffVersion || !diffDialog.open || error.name === 'AbortError') return;
    const message = node('p', error.message, 'diff-message diff-error'); message.setAttribute('role', 'alert');
    const retry = error.code === 'run_changed' ? button('Reload run', () => {closeDiff(); reload();}, 'diff-retry') : button('Retry diff', () => openDiff(task, file, opener), 'diff-retry');
    $('diff-content').replaceChildren(message, retry);
    diffDialog.removeAttribute('aria-busy');
  }
}
$('diff-close').addEventListener('click', closeDiff);
diffDialog.addEventListener('cancel', event => {event.preventDefault(); closeDiff();});
diffDialog.addEventListener('close', () => {if (!diffDialog.open && diffPosition) finishDiffClose();});
function onBackdrop(event) {
  const rect = diffDialog.getBoundingClientRect();
  return event.target === diffDialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom);
}
let backdropPressed = false;
diffDialog.addEventListener('pointerdown', event => {backdropPressed = onBackdrop(event);});
diffDialog.addEventListener('pointerup', event => {if (backdropPressed && onBackdrop(event)) closeDiff(); backdropPressed = false;});
diffDialog.addEventListener('keydown', event => {
  if (event.key !== 'Tab') return;
  const focusable = [...diffDialog.querySelectorAll('button:not([disabled]),[tabindex="0"]')];
  const first = focusable[0], last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last.focus();}
  else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
});
function makeFile(task, file) {
  const value = button('', () => openDiff(task, file, value), 'file');
  value.dataset.fileId = file.file_id;
  value.setAttribute('aria-label', 'View diff for ' + file.path);
  value.append(node('span', file.path, 'file-path'), node('span', number(file.score), scoreClass(file.score)), node('span', 'View diff', 'file-action'));
  return value;
}
function originalTaskLink(task) {
  try {
    const url = new URL(task.url);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) {
      const source = node('a', task.kind === 'merge_request' ? 'Original MR' : 'Original PR', 'task-source-link');
      source.href = url.href; source.target = '_blank'; source.rel = 'noopener noreferrer'; return source;
    }
  } catch (_) {}
  return null;
}
function makeTask(task) {
  const view = generation;
  const reportHash = runReportHash;
  const publicationHash = runPublicationHash;
  const details = node('details', null, 'task'); details.dataset.taskId = task.task_id;
  const summary = node('summary');
  const caption = task.repository + ' #' + task.number + ' · ' + task.title;
  const title = node('span', caption, 'title');
  const expand = button('Expand task', event => {event.preventDefault(); event.stopPropagation(); details.open = !details.open;}, 'task-toggle');
  expand.setAttribute('aria-label', 'Expand task ' + task.repository + ' #' + task.number);
  const score = node('span', null, 'numeric ' + scoreClass(task.score)); score.append(scoreView(task, 'score'));
  summary.append(node('span', task.status === 'fail' ? 'FAIL' : task.status === 'OK' ? 'OK' : 'WARN · ' + statusLabels[task.status], 'badge ' + task.status), title, score, expand);
  const telemetry = node('span', 'Total Tokens: ', 'task-metrics'); telemetry.append(measurement(task.tokens.total), ' · Agent Time: ', durationView(task));
  telemetry.title = tokensTitle(task) + '\n' + timeTitle(task); summary.insertBefore(telemetry, expand);
  expand.setAttribute('aria-expanded', String(expandedTasks.has(task.task_id)));
  details.append(summary);
  let loaded = false;
  details.addEventListener('toggle', async () => {
    if (details.open) expandedTasks.add(task.task_id); else expandedTasks.delete(task.task_id);
    expand.textContent = details.open ? 'Collapse task' : 'Expand task'; expand.setAttribute('aria-expanded', String(details.open));
    expand.setAttribute('aria-label', (details.open ? 'Collapse' : 'Expand') + ' task ' + task.repository + ' #' + task.number);
    if (!details.open || loaded || view !== generation) return;
    loaded = true;
    details.querySelector('.task-body')?.remove();
    const body = node('div', null, 'task-body'), sourceLink = originalTaskLink(task);
    if (sourceLink) body.append(sourceLink); body.append(node('p', 'Loading task…')); details.append(body);
    try {
      const data = await api('/runs/' + encodeURIComponent(runId) + '/tasks/' + encodeURIComponent(task.task_id) + '?' + taskSelection(reportHash, publicationHash));
      if (view !== generation || !details.isConnected) return;
      body.replaceChildren(node('h3', 'Task condition'), node('pre', data.condition, 'condition'));
      if (sourceLink) body.prepend(sourceLink);
      const info = node('p', 'Score: ', 'muted'); info.append(scoreView(data, 'score'), ' · F1 files: ', scoreView(data, 'f1_files'), ' · Agent Time: ', durationView(data), ' · Cost: ', costView(data), ' USD'); body.append(info);
      const tokens = node('p', 'Tokens: ', 'muted'); tokens.append(tokensView(data), ' · Total: ', measurement(data.tokens.total)); body.append(tokens);
      body.append(node('h3', 'Files (' + data.files.length + ')'));
      for (const file of data.files) body.append(makeFile(task, file));
      if (!data.files.length) body.append(node('p', data.scoring_status === 'available' ? 'No changed files.' : 'File scoring unavailable.', 'muted'));
      if (pendingTaskFocus?.taskId === task.task_id) {
        const target = [...body.querySelectorAll('button.file')].find(file => file.dataset.fileId === pendingTaskFocus.fileId);
        (target || summary).focus({preventScroll: true}); pendingTaskFocus = null;
      }
    } catch (error) {if (view !== generation || !details.isConnected) return; body.replaceChildren(node('p', error.message)); if (sourceLink) body.prepend(sourceLink); loaded = false; if (error.code === 'run_changed') showError(error);}
  });
  details.open = expandedTasks.has(task.task_id);
  return details;
}
async function renderRun(version) {
  if (!runId) throw new Error('A run ID is required.');
  const query = selection();
  const row = await api('/runs/' + encodeURIComponent(runId) + '?' + query);
  if (version !== generation) return;
  const taskQuery = new URLSearchParams(query); taskQuery.set('sort', $('task-sort').value); taskQuery.set('direction', $('task-direction').value);
  taskQuery.set('expected_report_sha256', row.report_sha256);
  taskQuery.set('expected_publication_sha256', row.publication_sha256);
  const tasks = await allPages('/runs/' + encodeURIComponent(runId) + '/tasks', taskQuery);
  if (version !== generation) return;
  if (tasks.report_sha256 !== row.report_sha256 || tasks.publication_sha256 !== row.publication_sha256) {const error = new Error('Run changed; reload the run.'); error.code = 'run_changed'; throw error;}
  runReportHash = row.report_sha256;
  runPublicationHash = row.publication_sha256;
  runAuthMode = row.auth_mode;
  document.title = row.display_name + ' · PR Similarity Viewer';
  const header = node('section', null, 'run-header');
  const title = node('h1', row.display_name); title.append(pinButton(row)); header.append(title);
  const actions = node('div', null, 'run-actions'); actions.append(configButton(row));
  const actor = window.viewerAccount.actor;
  if (actor && (actor.login === row.user || actor.role === 'admin')) {
    const remove = button('Delete Run', async () => {
      if (!confirm('Delete "' + row.display_name + '"? It will disappear from the leaderboard. The publication is retained in server trash.')) return;
      remove.disabled = true;
      try {
        await window.viewerRequest('/runs/' + encodeURIComponent(runId) + '?expected_publication_sha256=' + encodeURIComponent(row.publication_sha256), 'DELETE');
        location.assign('/');
      } catch (error) {showError(error); remove.disabled = false;}
    }, 'delete-run'); actions.append(remove);
  }
  if (window.viewerAccount.actor?.role === 'admin') {
    const archive = node('a', 'Download Run Archive', 'button-link'); archive.href = '/api/v1/runs/' + encodeURIComponent(runId) + '/archive';
    archive.setAttribute('download', runId + '.tar.gz'); actions.append(archive);
  }
  header.append(actions);
  header.append(node('div', row.user + ' · ' + row.runtime + ' · ' + row.status + ' · ' + dateText(row.created_at), 'metadata'));
  header.append(node('p', row.run_id + ' · Basket ' + row.basket_hash.slice(0, 12), 'hash'));
  const benchmark = row.benchmark;
  if (benchmark) {
    const controls = node('section', null, 'benchmark-metadata'); controls.append(node('h2', 'Benchmark settings'));
    const timeout = benchmark.timeout_seconds == null ? '—' : time(benchmark.timeout_seconds);
    const setupTimeout = benchmark.setup_timeout_seconds == null ? '—' : time(benchmark.setup_timeout_seconds);
    const retries = benchmark.retries;
    controls.append(node('p', 'Agent timeout: ' + timeout + ' · Setup timeout: ' + setupTimeout));
    controls.append(node('p', 'Retries: ' + (retries?.max_retries ?? '—') + ' · Categories: ' + (retries?.categories?.join(', ') || 'None')));
    controls.append(node('p', 'Diff exclusions: ' + (benchmark.diff_excludes?.join(', ') || 'None')));
    header.append(controls);
  }

  const basket = node('section', null, 'basket-info');
  basket.append(node('h2', row.basket.name), node('p', row.basket.description, 'basket-description'));
  const source = row.basket.task_source;
  const contexts = {'diff': 'Diff', 'issue': 'Issues', 'diff+issue': 'Diff + issues'};
  const sourceText = source.source === 'issue' ? 'Issues' : source.source === 'llm' ? 'LLM · ' + (contexts[source.context] || 'Mode unknown') : '—';
  basket.append(node('p', 'Task source: ' + sourceText, 'task-source')); header.append(basket);
  const tags = node('table', null, 'run-tags'); const tr = node('tr');
  for (const group of ['model', 'setup']) { const cell = tagsCell(row, group, 7); cell.prepend(node('div', group, 'muted')); tr.append(cell); }
  tags.append(tr); header.append(tags);
  const cards = node('div', null, 'stat-grid');
  cards.append(stat('Score', scoreView(row, 'score'), 'Selected task score', scoreClass(row.score)), stat('F1 files', scoreView(row, 'f1_files'), 'File precision/recall F1', scoreClass(row.f1_files)), stat('Median time', durationView(row, true), timeTitle(row)), stat('Total Agent Time', durationView(row), timeTitle(row)), stat('Wall Time', time(row.wall_time_seconds), 'Exact seconds: ' + row.wall_time_seconds + '\nElapsed from run creation to execution completion, including pauses before resume. Unaffected by task selection or report edits.'), stat('Cost, USD', costView(row), costTitle(row)), stat('Selected tasks', row.selected_tasks + '/' + row.total_tasks, statusSummary(row), taskClass(row)));
  header.append(cards);
  const tokens = node('p', 'Tokens (cache read / input / output / cache write / reasoning): ', 'muted'); tokens.append(tokensView(row)); tokens.title = tokensTitle(row);
  const total = node('p', 'Total: ', 'token-total'); total.append(measurement(row.tokens.total)); header.append(tokens, total);
  header.append(node('p', statusSummary(row), 'muted'));
  const returnTask = diffDialog.open ? diffOpener?.closest('.task')?.dataset.taskId : null;
  closeDiff();
  const taskControls = $('task-sorting'); taskControls.hidden = false;
  const focused = document.activeElement?.id;
  $('content').replaceChildren(header, node('h2', 'Tasks (' + tasks.total + ')'), taskControls, ...tasks.items.map(makeTask));
  if (['task-sort', 'task-direction', 'task-order-reset'].includes(focused)) $(focused).focus({preventScroll: true});
  if (returnTask) {
    const task = [...$('content').querySelectorAll('.task')].find(value => value.dataset.taskId === returnTask);
    (task?.querySelector('summary') || document.querySelector('.task>summary'))?.focus({preventScroll: true});
  }
}
async function reload() {
  if (metricTip) metricTip.hidden = true;
  const version = ++generation;
  $('activity').textContent = 'Loading…'; $('error').hidden = true;
  try {
    if (settingsPage) await window.viewerRenderSettings(); else if (usersPage) await window.viewerRenderUsers(); else if (detail) await renderRun(version); else await renderCatalogue(version);
    if (version === generation) $('activity').textContent = '';
  } catch (error) { if (version === generation) showError(error); }
}
function taskSelection(hash = runReportHash, publication = runPublicationHash) {
  const query = selection(); if (hash) query.set('expected_report_sha256', hash);
  if (publication) query.set('expected_publication_sha256', publication); return query;
}
function taskChanged() {
  if (diffDialog.open) pendingTaskFocus = {taskId: diffOpener?.closest('.task')?.dataset.taskId, fileId: diffOpener?.dataset.fileId};
  closeDiff();
  if ($('task-sort').value === 'task_order') $('task-direction').value = 'asc';
  $('task-direction').disabled = $('task-sort').value === 'task_order';
  try {sessionStorage.setItem('prsim-task-sorting', JSON.stringify({sort: $('task-sort').value, direction: $('task-direction').value}));} catch (_) {}
  reload();
}
function changed() {
  $('search-toggle').classList.toggle('has-filter', ['search', 'date-from', 'date-to'].some(id => $(id).value));
  try { sessionStorage.setItem('prsim-selection', JSON.stringify({pr: $('pr').value, file: $('file').value, statuses: [...document.querySelectorAll('#statuses input:checked')].map(i => i.value)})); } catch (_) {}
  offsets.clear(); reload();
}
for (const input of document.querySelectorAll('#statuses input')) input.addEventListener('change', changed);
for (const id of ['task-sort', 'task-direction']) $(id).addEventListener('change', taskChanged);
$('task-order-reset').addEventListener('click', () => {$('task-sort').value = 'task_order'; taskChanged();});
for (const id of ['pr', 'file', 'sort', 'direction', 'date-from', 'date-to', ...groups.map(g => 'filter-' + g)]) $(id).addEventListener('change', changed);
$('search-toggle').addEventListener('click', () => {
  const open = $('search-fields').hidden;
  $('search-fields').hidden = !open; $('search-toggle').setAttribute('aria-expanded', String(open));
});
$('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(changed, 200); });
$('pinned-only').addEventListener('click', () => { pinnedOnly = !pinnedOnly; $('pinned-only').setAttribute('aria-pressed', String(pinnedOnly)); changed(); });
$('reset').addEventListener('click', () => {
  for (const input of document.querySelectorAll('#statuses input')) input.checked = input.value !== 'fail';
  $('pr').value = 'LCpr'; $('file').value = 'LCf'; $('sort').value = 'score'; $('direction').value = 'desc';
  for (const id of ['search', 'date-from', 'date-to']) $(id).value = '';
  for (const group of groups) for (const option of $('filter-' + group).options) option.selected = false;
  for (const widget of filterWidgets.values()) widget.reset();
  pinnedOnly = false; $('pinned-only').setAttribute('aria-pressed', 'false');
  $('search-fields').hidden = true; $('search-toggle').setAttribute('aria-expanded', 'false'); changed();
});
try {
  const saved = JSON.parse(sessionStorage.getItem('prsim-selection'));
  if (saved && ['Mpr','FCpr','LCpr'].includes(saved.pr) && ['Mf','LCf'].includes(saved.file) && Array.isArray(saved.statuses) && saved.statuses.every(s => ['OK','no-op','timeout','fail'].includes(s))) {
    $('pr').value = saved.pr; $('file').value = saved.file;
    for (const input of document.querySelectorAll('#statuses input')) input.checked = saved.statuses.includes(input.value);
  }
} catch (_) {}
for (const group of groups) filterWidgets.set(group, createFilter(group));
try {
  const saved = JSON.parse(sessionStorage.getItem('prsim-task-sorting'));
  if (saved && ['score', 'total_tokens', 'time', 'task_order'].includes(saved.sort) && ['asc', 'desc'].includes(saved.direction)) {
    $('task-sort').value = saved.sort; $('task-direction').value = saved.sort === 'task_order' ? 'asc' : saved.direction;
  }
} catch (_) {}
$('task-direction').disabled = $('task-sort').value === 'task_order';
if (detail) { $('page-kind').textContent = 'Run Report'; $('catalogue-filters').hidden = true; $('sorting').hidden = true; $('pinned-only').hidden = true; }
if (usersPage || settingsPage) document.querySelector('.controls').hidden = true;
window.addEventListener('viewer-account-change', () => reload());
window.viewerAccountReady.then(() => reload());
