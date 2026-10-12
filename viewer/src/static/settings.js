'use strict';
(() => {
  const create = (tag, text) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n;};
  const control = (text, callback) => {const n = create('button', text); n.type = 'button'; n.onclick = callback; return n;};
  let version = 0;
  window.viewerRenderSettings = async () => {
    const current = ++version, content = document.getElementById('content'); content.replaceChildren();
    document.getElementById('page-kind').textContent = 'Settings';
    if (window.viewerAccount.actor?.role !== 'admin') {content.append(create('p', 'Administrator access required.')); return;}
    try {
      const [layout, stats] = await Promise.all([window.viewerRequest('/admin/baskets/layout'), window.viewerRequest('/admin/stats')]);
      if (current !== version || window.viewerAccount.actor?.role !== 'admin') return;
      content.append(create('h1', 'Settings'), create('p', stats.baskets + ' baskets · ' + stats.runs + ' runs · ' + stats.users + ' users'), create('p', 'Last successful refresh: ' + (stats.last_refresh_at ? new Date(stats.last_refresh_at).toLocaleString() : '—')));
      const message = create('p'); message.setAttribute('role', 'status'); content.append(message);
      const items = layout.items.map(item => ({...item}));
      const table = create('table'); content.append(table);
      const save = control('Save basket layout', async () => {
        save.disabled = true; reload.disabled = true; refresh.disabled = true; message.textContent = 'Saving…';
        for (const input of table.querySelectorAll('input,button')) input.disabled = true;
        try {
          await window.viewerRequest('/admin/baskets/layout', 'PUT', {expected_revision: layout.revision, items: items.map(({hash, hidden, collapsed}) => ({hash, hidden, collapsed}))});
          if (current === version) await window.viewerRenderSettings();
        } catch (error) {if (current === version) {message.textContent = error.message; reload.disabled = false;}}
      });
      const reload = control('Reload settings', () => window.viewerRenderSettings());
      const refresh = control('Refresh caches', async () => {
        refresh.disabled = true; save.disabled = true; reload.disabled = true;
        for (const input of table.querySelectorAll('input,button')) input.disabled = true;
        message.textContent = 'Refreshing caches… Public reads may wait until this finishes.';
        try {await window.viewerRequest('/admin/refresh', 'POST', {}); if (current === version) await window.viewerRenderSettings();}
        catch (error) {if (current === version) {message.textContent = error.message; refresh.disabled = false; save.disabled = false; reload.disabled = false; draw();}}
      });
      function dirty() {refresh.disabled = true; message.textContent = 'Unsaved layout. Save or reload before refreshing caches.';}
      function draw() {
        table.replaceChildren(); const head = create('tr');
        for (const title of ['Basket', 'Order', 'Initially collapsed', 'Hide from home']) head.append(create('th', title)); table.append(head);
        items.forEach((item, index) => {
          const row = create('tr'); row.dataset.basketHash = item.hash;
          const name = create('td', item.name); name.append(create('div', item.hash)); row.append(name);
          const order = create('td');
          for (const [label, delta] of [['Move up', -1], ['Move down', 1]]) {
            const move = control(label, () => {const target = index + delta; [items[index], items[target]] = [items[target], items[index]]; dirty(); draw();});
            move.disabled = index + delta < 0 || index + delta >= items.length; order.append(move);
          }
          row.append(order);
          for (const key of ['collapsed', 'hidden']) {
            const cell = create('td'), check = create('input'); check.type = 'checkbox'; check.checked = item[key];
            check.setAttribute('aria-label', (key === 'hidden' ? 'Hide ' : 'Collapse ') + item.name);
            check.onchange = () => {item[key] = check.checked; dirty();}; cell.append(check); row.append(cell);
          }
          table.append(row);
        });
      }
      draw(); content.append(save, reload, refresh);
    } catch (error) {if (current === version) content.append(create('p', error.message), control('Retry', () => window.viewerRenderSettings()));}
  };
})();
