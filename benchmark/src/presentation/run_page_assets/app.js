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
const runId = window.prsimLocal?.runId ?? new URLSearchParams(location.search).get('run_id');
const detail = Boolean(window.prsimLocal) || location.pathname === '/run';
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
const displayedAmount = value => value.amount ?? ((value.known_tasks || value.partial_tasks) ? value.known_subtotal : null);
const coverageTitle = value => {
  const tasks = value.known_tasks + ' complete tasks, ' + (value.partial_tasks || 0) + ' partial tasks, ' + (value.unavailable_tasks || 0) + ' unavailable tasks';
  const requests = value.request_coverage_available ? value.known_requests + '/' + value.total_requests + ' requests measured' : (value.known_requests || 0) + ' known requests; full request coverage unknown';
  return 'Final attempts; ' + requests + '; ' + tasks + (value.amount == null ? '; full total unknown' : '');
};
const costTitle = row => 'Source: ' + (Object.entries(row.cost.sources).map(([source, count]) => source + ' (' + count + ')').join(' + ') || '—') + '\n' + coverageTitle(row.cost) + '\nKnown subtotal: ' + money(row.cost.known_subtotal) + ' USD';
const tokensTitle = row => [...categories, 'total'].map(c => c.replace('_', ' ') + ': ' + coverageTitle(row.tokens[c]) + '; subtotal ' + number(row.tokens[c].known_subtotal, 0)).join('\n');
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
  if (value.status === 'partial') result.append(' (partial)');
  const message = [incomplete ? basis + ': ' + value.known_tasks + ' of ' + value.total_tasks + ' selected tasks have known data. Measurement is incomplete.' : '', value.status ? coverageTitle(value) : '', provenance].filter(Boolean).join('\n');
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
  if (window.prsimLocal) return window.prsimLocal.read(path, options);
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
  value.hidden = Boolean(window.prsimLocal);
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
    if (next) section.loadBasket?.();
  });
  toggle.setAttribute('aria-expanded', String(opened));
  section.firstElementChild.append(toggle);
  for (const child of children) child.hidden = !opened;
  return section;
}
let catalogueObserver = null;
async function renderCatalogue(version) {
  const query = filters();
  const data = await allPages('/baskets', query);
  if (version !== generation) return;
  populateFilters(data.filter_options);
  if (query.toString() !== filters().toString()) {offsets.clear(); return renderCatalogue(version);}
  $('counts').textContent = data.shown_runs + ' shown / ' + data.total_runs + ' runs';
  catalogueObserver?.disconnect();
  catalogueObserver = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) entry.target.loadBasket?.();
  }, {rootMargin: '200px'});
  const observer = catalogueObserver;
  const sections = data.items.map(basket => {
    let section = basketSection(basket, {items: [], offset: 0, limit: 25, total: 0});
    const body = section.querySelector('tbody');
    const row = node('tr'), cell = node('td', 'Loading…', 'muted'); cell.colSpan = 12;
    row.append(cell); body.append(row);
    let loading = false, loaded = false;
    const current = () => version === generation && section.isConnected;
    async function load() {
      if (!current() || loading || loaded || section.querySelector('[aria-expanded]').getAttribute('aria-expanded') !== 'true') return;
      loading = true; cell.replaceChildren(document.createTextNode('Loading…'));
      try {
        const params = new URLSearchParams(query); params.set('limit', '25'); params.set('offset', String(offsets.get(basket.hash) || 0));
        let page = await api('/baskets/' + basket.hash + '/runs?' + params);
        if (!current()) return;
        if (page.offset > 0 && page.offset >= page.total) {
          offsets.delete(basket.hash); params.set('offset', '0');
          page = await api('/baskets/' + basket.hash + '/runs?' + params);
          if (!current()) return;
        }
        const replacement = basketSection(basket, page);
        observer.unobserve(section); section.replaceWith(replacement); section = replacement; loaded = true;
      } catch (error) {
        if (current()) cell.replaceChildren(node('span', error.message + ' '), button('Retry', load));
      } finally {loading = false;}
    }
    section.loadBasket = load;
    return section;
  });
  $('content').replaceChildren(...sections);
  for (const section of sections) observer.observe(section);
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
function filePanelHeading(title, added, removed, truncated) {
  const heading = node('h4', title + ' (');
  heading.append(node('span', '+' + added, 'change-added'), ' ', node('span', '−' + removed, 'change-removed'), ')');
  if (truncated) heading.append(' · shown lines');
  return heading;
}
function unifiedDiffPanel(title, data) {
  const panel = node('section', null, 'diff-panel');
  let added = 0, removed = 0, inHunk = false;
  for (const line of data.text.split('\n')) {
    if (line.startsWith('@@')) inHunk = true;
    else if (inHunk && line.startsWith('+')) ++added;
    else if (inHunk && line.startsWith('-')) ++removed;
  }
  panel.append(filePanelHeading(title, added, removed, data.truncated));
  if (data.truncated) panel.append(node('p', 'Diff truncated by display limits', 'truncated'));
  const pre = diffBlock(data.text || (data.truncated ? 'Content exceeds display limit' : 'No changes.'));
  pre.tabIndex = 0; pre.setAttribute('aria-label', title);
  panel.append(pre);
  return panel;
}
let fileViewMode = 'diff';
let diffSelection = null;
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
  diffPosition = null; diffOpener = null; diffSelection = null;
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
  diffSelection = {task, file, opener};
  const full = fileViewMode === 'full';
  $('file-view-diff').setAttribute('aria-pressed', String(!full));
  $('file-view-full').setAttribute('aria-pressed', String(full));
  for (const widget of filterWidgets.values()) widget.close(false);
  $('diff-title').textContent = task.repository + ' #' + task.number + ' · ' + file.path;
  $('diff-content').replaceChildren(node('p', 'Loading diff…', 'diff-message'));
  diffDialog.setAttribute('aria-busy', 'true');
  if (!diffDialog.open) {
    diffPosition = {x: window.scrollX, y: window.scrollY};
    document.body.classList.add('diff-open'); diffDialog.showModal();
  }
  try {
    const data = await api('/runs/' + encodeURIComponent(runId) + '/tasks/' + encodeURIComponent(task.task_id) + '/files/' + encodeURIComponent(file.file_id) + '?' + taskSelection() + (full ? '&view=full' : ''), {signal});
    if (version !== diffVersion || !diffDialog.open) return;
    const grid = node('div', null, 'diff-grid');
    for (const [role, title] of [['human', 'Human'], ['agent', 'Agent']]) {
      grid.append(full ? fullFilePanel(title, data[role + '_file']) : unifiedDiffPanel(title, data[role + '_diff']));
    }
    $('diff-content').replaceChildren(grid); diffDialog.removeAttribute('aria-busy');
    requestAnimationFrame(() => {
      if (version !== diffVersion || !diffDialog.open) return;
      for (const pre of grid.querySelectorAll('pre')) {
        const first = pre.querySelector('.full-line.added,.full-line.removed');
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
for (const mode of ['diff', 'full']) {
  $('file-view-' + mode).addEventListener('click', () => {
    if (fileViewMode === mode || !diffDialog.open || !diffSelection) return;
    fileViewMode = mode;
    const {task, file, opener} = diffSelection;
    openDiff(task, file, opener);
  });
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
  const path = node('span', file.path, 'file-path');
  const state = file.changed_agent === true && file.changed_human === false ? 'Extra' : file.changed_human === true && file.changed_agent === false ? 'Missed' : null;
  if (state) {
    const badge = node('span', state, 'usage-status partial file-state ' + state.toLowerCase());
    path.append(badge);
    value.setAttribute('aria-label', 'View diff for ' + file.path + ' · ' + state + ' file');
  }
  value.append(path, node('span', number(file.score), scoreClass(file.score)), node('span', 'View diff', 'file-action'));
  return value;
}
function originalTaskLink(task) {
  try {
    const url = new URL(task.url);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) {
      const source = node('a', task.kind === 'merge_request' ? 'Open Human MR' : 'Open Human PR', 'task-source-link button-link');
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
  const score = taskMetric(task, 'score');
  summary.append(node('span', task.status === 'OK' ? 'OK' : statusLabels[task.status], 'badge ' + task.status), title, expand);
  const telemetry = node('div', null, 'task-metrics');
  telemetry.append(score, taskMetric(task, 'f1_files'), taskMetric(task, 'time'), taskMetric(task, 'tokens'), taskMetric(task, 'cost'));
  summary.insertBefore(telemetry, expand);
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
      if (window.prsimLocal && data.diagnostic) body.append(node('p', data.diagnostic, 'error'));
      body.prepend(taskChangeToolbar(task, sourceLink, reportHash, publicationHash));
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
  const previousRunFocus = document.activeElement;
  const header = buildRunSummary(row);
  const returnTask = diffDialog.open ? diffOpener?.closest('.task')?.dataset.taskId : null;
  closeDiff();
  const taskControls = $('task-sorting'); taskControls.hidden = false;
  const focused = document.activeElement?.id;
  $('content').replaceChildren(header, node('h2', 'Tasks (' + tasks.total + ')'), taskControls, ...tasks.items.map(makeTask));
  window.prsimLocal?.afterRender?.();
  if (previousRunFocus?.isConnected && previousRunFocus.closest('.controls')) previousRunFocus.focus({preventScroll: true});
  if (['task-sort', 'task-direction', 'task-order-reset'].includes(focused)) $(focused).focus({preventScroll: true});
  if (returnTask) {
    const task = [...$('content').querySelectorAll('.task')].find(value => value.dataset.taskId === returnTask);
    (task?.querySelector('summary') || document.querySelector('.task>summary'))?.focus({preventScroll: true});
  }
}
async function reload() {
  if (metricTip) metricTip.hidden = true;
  const version = ++generation;
  catalogueObserver?.disconnect();
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

;
const labelMap={cache_read:'Cache read',input:'Input',output:'Output',cache_write:'Cache write',reasoning:'Reasoning',total:'Total'};

const compact=v=>v==null?'—':new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:1}).format(v);
function statusBadge(v) {const b=node('span',v.status==='partial'?'Partial':v.status==='unavailable'?'Unavailable':'Complete','usage-status '+v.status);return b;}

let panel=null,anchor=null,pinned=false,hideTimer;
function dismissPanel(restore=false){if(!panel)return;const last=anchor;panel.hidden=true;if(last){last.removeAttribute('aria-describedby');last.removeAttribute('aria-expanded')}anchor=null;pinned=false;if(restore&&last?.isConnected)last.focus()}
function positionPanel(){
 if(!panel||panel.hidden||!anchor)return;
 const r=(anchor.querySelector('.compact-total') || anchor).getBoundingClientRect();if(!anchor.isConnected||r.bottom<0||r.top>innerHeight){dismissPanel();return}
 panel.style.maxHeight='calc(100dvh - 16px)';const w=panel.offsetWidth,natural=panel.scrollHeight+2;
 const below=innerHeight-r.bottom-16,above=r.top-16;const down=below>=natural||below>=above;
 const available=Math.max(80,down?below:above);panel.style.maxHeight=available+'px';
 panel.style.left=Math.max(8,Math.min(r.left,innerWidth-w-8))+'px';
 panel.style.top=(down?r.bottom+8:Math.max(8,r.top-panel.offsetHeight-8))+'px';
}
function ensurePanel(){if(panel)return;panel=node('section',null,'unified-popover');panel.id='usage-popover';panel.hidden=true;document.body.append(panel);panel.addEventListener('mouseenter',()=>clearTimeout(hideTimer));panel.addEventListener('mouseleave',scheduleHide);panel.addEventListener('focusin',()=>clearTimeout(hideTimer));panel.addEventListener('focusout',scheduleHide)}
function scheduleHide(){clearTimeout(hideTimer);hideTimer=setTimeout(()=>{if(!pinned&&!panel?.matches(':hover')&&!anchor?.matches(':hover')&&!panel?.contains(document.activeElement)&&document.activeElement!==anchor)dismissPanel()},180)}
function openPanel(control,title,content){ensurePanel();clearTimeout(hideTimer);if(anchor&&anchor!==control)dismissPanel();anchor=control;pinned=false;panel.replaceChildren();panel.setAttribute('role','tooltip');panel.setAttribute('aria-label',title);const h=node('header');h.append(node('strong',title));panel.append(h);if(content)panel.append(content);panel.classList.toggle('tooltip-title-only',!content);panel.hidden=false;control.setAttribute('aria-describedby',panel.id);positionPanel()}

function compactCoverage(v){return v.request_coverage_available?number(v.known_requests,0)+' / '+number(v.total_requests,0)+' requests measured':'Request coverage unknown'}
function tokenTooltip(row){
 const box=node('div',null,'small-usage'),list=node('dl',null,'token-list');
 for(const c of categories){const v=row.tokens[c],term=node('dt',labelMap[c]),value=node('dd',compact(displayedAmount(v)));if(v.status!=='complete')value.append(node('span',v.status==='partial'?'partial':'unavailable','small-state'));list.append(term,value)}
 box.append(list);
 const values=['total',...categories].map(c=>row.tokens[c]);
 const shared=values.every(v=>v.known_requests===values[0].known_requests&&v.total_requests===values[0].total_requests&&v.request_coverage_available===values[0].request_coverage_available);
 if(shared)box.append(node('p',compactCoverage(row.tokens.total),'tip-foot'));
 else{for(const c of ['total',...categories])box.append(node('p',labelMap[c]+': '+compactCoverage(row.tokens[c]),'tip-foot'))}
 if(values.some(v=>v.status==='partial'))box.append(node('p','Partial = known subtotal','tip-foot'));
 box.append(node('p','Final attempts only','tip-foot'));
 return box;
}
function costTooltip(row) {
 const box = node('div', null, 'small-usage');
 box.append(node('p', displayedAmount(row.cost) == null ? 'Unavailable' : money(displayedAmount(row.cost)) + ' USD', 'tip-cost'));
 box.append(node('p', compactCoverage(row.cost), 'tip-foot'));
 const sources = Object.keys(row.cost.sources || {});
 if (sources.length > 1) {
   box.append(tooltipRows([['Source', 'Native + saved reprice'], ['Basis', 'Mixed pricing sources']]));
 } else if (sources[0] === 'reprice') {
   box.append(tooltipRows([['Source', 'Saved reprice'], ['Basis', 'API-equivalent estimate']]));
 } else {
   box.append(tooltipRows([['Source', sources[0] === 'opencode' ? 'Reported by OpenCode' : 'Unavailable']]));
 }
 if (row.cost.status === 'partial') box.append(node('p', 'Partial = known subtotal', 'tip-foot'));
 if (row.cost.native_zero_tasks > 0) {
   box.append(node('p', row.auth_mode === 'chatgpt_oauth' ? 'Subscription zero reported' : 'Native zero reported', 'tip-foot'));
   box.append(node('p', 'Zero does not verify free API usage', 'tip-foot'));
 }
 box.append(node('p', 'Final attempts only', 'tip-foot'));
 return box;
}
function metricControl(row,kind){
 const value=kind==='Tokens'?row.tokens.total:row.cost;
 const display=kind==='Tokens'?compact(displayedAmount(value)):displayedAmount(value)==null?'—':'$'+number(displayedAmount(value),2);
 const control=button('',event=>{event.stopPropagation();show()},'metric-trigger');
 const show=()=>openPanel(control,kind,kind==='Tokens'?tokenTooltip(row):costTooltip(row));
 control.append(node('span',display,'compact-total'));
 if(value.status!=='complete')control.append(statusBadge(value));
 else if(kind==='Tokens'&&categories.some(c=>row.tokens[c].status!=='complete'))control.append(node('span','Usage incomplete','usage-status partial'));
 control.setAttribute('aria-label',kind+' '+display+'; show breakdown');
 control.addEventListener('mouseenter',show);control.addEventListener('mouseleave',scheduleHide);
 control.addEventListener('focus',()=>{if(!control.matches(':hover'))show()});control.addEventListener('blur',scheduleHide);
 return control;
}
const originalRow=runRow;
runRow=function(row,position){const tr=originalRow(row,position),cell=tr.cells[10],cost=tr.cells[11];
 cell.replaceChildren(metricControl(row,'Tokens'));cell.removeAttribute('title');
 cost.replaceChildren(metricControl(row,'Cost'));cost.removeAttribute('title');
 for(const el of tr.querySelectorAll('[title]')){el.dataset.tip=el.title;el.removeAttribute('title')}
 for(const btn of tr.querySelectorAll('.metric-note')){btn.dataset.tip=btn.getAttribute('aria-label');const replacement=node('span','ⓘ','quiet-info');replacement.tabIndex=0;replacement.dataset.tip=btn.dataset.tip;replacement.setAttribute('aria-label',btn.dataset.tip);btn.replaceWith(replacement);}
 tr.cells[3].dataset.tip=row.tag_groups.model.join(' · ');
 tr.cells[5].textContent=new Date(row.created_at).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'});
 const runStatus = tr.cells[2].querySelectorAll('.run-meta')[1];
 if (row.status === 'completed') runStatus.remove();
 else {
   const label = row.status.replaceAll('_', ' ');
   const badge = node('span', label.charAt(0).toUpperCase() + label.slice(1), 'usage-status partial run-status');
   badge.setAttribute('aria-label', 'Run status: ' + row.status);
   runStatus.replaceChildren(badge);
 }
 const taskCell = tr.cells[8];
 taskCell.className = 'numeric task-status-cell';
 delete taskCell.dataset.tip;
 const statuses = [['OK', 'OK', 'ok'], ['no-op', 'No-op', 'noop'], ['timeout', 'Timeout', 'timeout'], ['fail', 'Fail', 'fail']];
 const summary = statuses.map(([key, label]) => label + ': ' + row.status_counts[key]).join(' · ');
 const breakdown = button('', event => {
   event.stopPropagation();
   openPanel(breakdown, 'Task statuses', node('p', 'All tasks. ' + summary));
 }, 'task-status-breakdown');
 breakdown.dataset.tip = 'All tasks. ' + summary;
 breakdown.setAttribute('aria-label', 'All tasks. ' + summary);
 statuses.forEach(([key, label, color], index) => {
   if (index) breakdown.append(node('span', '/', 'task-status-separator'));
   const value = node('span', String(row.status_counts[key]), 'task-status-count task-status-' + color);
   value.dataset.status = key;
   breakdown.append(value);
 });
 taskCell.replaceChildren(breakdown);
 return tr;
};
// One tooltip owner; native title and old metric buttons are absent in improved rows.
document.addEventListener('pointerover',e=>{const target=e.target.closest('[data-tip]');if(!target||target.contains(e.relatedTarget)||pinned)return;openTip(target)});
document.addEventListener('pointerout',e=>{if(e.target.closest('[data-tip]'))scheduleHide()});
document.addEventListener('focusin',e=>{const target=e.target.closest('[data-tip]');if(target&&!pinned)openTip(target)});
document.addEventListener('focusout',scheduleHide);
document.addEventListener('pointerdown',e=>{if(panel&&!panel.hidden&&!panel.contains(e.target)&&!anchor?.contains(e.target))dismissPanel()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&panel&&!panel.hidden){e.preventDefault();dismissPanel(pinned)}});
window.addEventListener('scroll',positionPanel,true);window.addEventListener('resize',positionPanel);
const originalBasket=basketSection;
basketSection=function(b,page){const section=originalBasket(b,page);const colgroup=node('colgroup');for(const width of [26,34,220,150,88,90,60,65,120,96,140,105]){const col=node('col');col.style.width=width+'px';colgroup.append(col)}section.querySelector('table').prepend(colgroup);const pinHead=section.querySelectorAll('th')[1];pinHead.textContent='★';pinHead.setAttribute('aria-label','Pinned');return section};

const decorateRow=runRow;runRow=function(row,pos){const tr=decorateRow(row,pos);const labels=['Rank','Pinned','Run','Model','Setup','Date','Score','F1 files','Tasks','Median time','Tokens','Cost'];[...tr.cells].forEach((td,i)=>td.dataset.label=labels[i]);return tr;};

const descriptionBasket = basketSection;
basketSection = function(basket, page) {
 const section = descriptionBasket(basket, page);
 section.querySelectorAll('th')[11].textContent = 'Cost';
 const heading = section.querySelector('.basket-heading');
 const hash = heading.querySelector('.hash');
 const description = (basket.description || '').trim();
 if (description) hash.replaceWith(node('span', description, 'basket-description'));
 else hash.remove();
 heading.querySelector('.basket-description-tip')?.remove();
 heading.querySelector('h2').removeAttribute('aria-describedby');
 return section;
};

// Bind the tooltip to the displayed value rather than the full table cell.
const tooltipDefinitions = new WeakMap();
function tooltipRows(rows) {
 const list = node('dl', null, 'tooltip-fields');
 for (const [label, value, color] of rows) {
   const term = node('dt', label);
   const content = node('dd', String(value), color || '');
   list.append(term, content);
 }
 return list;
}
function bindTip(target, title, build) {
 target.dataset.tip = '';
 tooltipDefinitions.set(target, {title, build});
 if (!target.matches('button,a,input,select')) target.tabIndex = 0;
}
function openTip(target) {
 const definition = tooltipDefinitions.get(target);
 if (definition) openPanel(target, definition.title, definition.build());
 else {
   const field = target.closest('[data-label]')?.dataset.label;
   const title = field ? field + (target.matches('.quiet-info') ? ' coverage' : '') : target.getAttribute('aria-label');
   if (!title) return;
   const body = node('div');
   for (const line of target.dataset.tip.split(/\n| · /).filter(Boolean)) body.append(node('p', line));
   openPanel(target, title, body);
 }
}
function valueAnchor(cell) {
 const anchor = node('span', null, 'value-tip-anchor');
 anchor.append(...cell.childNodes);
 if ('tip' in cell.dataset) {anchor.dataset.tip = cell.dataset.tip; delete cell.dataset.tip;}
 cell.append(anchor);
 anchor.tabIndex = 0;
 return anchor;
}
const tooltipRow = runRow;
runRow = function(row, position) {
 const tr = tooltipRow(row, position);
 for (const cell of tr.cells) {
   if (cell.matches('[data-tip]') && ![tr.cells[2], tr.cells[3]].includes(cell)) valueAnchor(cell);
 }
 const name = tr.cells[2].querySelector('.run-name');
 bindTip(name, 'Run ID', () => node('p', row.run_id));
 delete tr.cells[3].dataset.tip;
 const facts = row.inference?.configured || {};
 const known = fact => fact?.status === 'known' ? fact.value : 'Unknown';
 const provider = known(facts.provider);
 const rawModel = known(facts.model);
 const model = provider !== 'Unknown' && rawModel.startsWith(provider + '/') ? rawModel.slice(provider.length + 1) : rawModel;
 for (const tag of tr.cells[3].querySelectorAll('.tag')) {
   bindTip(tag, 'Model', () => tooltipRows([['Provider', provider], ['Model', model], ['Effort', known(facts.effort)]]));
 }
 for (const tag of tr.cells[4].querySelectorAll('.tag')) bindTip(tag, 'Custom run tags', () => null);
 const date = tr.cells[5].querySelector('.value-tip-anchor') || valueAnchor(tr.cells[5]);
 bindTip(date, 'Run date and time', () => {
   const box = node('div');
   box.append(node('p', row.created_at), node('p', 'Display zone: ' + Intl.DateTimeFormat().resolvedOptions().timeZone, 'tip-foot'));
   return box;
 });
 const score = tr.cells[6].querySelector('.value-tip-anchor') || valueAnchor(tr.cells[6]);
 bindTip(score, 'Run Score', () => null);
 const f1 = tr.cells[7].querySelector('.value-tip-anchor') || valueAnchor(tr.cells[7]);
 bindTip(f1, 'Global File Selection F1 (Precision/Recall)', () => null);
 const duration = tr.cells[9].querySelector('.value-tip-anchor') || valueAnchor(tr.cells[9]);
 for (const child of duration.querySelectorAll('[data-tip]')) delete child.dataset.tip;
 bindTip(duration, 'Agent execution time', () => {
   const box = node('div');
   const seconds = value => value == null ? 'Unavailable' : String(value) + ' s';
   box.append(tooltipRows([
     ['Measured tasks', row.time.known_tasks + ' / ' + row.time.total_tasks],
     ['Median per task', seconds(row.time.median_seconds)],
     ['Total', seconds(row.time.total_seconds)]
   ]), node('p', 'Total across selected tasks', 'tip-foot'), node('p', 'Setup excluded', 'tip-foot'), node('p', 'Final attempts only', 'tip-foot'));
   return box;
 });
 const tasks = tr.cells[8].querySelector('.task-status-breakdown');
 const taskRows = () => tooltipRows([
   ['OK', row.status_counts.OK, 'task-status-ok'],
   ['No-op', row.status_counts['no-op'], 'task-status-noop'],
   ['Timeout', row.status_counts.timeout, 'task-status-timeout'],
   ['Fail', row.status_counts.fail, 'task-status-fail']
 ]);
 bindTip(tasks, 'Task statuses · all tasks', taskRows);
 tasks.onclick = event => {event.stopPropagation();openTip(tasks);};
 return tr;
};

// A disabled pin button cannot receive pointer or keyboard events itself.
const plainPinButton = pinButton;
pinButton = function(row) {
 const control = plainPinButton(row);
 if (window.prsimLocal) return control;
 control.removeAttribute('title');
 const wrapper = node('span', null, 'pin-tip-anchor');
 const title = row.pinned ? 'Unpin run' : 'Pin run';
 const content = () => node('p', 'Administrators only', 'tip-foot');
 wrapper.append(control);
 bindTip(wrapper, title, content);
 wrapper.setAttribute('aria-label', title + '. Administrators only');
 wrapper.tabIndex = control.disabled ? 0 : -1;
 if (!control.disabled) bindTip(control, title, content);
 wrapper.addEventListener('click', event => event.stopPropagation());
 return wrapper;
};
// Tag expansion replaces nodes; apply the same hint to newly shown tags.
const setupTagHints = new MutationObserver(() => {
 for (const tag of document.querySelectorAll('.run-row td:nth-child(5) .tag')) {
   if (!tooltipDefinitions.has(tag)) bindTip(tag, 'Custom run tags', () => null);
 }
});
setupTagHints.observe(document.getElementById('content'), {childList: true, subtree: true});


// Run overview uses the catalogue's value formatting and tooltip definitions.
const runPanelState = new Map();
function runDisclosure(row,key,title){const d=node('details',null,'mock-disclosure');d.open=runPanelState.get(row.run_id+key)||false;d.append(node('summary',title));d.addEventListener('toggle',()=>runPanelState.set(row.run_id+key,d.open));return d;}
function summaryValue(value, state) {
 const wrap = node('span', null, 'summary-value');
 wrap.append(node('span', value, 'summary-number'));
 if (state) wrap.append(node('span', state === 'partial' ? 'Partial' : 'Unavailable', 'usage-status ' + state));
 return wrap;
}
function summaryCard(label, value, color = '') {
 const card = node('div', null, 'stat ' + color);
 card.dataset.metric = label;
 card.append(node('span', label, 'label'), value);
 return card;
}
function buildRunSummary(row) {
 const header = node('section', null, 'run-header run-overview');
 const reusable = runRow(row, 1);
 const top = node('div', null, 'run-topline');
 const title = node('h1');
 const name = node('span', row.display_name, 'run-title-text');
 title.append(name, pinButton(row));
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

 actions.querySelector('.config-button').remove();
 const copy = button('Copy run ID', async event => {
   event.stopPropagation();
   try {await navigator.clipboard.writeText(row.run_id); copy.textContent = 'Copied';}
   catch (_) {copy.textContent = 'Copy failed';}
   setTimeout(() => {if (copy.isConnected) copy.textContent = 'Copy run ID';}, 1800);
 }, 'copy-run-id');
 copy.setAttribute('aria-live', 'polite');
 actions.prepend(copy);
 top.append(title, actions);
 const metadata = node('div', null, 'metadata');
 metadata.append(node('span', row.user));
 const status = reusable.querySelector('.run-status');
 if (status) metadata.append(status);
 metadata.append(node('span', new Date(row.created_at).toLocaleString('en-GB', {dateStyle:'medium', timeStyle:'long'})));
 const tags = node('div', null, 'run-context-tags');
 for (const [index, label, cls] of [[4, 'Setup', 'setup-tags']]) {
   const group = node('div', null, 'run-context-group ' + cls);
   group.append(node('span', label, 'context-label'));
   const values = node('div', null, 'context-values');
   values.append(...reusable.cells[index].childNodes);
   group.append(values); tags.append(group);
   if (cls === 'setup-tags') {
     const annotate = () => {for (const tag of group.querySelectorAll('.tag')) if (!tooltipDefinitions.has(tag)) bindTip(tag, 'Custom run tags', () => null);};
     group.addEventListener('click', annotate); annotate();
   }
 }
 const model = node('div', null, 'run-model-fields');
 for (const key of ['provider','model','effort']) {
   const fact = row.inference?.configured?.[key];
   const provider = row.inference?.configured?.provider?.value;
   const shown = key==='model' && provider && fact?.value?.startsWith(provider+'/') ? fact.value.slice(provider.length+1) : fact?.value;
   const field = node('div'); field.append(node('span', key[0].toUpperCase()+key.slice(1), 'context-label'), node('span', shown || 'Unknown'));
   model.append(field);
 }
 tags.prepend(model);
 const basket = node('section', null, 'run-basket-context');
 basket.append(node('h2', row.basket.name), node('p', row.basket.description, 'run-basket-description'));
 const source = row.basket.task_source;
 const contexts = {diff: 'Diff', issue: 'Issues', 'diff+issue': 'Diff + issues'};
 const sourceText = source.source === 'issue' ? 'Issues' : source.source === 'llm' ? 'LLM · ' + (contexts[source.context] || 'Mode unknown') : 'Unknown';
 basket.append(node('p', 'Task source: ' + sourceText, 'run-source'));
 const options = node('div', null, 'run-options'); window.viewerConfigButtons(options, row);tags.append(options);
 const setup = runDisclosure(row,'setup','Run setup');
 const area = node('div',null,'mock-context');setup.append(area);
 function panel(title,rows){const box=node('section',null,'mock-panel');box.append(node('h3',title));const fields=node('dl',null,'mock-fields');for(const [label,value] of rows){const dd=node('dd');dd.append(value instanceof Node?value:document.createTextNode(String(value)));fields.append(node('dt',label),dd);}box.append(fields);area.append(box);return box;}
 const b=row.benchmark;
 const runtime=panel('Runtime & access',[['OpenCode',row.runtime?.replace(/^opencode:v?/, '') || 'Unknown'],['Authentication','Loading…'],['Permissions','Loading…']]);
 panel('Execution',[
 ['Agent timeout',b?.timeout_seconds == null ? 'Unknown' : time(b.timeout_seconds)],
 ['Retries',b?.retries?.max_retries == null ? 'Unknown' : b.retries.max_retries+' additional '+(b.retries.max_retries===1?'attempt':'attempts')],
 ['Retry categories',b?.retries?.categories?.join(', ') || (b?.retries?.categories ? 'None':'Unknown')],
 ['Diff exclusions',b?.diff_excludes?.join(', ') || (b?.diff_excludes ? 'None':'Unknown')]
 ]);
 const agent=panel('Agent setup',[['Setup script','Loading…'],['Setup timeout',b?.setup_timeout_seconds == null ? 'Not applicable':time(b.setup_timeout_seconds)],['Task instructions','Loading…'],['Extra instructions','Loading…'],['Skills / plugins','Loading…'],['Environment overrides','Loading…']]);
 function fillSetup(data){
   if(!header.isConnected)return;
   const info=data.setup_summary || {},native=data.config;
   const auth={api_key:'API key',chatgpt_oauth:'ChatGPT OAuth'};
   runtime.querySelectorAll('dd')[1].textContent=auth[info.auth_mode || row.auth_mode] || 'Unknown';
   runtime.querySelectorAll('dd')[2].textContent=native ? (JSON.stringify(native.permission)==='{"*":"allow"}' ? 'Allow all tools' : native.permission ? 'Custom rules':'Runtime default') : 'Unavailable';
   const cells=agent.querySelectorAll('dd');cells[0].textContent=info.setup_script===true?'Configured':info.setup_script===false?'None':'Unknown';
   if(info.instructions_default && info.instructions_text){const trigger=button('[default]',()=>window.viewerShowInstructions(info.instructions_text,trigger),'mock-default');cells[2].replaceChildren(trigger);}else cells[2].textContent=data.setup_summary?'Custom':'Unknown';
   cells[3].textContent=native ? (native.instructions?.length ? native.instructions.length+' configured':'None configured'):'Unavailable';
   cells[4].textContent=native ? ((native.skills && Object.keys(native.skills).length) || native.plugin?.length ? 'Configured':'None configured'):'Unavailable';
   cells[5].textContent=info.env_count===0?'None':info.env_count==null?'Unknown':info.env_count+' configured';
 }
 window.viewerLoadInputConfig(row).then(fillSetup).catch(()=>{for(const cell of area.querySelectorAll('dd'))if(cell.textContent==='Loading…')cell.textContent='Unavailable';});
 header.append(top, metadata, tags, basket, setup);
 const controls = document.querySelector('.controls');
 controls.querySelector('.hint').textContent = 'Statuses select tasks for metrics; the task list stays complete';
 const settings=runDisclosure(row,'metrics','Run Metric Settings');settings.classList.add('mock-filter-disclosure');settings.append(controls);header.append(settings);
 const cards = node('div', null, 'stat-grid run-summary-grid');
 for (const [index, label, field, tip] of [[6, 'Score', 'score', 'Run Score'], [7, 'F1 files', 'f1_files', 'Global File Selection F1 (Precision/Recall)']]) {
   const value = reusable.cells[index].querySelector('.value-tip-anchor');
   const state = row[field] == null ? 'unavailable' : row.scored_tasks < row.selected_tasks ? 'partial' : null;
   value.replaceChildren(summaryValue(number(row[field]), state));
   bindTip(value, tip, () => state ? tooltipRows([['Scored tasks', row.scored_tasks + ' / ' + row.selected_tasks]]) : null);
   cards.append(summaryCard(label, value, scoreClass(row[field])));
 }
 const counts = node('div', null, 'task-counts-visible');
 for(const [key,label,cls] of [['OK','OK','ok'],['no-op','No-op','noop'],['timeout','Timeout','timeout'],['fail','Fail','fail']]) counts.append(node('span', row.status_counts[key]+' '+label, 'task-status-'+cls));
 const taskCard = summaryCard('Tasks', counts);
 taskCard.append(node('div', row.selected_tasks + ' / ' + row.total_tasks + ' selected for metrics', 'selection-note'));
 const tokens = summaryCard('Tokens', summaryValue(compact(displayedAmount(row.tokens.total)), row.tokens.total.status === 'complete' ? null : row.tokens.total.status));
 const breakdown = node('div', null, 'summary-breakdown');
 const sharedCoverage = categories.every(key => compactCoverage(row.tokens[key]) === compactCoverage(row.tokens.total));
 for (const key of categories) {
   const value = row.tokens[key];
   const line = node('div'); line.append(node('span', labelMap[key]), node('span', compact(displayedAmount(value)) + (value.status === 'complete' ? '' : ' · '+value.status)));
   breakdown.append(line);
   if (!sharedCoverage && value.status !== 'complete') breakdown.append(node('p', compactCoverage(value), 'metric-context'));
 }
 tokens.append(breakdown, node('p', compactCoverage(row.tokens.total), 'metric-context'));
 if(row.tokens.total.status==='partial')tokens.append(node('p', 'Partial = known subtotal', 'metric-context'));
 tokens.append(node('p', 'Final attempts only', 'metric-context'));
 cards.append(taskCard, tokens);
 const median = reusable.cells[9].querySelector('.value-tip-anchor');
 const timeDetails = tooltipDefinitions.get(median).build;
 const timeState = row.time.median_seconds == null ? 'unavailable' : row.time.known_tasks < row.time.total_tasks ? 'partial' : null;
 median.replaceChildren(summaryValue(time(row.time.median_seconds), timeState));
 const medianCard = summaryCard('Median time', median); medianCard.append(node('p', 'Agent time per task', 'metric-context')); cards.append(medianCard);
 const totalSeconds = row.time.total_seconds ?? (row.time.known_tasks > 0 ? row.time.known_subtotal_seconds : null);
 const total = node('span', null, 'value-tip-anchor');
 total.append(summaryValue(time(totalSeconds), totalSeconds == null ? 'unavailable' : row.time.total_seconds == null ? 'partial' : null));
 bindTip(total, 'Agent execution time', timeDetails);
 const totalCard = summaryCard('Total Agent Time', total); totalCard.append(node('p', 'Sum across selected tasks', 'metric-context')); cards.append(totalCard);
 const wall = node('span', null, 'value-tip-anchor'); wall.append(summaryValue(time(row.wall_time_seconds), row.wall_time_seconds == null ? 'unavailable' : null));
 bindTip(wall, 'Wall time', () => {
   const box = node('div');
   box.append(tooltipRows([['Seconds', row.wall_time_seconds ?? 'Unavailable']]), node('p', 'From run start to completion', 'tip-foot'), node('p', 'Includes pauses before resume', 'tip-foot'), node('p', 'Independent of task selection', 'tip-foot'));
   return box;
 });
 const wallCard = summaryCard('Wall Time', wall); wallCard.append(node('p', 'Run duration, including pauses', 'metric-context'));
 const cost = summaryCard('Cost', summaryValue(displayedAmount(row.cost) == null ? '—' : '$'+number(displayedAmount(row.cost),2), row.cost.status === 'complete' ? null : row.cost.status));
 const sources = Object.keys(row.cost.sources || {});
 cost.append(node('p', compactCoverage(row.cost), 'metric-context'));
 cost.append(node('p', sources.length > 1 ? 'Native + Repriced' : sources[0] === 'reprice' ? 'Repriced · API-equivalent estimate' : sources[0] === 'opencode' ? 'Native · OpenCode' : 'Pricing source unavailable', 'metric-context'));
 if (row.cost.native_zero_tasks > 0) cost.append(node('p', (row.auth_mode === 'chatgpt_oauth' ? 'Subscription zero reported' : 'Native zero reported')+' · Zero does not verify free API usage', 'metric-context'));
 if(row.cost.status==='partial')cost.append(node('p', 'Partial = known subtotal', 'metric-context'));
 cost.append(node('p', 'Final attempts only', 'metric-context'));
 cards.append(wallCard, cost);
 const tokenDetails=runDisclosure(row,'tokens','Token breakdown');tokenDetails.className='mock-token-details';
 for(const child of [...tokens.children])if(child.matches('.summary-breakdown,.metric-context'))tokenDetails.append(child);
 tokens.append(tokenDetails);
 for(const name of ['Score','F1 files','Tasks','Median time','Total Agent Time','Wall Time','Tokens','Cost'])cards.append(cards.querySelector('[data-metric="'+name+'"]'));
 header.append(cards);
 return header;
}


// Task metrics share the run/catalogue tooltip owner and measurement semantics.
function taskMetric(row, kind) {
 const wrap=node('div',null,'task-metric');
 const labels={score:'Score',f1_files:'F1 files',tokens:'Tokens',time:'Agent time',cost:'Cost'};
 wrap.dataset.metric=kind;wrap.append(node('span',labels[kind],'task-metric-label'));
 let value;
 if(kind==='tokens'||kind==='cost') {
   value=metricControl({...row,auth_mode:row.auth_mode??runAuthMode},kind==='tokens'?'Tokens':'Cost');
   value.addEventListener('click',event=>event.preventDefault());
 } else {
   let shown,state=null,title,build=()=>null;
   if(kind==='time'){
     const t=row.time;const seconds=t.total_seconds??(t.known_tasks>0?t.known_subtotal_seconds:null);
     shown=time(seconds);state=seconds==null?'unavailable':t.total_seconds==null?'partial':null;title='Agent execution time';
     build=()=>{const content=node('div');content.append(tooltipRows([['Seconds',seconds??'Unavailable']]),node('p','Final attempt · setup excluded','tip-foot'));return content;};
   } else {
     shown=number(row[kind]);state=row[kind]==null?'unavailable':null;
     title=kind==='score'?'Task Score':'File Selection F1 (Precision/Recall)';
   }
   value=node('span',null,'value-tip-anchor '+(kind==='score'||kind==='f1_files'?scoreClass(row[kind]):''));
   value.append(summaryValue(shown,state));bindTip(value,title,build);
 }
 wrap.append(value);return wrap;
}


function fullFilePanel(title, data) {
 const added=data.lines.filter(line=>line.kind==='added').length;
 const removed=data.lines.filter(line=>line.kind==='removed').length;
 const panel=node('section',null,'diff-panel full-file-panel');
 panel.append(filePanelHeading(title, added, removed, data.truncated));
 const note=node('div',null,'file-view-note');
 const states={added:'File added',deleted:'File deleted',absent:'File absent',unchanged:'No changes',modified:'Changes relative to base'};
 note.append(node('span',states[data.state]||'Changes relative to base'));
 const legend=node('span',null,'file-view-legend');legend.append(node('span','Added','added-key'),node('span','Removed','removed-key'));note.append(legend);panel.append(note);
 if(data.truncated)panel.append(node('p','File view truncated by display limits','truncated'));
 const pre=node('pre',null,'full-file');pre.tabIndex=0;pre.setAttribute('aria-label',title);
 const lines=node('span',null,'full-file-lines');
 for(const line of data.lines){
  const row=node('span',null,'full-line '+line.kind);
  const oldNumber=node('span',line.base_line??'','line-number');const newNumber=node('span',line.result_line??'','line-number');
  oldNumber.setAttribute('aria-hidden','true');newNumber.setAttribute('aria-hidden','true');
  row.append(oldNumber,newNumber,node('span',line.text+'\n','line-code'));lines.append(row);
 }
 if(!data.lines.length)lines.append(node('span',data.truncated?'Content exceeds display limit':data.state==='absent'?'File is absent':data.state==='deleted'?'Deleted empty file':'Empty file','empty-file-note'));
 pre.append(lines);
 const scrollWrap=node('div',null,'file-scroll-wrap');
 const map=node('div',null,'file-change-map');map.setAttribute('role','img');map.setAttribute('aria-label','Added and removed line locations');
 const total=Math.max(1,data.lines.length);
 for(let i=0;i<data.lines.length;){
  const kind=data.lines[i].kind;let end=i+1;while(end<data.lines.length&&data.lines[end].kind===kind)end++;
  if(kind==='added'||kind==='removed'){
   const mark=node('span',null,'file-change-mark '+kind);mark.style.top=(i/total*100)+'%';mark.style.height=((end-i)/total*100)+'%';mark.setAttribute('aria-hidden','true');map.append(mark);
  }
  i=end;
 }
 const viewport=node('span',null,'file-map-viewport');viewport.setAttribute('aria-hidden','true');map.append(viewport);
 scrollWrap.append(pre,map);panel.append(scrollWrap);
 pre.addEventListener('scroll',()=>updateFileMap(scrollWrap));requestAnimationFrame(()=>{if(pre.isConnected)updateFileMap(scrollWrap)});
 if(data.ends_with_newline===false&&!data.truncated&&data.state!=='deleted')panel.append(node('p','No newline at end of file','file-view-footer'));
 return panel;
}

function updateFileMap(wrap){
 const pre=wrap.querySelector('pre'),viewport=wrap.querySelector('.file-map-viewport');
 const height=Math.max(1,pre.scrollHeight);viewport.style.top=(pre.scrollTop/height*100)+'%';viewport.style.height=(Math.min(1,pre.clientHeight/height)*100)+'%';
}
window.addEventListener('resize',()=>document.querySelectorAll('.file-scroll-wrap').forEach(updateFileMap));


const taskChangesCache = new Map();
function taskChangeToolbar(task, sourceLink, reportHash, publicationHash) {
 const toolbar=node('div',null,'task-change-toolbar'),stats=node('div',null,'task-change-stats');toolbar.append(stats);if(sourceLink)toolbar.append(sourceLink);
 const key=[runId,task.task_id,reportHash,publicationHash].join(':');
 function render(data){
  stats.replaceChildren();
  for(const [role,label] of [['human',task.kind==='merge_request'?'Human MR':'Human PR'],['agent','Agent Solution']]){
   const item=node('div',null,'task-change-summary');item.dataset.side=role;item.append(node('span',label+':','change-label'));
   const value=data?.[role];
   if(!value)item.append(node('span','Loading…','muted'));
   else{
    if(value.added!=null && value.removed!=null){item.append(node('span','+'+number(value.added,0),'change-added'),node('span','−'+number(value.removed,0),'change-removed'));}
    if(value.files!=null)item.append(node('span',(value.added!=null?', ':'')+number(value.files,0)+' '+(value.files===1?'file':'files'),'change-files'));
    if(value.status!=='complete')item.append(node('span',value.status==='partial'?'Partial':'Unavailable','usage-status '+value.status));
    if(value.status!=='complete')bindTip(item,'Change counts',()=>node('p',value.files==null?'File scoring unavailable':value.measured_files+' / '+value.files+' changed files measured'));
   }
   stats.append(item);
  }
 }
 render(null);
 const load=()=>{
  if(!taskChangesCache.has(key))taskChangesCache.set(key,api('/runs/'+encodeURIComponent(runId)+'/tasks/'+encodeURIComponent(task.task_id)+'?'+taskSelection(reportHash,publicationHash)+'&view=changes').catch(error=>{taskChangesCache.delete(key);throw error}));
  taskChangesCache.get(key).then(data=>{if(toolbar.isConnected)render(data)}).catch(error=>{
   if(!toolbar.isConnected)return;
   stats.replaceChildren(node('span','Change counts unavailable','muted'));
   const retry=button(error.code==='run_changed'?'Reload run':'Retry',()=>{if(error.code==='run_changed')reload();else {render(null);load()}});stats.append(retry);
  });
 };
 load();return toolbar;
}
