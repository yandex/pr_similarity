'use strict';
// Browser credentials stay in HttpOnly cookies; only the CSRF challenge is held in memory.
window.viewerAccount = {actor: null, csrf: null};
(() => {
  const el = id => document.getElementById(id);
  const create = (tag, text) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n;};
  const control = (text, callback) => {const n = create('button', text); n.type = 'button'; n.onclick = callback; return n;};
  const dialog = el('account-dialog');
  let passwordMode = false;
  async function request(path, method = 'GET', value) {
    const headers = method === 'GET' ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': window.viewerAccount.csrf || ''};
    const response = await fetch('/api/v1' + path, {method, headers, ...(value !== undefined ? {body: JSON.stringify(value)} : {})});
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {window.viewerAccount.actor = null; render(); window.dispatchEvent(new Event('viewer-account-change'));}
      if (data.error?.code === 'csrf_denied') await refresh();
      throw new Error(data.error?.message || 'Request failed');
    }
    return data;
  }
  window.viewerRequest = request;
  function open(change = false) {
    passwordMode = change;
    el('account-title').textContent = change ? 'Change password' : 'Log in';
    el('account-submit').textContent = change ? 'Change password' : 'Log in';
    el('account-login').parentElement.hidden = change; el('account-login').required = !change;
    el('password-label').textContent = change ? 'Current password' : 'Password';
    el('new-password-label').hidden = !change; el('account-new-password').required = change;
    el('confirm-password-label').hidden = !change; el('account-confirm-password').required = change;
    el('account-error').hidden = true; el('account-form').reset(); dialog.showModal();
    (change ? el('account-password') : el('account-login')).focus();
  }
  function render() {
    const nav = el('account'); const actor = window.viewerAccount.actor;
    nav.replaceChildren(create('span', actor ? actor.login + ' · ' + actor.role : 'Guest'));
    if (actor) {
      if (actor.role === 'admin') {
        const users = create('a', 'Users'); users.href = '/admin/users';
        const settings = create('a', 'Settings'); settings.href = '/settings'; nav.append(users, settings);
      }
      nav.append(control('Change password', () => open(true)), control('Log out', async () => {
        try {await request('/auth/session', 'DELETE'); await refresh();} catch (error) {alert(error.message);}
      }));
    } else nav.append(control('Log in', () => open()));
  }
  async function refresh() {
    const previous = JSON.stringify(window.viewerAccount.actor);
    const state = await request('/auth/session');
    window.viewerAccount.actor = state.actor; window.viewerAccount.csrf = state.csrf_token; render();
    if (previous !== JSON.stringify(state.actor)) window.dispatchEvent(new Event('viewer-account-change'));
    return state;
  }
  window.viewerRefreshAccount = refresh;
  el('account-cancel').onclick = () => dialog.close();
  function outside(event) {const box = dialog.getBoundingClientRect(); return event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);}
  let backdrop = false;
  dialog.addEventListener('pointerdown', event => {backdrop = outside(event);});
  dialog.addEventListener('pointerup', event => {if (backdrop && outside(event)) dialog.close(); backdrop = false;});
  el('account-confirm-password').addEventListener('paste', event => event.preventDefault());
  el('account-confirm-password').addEventListener('beforeinput', event => {if (event.inputType === 'insertFromPaste') event.preventDefault();});
  dialog.addEventListener('close', () => {el('account-password').value = ''; el('account-new-password').value = ''; el('account-confirm-password').value = '';});
  el('account-form').onsubmit = async event => {
    event.preventDefault(); el('account-submit').disabled = true; el('account-error').hidden = true;
    try {
      if (passwordMode && el('account-new-password').value !== el('account-confirm-password').value) {
        el('account-confirm-password').focus(); throw new Error('New passwords do not match.');
      }
      if (passwordMode) await request('/auth/password', 'PUT', {current_password: el('account-password').value, new_password: el('account-new-password').value});
      else await request('/auth/session', 'POST', {login: el('account-login').value, password: el('account-password').value});
      dialog.close(); await refresh();
    } catch (error) {el('account-error').textContent = error.message; el('account-error').hidden = false;}
    finally {el('account-submit').disabled = false;}
  };
  render(); window.viewerAccountReady = refresh().catch(error => {el('account').append(create('span', error.message));});
  window.addEventListener('focus', () => refresh().catch(() => {}));
  // Refresh the short CSRF challenge while the page stays open, without extending the session.
  setInterval(() => {if (!document.hidden) refresh().catch(() => {});}, 240000);
  window.viewerRenderUsers = async () => {
    const content = el('content'); content.replaceChildren(); el('page-kind').textContent = 'Users';
    if (window.viewerAccount.actor?.role !== 'admin') {content.append(create('p', 'Administrator access required.')); return;}
    try {
      content.append(create('h1', 'Users'));
      const form = create('form'); form.className = 'admin-create';
      const login = create('input'); login.placeholder = 'Login'; login.setAttribute('aria-label', 'New user login'); login.autocomplete = 'off'; login.maxLength = 64; login.required = true;
      const password = create('input'); password.type = 'password'; password.placeholder = 'Password'; password.setAttribute('aria-label', 'New user password'); password.autocomplete = 'new-password'; password.required = true; password.maxLength = 1024;
      const submit = create('button', 'Create user'); submit.type = 'submit';
      form.append(login, password, submit); content.append(form);
      form.onsubmit = async event => {event.preventDefault(); submit.disabled = true;
        try {await request('/admin/users', 'POST', {login: login.value, password: password.value}); password.value = ''; await window.viewerRenderUsers();}
        catch (error) {el('error').textContent = error.message; el('error').hidden = false; submit.disabled = false;}
      };
      const table = create('table'); const head = create('tr'); for (const label of ['Login', 'Role', 'Access', 'Password']) head.append(create('th', label)); table.append(head); content.append(table);
      let offset = 0, total = 0;
      do {
        const data = await request('/admin/users?details=1&limit=200&offset=' + offset);
        if (window.viewerAccount.actor?.role !== 'admin') {content.replaceChildren(create('p', 'Administrator access required.')); return;}
        total = data.total; offset += data.items.length;
        for (const user of data.items) {
          const row = create('tr'); row.dataset.login = user.login; row.append(create('td', user.login), create('td', user.role));
          if (user.role === 'admin') {
            row.append(create('td', user.enabled ? 'Enabled' : 'Disabled'), create('td', user.login === window.viewerAccount.actor.login ? 'Use Change password' : 'Protected admin'));
            table.append(row); continue;
          }
          const access = create('td'); const toggle = control(user.enabled ? 'Disable' : 'Enable', async () => {
            toggle.disabled = true;
            try {await request('/admin/users/' + encodeURIComponent(user.login), 'PATCH', {enabled: !user.enabled}); await window.viewerRefreshAccount(); await window.viewerRenderUsers();}
            catch (error) {alert(error.message); toggle.disabled = false;}
          }); access.append(toggle); row.append(access);
          const reset = create('td'); const resetForm = create('form'); const next = create('input'); next.type = 'password'; next.autocomplete = 'new-password'; next.required = true; next.maxLength = 1024; next.setAttribute('aria-label', 'New password for ' + user.login);
          const resetButton = create('button', 'Reset password'); resetButton.type = 'submit'; resetForm.append(next, resetButton); reset.append(resetForm); row.append(reset);
          resetForm.onsubmit = async event => {event.preventDefault(); resetButton.disabled = true;
            try {await request('/admin/users/' + encodeURIComponent(user.login) + '/password', 'PUT', {password: next.value}); next.value = ''; await refresh();}
            catch (error) {alert(error.message);} finally {resetButton.disabled = false;}
          };
          table.append(row);
        }
      } while (offset < total);
    } catch (error) {content.append(create('p', error.message));}
  };
})();
