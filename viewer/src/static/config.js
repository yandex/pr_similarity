'use strict';
(() => {
  const dialog = document.getElementById('config-dialog'), content = document.getElementById('config-content');
  let version = 0, controller = null, opener = null, openedRunId = null;
  const create = (tag, text) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n;};
  function closed() {
    ++version; controller?.abort(); controller = null; dialog.removeAttribute('aria-busy'); content.replaceChildren();
    const target = opener?.isConnected ? opener : [...document.querySelectorAll('button.config-button')].find(button => button.dataset.runId === openedRunId);
    target?.focus({preventScroll: true}); opener = null; openedRunId = null;
  }
  document.getElementById('config-close').onclick = () => dialog.close();
  dialog.addEventListener('close', closed);
  function outside(event) {const box = dialog.getBoundingClientRect(); return event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);}
  let backdrop = false; dialog.addEventListener('pointerdown', event => {backdrop = outside(event);});
  dialog.addEventListener('pointerup', event => {if (backdrop && outside(event)) dialog.close(); backdrop = false;});
  window.viewerOpenConfig = async (row, button) => {
    controller?.abort(); controller = new AbortController(); const current = ++version; opener = button; openedRunId = row.run_id;
    document.getElementById('config-title').textContent = 'opencode.json · ' + row.display_name;
    content.replaceChildren(create('p', 'Loading config…')); dialog.setAttribute('aria-busy', 'true');
    if (!dialog.open) dialog.showModal();
    try {
      const query = new URLSearchParams(); if (row.report_sha256) query.set('expected_report_sha256', row.report_sha256);
      if (row.publication_sha256) query.set('expected_publication_sha256', row.publication_sha256);
      const response = await fetch('/api/v1/runs/' + encodeURIComponent(row.run_id) + '/config?' + query, {signal: controller.signal});
      const data = await response.json(); if (current !== version || !dialog.open) return;
      if (!response.ok) {const error = new Error(data.error?.message || 'Config request failed'); error.changed = data.error?.code === 'run_changed'; throw error;}
      content.replaceChildren();
      if (data.supported) {
        content.append(create('p', 'OpenCode input. Resource paths are relative to the agent directory in the run archive.'), create('pre', JSON.stringify(data.config, null, 2)));
        const details = create('details'); details.append(create('summary', 'Source details'), create('p', data.source.path + ' · SHA-256: ' + data.source.sha256)); content.append(details);
      } else content.append(create('p', 'Config unavailable (' + data.reason + '). The complete snapshot remains in the original archive.'));
    } catch (error) {
      if (current !== version || !dialog.open || error.name === 'AbortError') return;
      const retry = create('button', error.changed ? 'Reload run' : 'Retry'); retry.type = 'button';
      retry.onclick = () => error.changed ? location.reload() : window.viewerOpenConfig(row, button);
      content.replaceChildren(create('p', error.message), retry);
    } finally {if (current === version) dialog.removeAttribute('aria-busy');}
  };
})();
