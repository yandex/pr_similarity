"""Rendered browser behavior against a live local server and in-memory S3."""

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from helpers import FileCallback, archive_fixture, inference
from playwright.sync_api import Page, Route, expect, sync_playwright
from starlette.applications import Starlette
from test_task_sort import prepare_tasks

from viewer.config import Config, Limits
from viewer.persistence.storage import MemoryStore
from viewer.util import digest, json_bytes
from viewer.validation.archive import validate_archive
from viewer.web.api import create_app


def _publication_variant(name: str, basket: str, score: float, status: str) -> FileCallback:
    """Build one named model, basket and execution variant for browser fixtures."""

    def change(report: dict, source: dict, files: dict[str, bytes]) -> None:
        """Bind the selected model, basket, score and execution status to the archive."""
        report['agent']['name'] = name
        model = 'openai/' + ('Beta' if name == 'Agent B' else 'Alpha')
        effort = 'low' if name == 'Agent C' else 'high'
        report['inference'] = inference(model, effort)
        identity = {
            'identity_version': 1,
            'native_config': {
                'model': model,
                'provider': {'openai': {'models': {model.split('/', 1)[1]: {'options': {'reasoningEffort': effort}}}}},
            },
        }
        files['config_identity.json'] = json_bytes(identity)
        report['config']['identity_sha256'] = digest(json_bytes(identity))
        report['run']['tags'] = [
            *['setup: long environment setting ' + str(i) for i in range(8)],
            'sys:pinned',
        ]
        source['meta']['name'] = basket
        source['tasks'][0]['task'] = {
            'task': '<script>window.__viewerInjected = true</script>',
            'subtasks': [],
        }
        source['tasks'][0]['agent_task'] = 'Task: <script>window.__viewerInjected = true</script>'
        report['tasks'][0]['agent_task'] = source['tasks'][0]['agent_task']
        files['basket.json'] = json_bytes(source)
        report['basket']['sha256'] = digest(files['basket.json'])
        row = report['tasks'][0]
        row['scoring'].update(Mf_mlp=score, LCf_mlp=score)
        row['scoring']['files'][0]['mlp'] = score
        row['telemetry']['cost']['amount'] = score
        row['execution']['status'] = status
        report['summary']['error_tasks'] = 1 if status == 'error' else 0

    return change


@pytest.fixture
def browser_page(tmp_path: Path) -> Iterator[tuple[Page, str, Starlette, list[str]]]:
    """Yield a Chromium page against a local viewer populated with three runs."""
    token = tmp_path / 'admin'
    token.write_text('A' * 32)
    token.chmod(0o400)
    config = Config(runtime=tmp_path / 'runtime', admin_token_file=token, storage='mock')
    app = create_app(config, MemoryStore())
    app.state.service.db.create_user('operator', 'secret', role='admin')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = 'http://127.0.0.1:' + str(port)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='error'))
    thread = threading.Thread(target=server.run)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with httpx.Client(base_url=origin) as client:
            assert (
                client.post(
                    '/api/v1/admin/users',
                    json={'login': 'alice', 'password': 'secret'},
                    headers={'Authorization': 'Bearer ' + 'A' * 32},
                ).status_code
                == 201
            )
            for name, basket, score, status in [
                ('Agent A', 'Basket A', 0.8, 'completed'),
                ('Agent B', 'Basket B', 0.5, 'completed'),
                ('Agent C', 'Basket A', 0.2, 'error'),
            ]:

                change = _publication_variant(name, basket, score, status)

                path = tmp_path / (name + '.tar.gz')
                report, manifest = archive_fixture(path, run_id=str(uuid.uuid4()), configure=change)
                grant = client.post('/api/v1/auth/publish-token', json={'login': 'alice', 'password': 'secret'}).json()
                data = path.read_bytes()
                headers = {
                    'Authorization': 'Bearer ' + grant['token'],
                    'Content-Type': 'application/gzip',
                    'Idempotency-Key': str(uuid.uuid4()),
                    'X-Archive-SHA256': digest(data),
                    'X-Report-SHA256': manifest['report_sha256'],
                }
                result = client.put('/api/v1/runs/' + report['run']['id'], content=data, headers=headers)
                assert result.status_code == 201, result.text
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1600, 'height': 1100})
            context.route(
                '**/*', lambda route: route.continue_() if route.request.url.startswith(origin + '/') else route.abort()
            )
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            yield page, origin, app, errors
            context.close()
            browser.close()
            assert errors == []
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        assert not thread.is_alive()


def test_catalogue_filters_pinned_expansion_and_task_statuses(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify catalogue filters pinned expansion and task statuses."""
    page, origin, app, errors = browser_page
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    assert page.locator('.basket').count() == 2
    assert page.locator('tr.run-row').count() == 3
    assert page.locator('#filter-effort').count() == 0
    assert page.locator('table').first.locator('th').all_text_contents() == [
        '№',
        'Pinned',
        'Run',
        'Model',
        'Setup',
        'Date',
        'Score',
        'F1 files',
        'Tasks',
        'Median time',
        'Tokens',
        'Cost',
    ]
    row = page.locator('tr.run-row').filter(has=page.get_by_role('link', name='Agent A', exact=True))
    assert row.locator('td').nth(3).inner_text() == 'openai/Alpha [high]'
    assert row.locator('td').nth(11).inner_text() == '0.80$ⓘ'
    initial_box = row.bounding_box()
    assert initial_box is not None
    before = initial_box['height']
    row.get_by_role('button', name='Expand setup tags for Agent A').click()
    page.wait_for_function(
        '() => document.querySelector(\'button[aria-label="Collapse setup tags for Agent A"]\') !== null'
    )
    expanded_box = row.bounding_box()
    assert expanded_box is not None and expanded_box['height'] > before
    assert page.url == origin + '/'
    assert row.get_by_role('button', name='Pin Agent A', exact=True).is_disabled()
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.get_by_role('link', name='Users', exact=True).wait_for()
    row.get_by_role('button', name='Pin Agent A', exact=True).click()
    page.get_by_role('button', name='Unpin Agent A', exact=True).wait_for()
    page.get_by_role('button', name='Show Pinned Only').click()
    page.wait_for_function('() => document.querySelectorAll(\'tr.run-row\').length === 1')
    assert page.get_by_role('link', name='Agent A', exact=True).count() == 1
    page.get_by_role('button', name='Show Pinned Only').click()
    page.wait_for_function('() => document.querySelectorAll(\'tr.run-row\').length === 3')
    page.get_by_role('button', name='Choose Model', exact=True).click()
    page.get_by_role('checkbox', name='openai/Beta [high]', exact=True).check()
    page.keyboard.press('Escape')
    page.wait_for_function('() => document.querySelectorAll(\'tr.run-row\').length === 1')
    assert page.get_by_role('link', name='Agent B', exact=True).count() == 1
    page.get_by_role('button', name='Reset', exact=True).click()
    page.wait_for_function('() => document.querySelectorAll(\'tr.run-row\').length === 3')
    c = page.locator('tr.run-row').filter(has=page.get_by_role('link', name='Agent C', exact=True))
    assert c.locator('td').nth(6).inner_text() == '—'
    assert c.locator('td').nth(11).inner_text() == '—'
    page.locator('#statuses input[value=fail]').check()
    page.wait_for_function(
        (
            '() => [...document.querySelectorAll(\'tr.run-row\')].find(r=>r.text'
            'Content.includes(\'Agent C\')).children[6].textContent !== \'\u2014\''
        )
    )
    assert float(c.locator('td').nth(6).inner_text()) == 0.2
    page.screenshot(path='/tmp/prsim-viewer-catalogue.png', full_page=True)


def test_run_expands_without_url_change_and_escapes_html(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """Verify run expands without url change and escapes html."""
    page, origin, app, errors = browser_page
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    properties = [
        'fontFamily',
        'fontSize',
        'lineHeight',
        'backgroundColor',
        'color',
        'borderWidth',
        'borderRadius',
        'padding',
    ]
    style = '(e,properties)=>Object.fromEntries(properties.map(p=>[p,getComputedStyle(e)[p]]))'
    page.locator('#file').select_option('Mf')
    page.wait_for_function('() => document.getElementById(\'activity\').textContent === \'\'')
    page.get_by_role('link', name='Agent A', exact=True).click()
    page.wait_for_selector('.task>summary')
    url = page.url
    assert '/run?run_id=' in url
    assert page.locator('#file').input_value() == 'Mf'
    page.locator('.task-toggle').click()
    page.wait_for_selector('.condition')
    assert '<script>' in page.locator('.condition').inner_text()
    assert page.evaluate('window.__viewerInjected') is None
    page.locator('button.file').click()
    page.wait_for_selector('.diff-grid')
    assert page.locator('.diff-panel h4').all_text_contents() == ['Human diff', 'Agent diff']
    assert 'print(' in page.locator('.diff-panel pre').first.inner_text()
    assert page.url == url
    page.get_by_role('button', name='Close diff', exact=True).click()
    page.locator('#file').select_option('Mf')
    page.wait_for_function('() => document.getElementById(\'activity\').textContent === \'\'')
    assert page.url == url
    assert page.locator('.task').count() == 1
    archive = page.get_by_role('link', name='Download Run Archive', exact=True)
    assert archive.count() == 0
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    expect(archive).to_be_visible()
    page.mouse.move(0, 0)
    assert archive.evaluate(style, properties) == page.locator('button.config-button').evaluate(style, properties)
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    assert archive.get_attribute('download') == rid + '.tar.gz'
    with page.expect_download() as downloaded:
        archive.click()
    downloaded.value.save_as(tmp_path / 'downloaded.tar.gz')
    assert (
        digest((tmp_path / 'downloaded.tar.gz').read_bytes()) == app.state.service.runs[rid]['marker']['archive_sha256']
    )
    assert page.url == url
    page.get_by_role('button', name='Log out', exact=True).click()
    expect(archive).to_have_count(0)
    page.screenshot(path='/tmp/prsim-viewer-run.png', full_page=True)


def test_late_task_error_and_diff_response_do_not_replace_current_view(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """Verify late task error and diff response do not replace current view."""
    page, origin, app, errors = browser_page
    svc = app.state.service
    path = tmp_path / 'late-sorting.tar.gz'
    report, _ = archive_fixture(path, prepare=prepare_tasks(6))
    svc.publish(validate_archive(path, tmp_path / 'late-sorting', svc.config.limits), 'alice', 'late-sorting')
    page.goto(origin + '/run?run_id=' + report['run']['id'])
    page.wait_for_selector('.task')
    first = page.locator('.task').first
    id = first.get_attribute('data-task-id')
    assert id is not None
    pattern = '**/tasks/' + quote(id, safe='') + '?*'
    pending = []

    def intercept(route: Route) -> None:
        """Hold the first matching request and allow subsequent requests to proceed."""
        if not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route(pattern, intercept)
    with page.expect_request(pattern):
        first.locator('.task-toggle').click()
    page.locator('#task-direction').select_option('asc')
    page.wait_for_function('() => document.querySelector("#activity").textContent === ""')
    page.locator('.task[data-task-id="' + id + '"] .condition').wait_for()
    pending[0].fulfill(
        status=409,
        content_type='application/json',
        body='{"error":{"code":"run_changed","message":"OLD_RESPONSE_ERROR"}}',
    )
    assert page.locator('#error').is_hidden()
    diff = []
    pattern = '**/tasks/' + quote(id, safe='') + '/files/*?*'
    page.route(pattern, lambda route: diff.append(route))
    task = page.locator('.task[data-task-id="' + id + '"]')
    with page.expect_request(pattern):
        task.locator('button.file').click()
    page.locator('#task-sort').select_option('total_tokens')
    page.wait_for_function('() => document.querySelector("#activity").textContent === ""')
    diff[0].fulfill(
        status=409, content_type='application/json', body='{"error":{"code":"run_changed","message":"OLD_DIFF_ERROR"}}'
    )
    assert not page.locator('#diff-dialog').evaluate('(d)=>d.open')
    expect(page.locator('.task[data-task-id="' + id + '"] button.file')).to_be_focused()


def test_task_link_disallows_script_scheme(browser_page: tuple[Page, str, Starlette, list[str]]) -> None:
    """Verify task link disallows script scheme."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')

    def response(route: Route) -> None:
        """Inject a script-scheme pull-request URL into the task response."""
        source = route.fetch()
        data = source.json()
        data['items'][0]['url'] = 'javascript:window.__badLink=true'
        route.fulfill(response=source, json=data)

    page.route('**/api/v1/runs/' + rid + '/tasks?*', response)
    page.goto(origin + '/run?run_id=' + rid)
    page.wait_for_selector('.task')
    assert page.locator('.task summary a.title').count() == 0
    page.locator('.task-toggle').click()
    page.wait_for_selector('.condition')
    assert page.locator('.task-source-link').count() == 0
    assert page.evaluate('window.__badLink') is None


def test_guest_login_users_and_password_change(browser_page: tuple[Page, str, Starlette, list[str]]) -> None:
    """Verify guest login users and password change."""
    page, origin, app, errors = browser_page
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    assert page.locator('#account-dialog').evaluate('(d) => d.open') is False
    assert page.get_by_role('link', name='Users', exact=True).count() == 0
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.get_by_role('link', name='Users', exact=True).click()
    page.wait_for_selector('tr[data-login=alice]')
    page.get_by_role('textbox', name='New user login', exact=True).fill('new-user')
    page.get_by_role('textbox', name='New user password', exact=True).fill('new-secret')
    page.get_by_role('button', name='Create user', exact=True).click()
    row = page.locator('tr[data-login=new-user]')
    row.wait_for()
    assert row.locator('td').nth(1).inner_text() == 'user'
    row.get_by_role('button', name='Disable', exact=True).click()
    row.get_by_role('button', name='Enable', exact=True).wait_for()
    page.get_by_role('button', name='Change password', exact=True).click()
    page.locator('#account-password').fill('secret')
    page.locator('#account-new-password').fill('new-secret')
    page.locator('#account-confirm-password').fill('new-secret')
    page.locator('#account-submit').click()
    page.locator('#account').get_by_role('button', name='Log in', exact=True).wait_for()
    assert app.state.service.db.authenticate('operator', 'new-secret')['role'] == 'admin'


def test_admin_rows_have_no_access_or_reset_controls(browser_page: tuple[Page, str, Starlette, list[str]]) -> None:
    """Verify admin rows have no access or reset controls."""
    page, origin, app, errors = browser_page
    app.state.service.db.create_user('second', 'secret', role='admin')
    page.goto(origin + '/admin/users')
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.wait_for_selector('tr[data-login=second]')
    for name in ('operator', 'second'):
        row = page.locator('tr[data-login="' + name + '"]')
        assert row.locator('button,input').count() == 0
    assert page.locator('tr[data-login=alice]').get_by_role('button', name='Disable', exact=True).is_visible()
    assert page.locator('tr[data-login=alice]').get_by_role('button', name='Reset password', exact=True).is_visible()
    page.get_by_role('button', name='Change password', exact=True).click()
    expect(page.locator('#account-confirm-password')).to_be_visible()


def test_pending_user_list_discards_response_after_admin_session_loss(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify pending user list discards response after admin session loss."""
    page, origin, app, errors = browser_page
    page.goto(origin + '/admin/users')
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.wait_for_selector('tr[data-login=operator]')
    page.evaluate('''async () => {
      const original = window.fetch; let release;
      window.fetch = (...args) => String(args[0]).includes('/admin/users?') ? new Promise(resolve => {
        release = () => resolve(new Response(JSON.stringify(
          {items:[{login:'operator',role:'admin',enabled:true}],total:1})));
      }) : original(...args);
      try {
        const pending = window.viewerRenderUsers();
        window.viewerAccount.actor = null; release(); await pending;
      } finally {window.fetch = original;}
    }''')
    expect(page.locator('#content')).to_have_text('Administrator access required.')
    assert page.locator('#content').locator('button,input').count() == 0


def test_password_confirmation_requires_matching_typed_value(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify password confirmation requires matching typed value."""
    page, origin, app, errors = browser_page
    page.goto(origin)
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.get_by_role('button', name='Change password', exact=True).click()
    confirm = page.locator('#account-confirm-password')
    assert confirm.count() == 1
    assert confirm.is_visible() and confirm.evaluate('(input)=>input.required')
    assert (
        confirm.evaluate(
            (
                'input=>input.dispatchEvent(new ClipboardEvent("paste",{bubbles:tr'
                'ue,cancelable:true,clipboardData:new DataTransfer()}))'
            )
        )
        is False
    )
    page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
    page.evaluate('navigator.clipboard.writeText("clipboard-probe")')
    confirm.press('Control+v')
    assert confirm.input_value() == ''
    changes = []
    page.on('request', lambda r: changes.append(r) if r.method == 'PUT' and r.url.endswith('/auth/password') else None)
    page.locator('#account-password').fill('secret')
    page.locator('#account-new-password').fill('changed')
    confirm.press_sequentially('different')
    page.locator('#account-submit').click()
    expect(page.locator('#account-error')).to_have_text('New passwords do not match.')
    assert not changes and app.state.service.db.authenticate('operator', 'secret')['role'] == 'admin'
    confirm.fill('')
    confirm.press_sequentially('changed')
    page.locator('#account-submit').click()
    page.locator('#account').get_by_role('button', name='Log in', exact=True).wait_for()
    assert len(changes) == 1 and app.state.service.db.authenticate('operator', 'changed')['role'] == 'admin'
    assert confirm.input_value() == '' and page.locator('#account-new-password').input_value() == ''
    page.get_by_role('button', name='Log in', exact=True).click()
    assert confirm.is_hidden() and not confirm.evaluate('(input)=>input.required')
    expect(page.locator('#account-login-hint')).to_be_visible()


def test_pagination_keeps_baskets_and_unknown_inference(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """Verify pagination keeps baskets and unknown inference."""
    page, origin, app, errors = browser_page
    svc = app.state.service
    for i in range(23):

        def change(report: dict, source: dict, files: dict[str, bytes]) -> None:
            """Create another named run in the shared basket for pagination checks."""
            report['agent']['name'] = 'Bulk ' + str(i)
            source['meta']['name'] = 'Basket A'
            source['tasks'][0]['task'] = {
                'task': '<script>window.__viewerInjected = true</script>',
                'subtasks': [],
            }
            source['tasks'][0]['agent_task'] = 'Task: <script>window.__viewerInjected = true</script>'
            report['tasks'][0]['agent_task'] = source['tasks'][0]['agent_task']
            files['basket.json'] = json_bytes(source)
            report['basket']['sha256'] = digest(files['basket.json'])
            report['tasks'][0]['scoring'].update(Mf_mlp=0.3, LCf_mlp=0.3)
            report['tasks'][0]['scoring']['files'][0]['mlp'] = 0.3

        path = tmp_path / ('bulk-' + str(i) + '.tar.gz')
        report, _ = archive_fixture(path, run_id=str(uuid.uuid4()), configure=change)
        imported = validate_archive(path, tmp_path / ('bulk-data-' + str(i)), Limits())
        svc.publish(imported, 'alice', 'bulk-' + str(i))
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    basket = page.locator('.basket').filter(has_text='Basket A')
    assert basket.locator('tr.run-row').count() == 25
    assert basket.locator('.pagination').count() == 0
    assert page.locator('.basket').filter(has_text='Basket B').locator('.pagination').count() == 0
    report, _ = archive_fixture(tmp_path / 'extra.tar.gz', run_id=str(uuid.uuid4()), configure=change)
    svc.publish(validate_archive(tmp_path / 'extra.tar.gz', tmp_path / 'extra-data', Limits()), 'alice', 'extra')
    extra = report['run']['id']
    page.goto(origin)
    page.wait_for_selector('.basket .pagination')
    basket = page.locator('.basket').filter(has_text='Basket A')
    bulk = basket.locator('tr.run-row').filter(has_text='Bulk').first
    assert bulk.locator('td').nth(3).inner_text() == 'unknown'
    assert bulk.locator('td').nth(4).inner_text() == '—'
    basket.get_by_role('button', name='Next', exact=True).click()
    page.wait_for_function('() => document.querySelector(\'.basket .pagination\').textContent.includes(\'26\u201326\')')
    assert basket.locator('tr.run-row').count() == 1
    assert page.locator('.basket').count() == 2
    assert basket.locator('td').first.inner_text() == '26'
    svc.delete(extra)
    page.evaluate('reload()')
    expect(basket.locator('.run-row')).to_have_count(25)
    assert basket.locator('.pagination').count() == 0


def test_catalogue_five_tag_preview_sixth_collapses_into_plus(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify catalogue five tag preview sixth collapses into plus."""
    page, origin, app, errors = browser_page
    svc = app.state.service
    rid = next(r for r in svc.runs if svc.run(r)['display_name'] == 'Agent A')
    for count in (5, 6):
        svc.patch_metadata(rid, 'alice', {'tags': ['setup-tag-' + str(i) for i in range(count)]})
        page.goto(origin)
        page.wait_for_selector('tr.run-row')
        row = page.locator('tr.run-row').filter(has=page.get_by_role('link', name='Agent A', exact=True))
        setup = row.locator('td').nth(4)
        assert setup.locator('.tag').count() == 5
        if count == 5:
            assert setup.locator('button').count() == 0
        else:
            expect(setup.get_by_role('button', name='Expand setup tags for Agent A')).to_have_text('+1')
            setup.get_by_role('button', name='Expand setup tags for Agent A').click()
            assert setup.locator('.tag').count() == 6
            setup.get_by_role('button', name='Collapse setup tags for Agent A').click()
            assert setup.locator('.tag').count() == 5


def test_settings_guest_message_layout_hide_and_cache_refresh(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify settings guest message layout hide and cache refresh."""
    page, origin, app, errors = browser_page
    page.goto(origin + '/settings')
    page.get_by_text('Administrator access required.', exact=True).wait_for()
    assert not page.locator('#account-dialog').evaluate('(d)=>d.open')
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.get_by_role('checkbox', name='Hide Basket A', exact=True).wait_for()
    page.get_by_role('checkbox', name='Hide Basket A', exact=True).check()
    page.get_by_role('checkbox', name='Collapse Basket B', exact=True).check()
    page.get_by_role('button', name='Save basket layout', exact=True).click()
    expect(page.get_by_role('button', name='Save basket layout', exact=True)).to_be_enabled()
    page.get_by_role('button', name='Refresh caches', exact=True).click()
    expect(page.get_by_role('button', name='Refresh caches', exact=True)).to_be_enabled()
    page.goto(origin)
    page.wait_for_selector('.basket')
    assert page.locator('.basket').count() == 1
    basket = page.locator('.basket').filter(has_text='Basket B')
    assert basket.get_by_role('button', name='Expand basket', exact=True).is_visible()
    assert basket.locator('table').is_hidden()
    basket.get_by_role('button', name='Expand basket', exact=True).click()
    assert basket.locator('table').is_visible()


def test_hidden_basket_removes_facets_and_stale_selected_filter(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify hidden basket removes facets and stale selected filter."""
    page, origin, app, errors = browser_page
    svc = app.state.service
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    page.get_by_role('button', name='Choose Model', exact=True).click()
    page.get_by_role('checkbox', name='openai/Beta [high]', exact=True).check()
    page.keyboard.press('Escape')
    page.wait_for_function('() => document.querySelectorAll(\'tr.run-row\').length===1')
    rid = next(r for r in svc.runs if svc.run(r)['display_name'] == 'Agent B')
    h = svc.run(rid)['basket_hash']
    svc.set_settings(
        svc.settings()['revision'], svc.order, {k: {'hidden': k == h, 'collapsed': False} for k in svc.order}
    )
    page.locator('#direction').select_option('asc')
    expect(page.locator('#counts')).to_have_text('2 shown / 2 runs')
    assert page.locator('tr.run-row').count() == 2 and page.locator('#filter-model option').all_text_contents() == [
        'openai/Alpha [high]',
        'openai/Alpha [low]',
    ]
    assert page.locator('#filter-basket option[value="' + h + '"]').count() == 0
    assert page.locator('#filter-model').evaluate('(s)=>s.selectedOptions.length') == 0
    page.goto(origin + '/run?run_id=' + rid)
    page.wait_for_selector('.task')
    svc.set_settings(
        svc.settings()['revision'], svc.order, {k: {'hidden': True, 'collapsed': False} for k in svc.order}
    )
    page.goto(origin)
    expect(page.locator('#counts')).to_have_text('0 shown / 0 runs')
    expect(page.get_by_text('No published runs.', exact=True)).to_be_visible()
    assert page.locator('.basket').count() == 0
    assert 'hidden from home' not in page.locator('#content').inner_text()


def test_guest_config_dialog_close_and_late_response(browser_page: tuple[Page, str, Starlette, list[str]]) -> None:
    """Verify guest config dialog close and late response."""
    page, origin, app, errors = browser_page
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    assert page.locator('button.config-button').count() == 0
    page.get_by_role('link', name='Agent A', exact=True).click()
    page.wait_for_selector('.task')
    page.get_by_role('button', name='See Run Config for Agent A', exact=True).click()
    page.wait_for_selector('#config-content pre')
    assert 'model' in page.locator('#config-content pre').inner_text()
    assert 'native_config' not in page.locator('#config-content pre').inner_text()
    assert 'timeout_seconds' not in page.locator('#config-content pre').inner_text()
    page.get_by_role('button', name='Close config', exact=True).click()
    assert not page.locator('#config-dialog').evaluate('(d)=>d.open')
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    page.route(
        '**/api/v1/runs/' + rid + '/config?*',
        lambda route: route.fulfill(
            status=503, content_type='application/json', body='{"error":{"message":"Try later"}}'
        ),
    )
    page.get_by_role('button', name='See Run Config for Agent A', exact=True).click()
    page.get_by_role('button', name='Retry', exact=True).wait_for()
    page.keyboard.press('Escape')
    assert not page.locator('#config-dialog').evaluate('(d)=>d.open')
    page.unroute('**/api/v1/runs/' + rid + '/config?*')
    pending = []
    pattern = '**/api/v1/runs/' + rid + '/config?*'
    page.route(pattern, lambda route: pending.append(route))
    with page.expect_request('**/api/v1/runs/' + rid + '/config?*'):
        page.get_by_role('button', name='See Run Config for Agent A', exact=True).click()
    page.keyboard.press('Escape')
    assert pending
    pending[0].fulfill(
        status=200, content_type='application/json', body='{"supported":true,"config":{"runtime":"late"}}'
    )
    assert not page.locator('#config-dialog').evaluate('(d)=>d.open')
    assert page.locator('#config-content pre').count() == 0


def test_task_sorting_pr_links_expansion_and_config_preservation(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """Verify task sorting pr links expansion and config preservation."""
    page, origin, app, errors = browser_page
    svc = app.state.service
    path = tmp_path / 'sorting.tar.gz'
    report, _ = archive_fixture(path, prepare=prepare_tasks(6))
    svc.publish(validate_archive(path, tmp_path / 'sorting-data', svc.config.limits), 'alice', 'sorting')
    page.goto(origin + '/run?run_id=' + report['run']['id'])
    page.wait_for_selector('.task')

    def numbers() -> list[int]:
        """Read request numbers from the currently rendered task captions."""
        return page.locator('.task>summary .title').evaluate_all(
            '(titles)=>titles.map(t=>Number(t.textContent.match(/#(\\d+)/)[1]))'
        )

    assert numbers() == [6, 5, 4, 3, 2, 1]
    first = page.locator('.task').first
    first.locator('summary .title').click()
    page.wait_for_selector('.condition')
    assert first.evaluate('(d)=>d.open') is True
    link = first.get_by_role('link', name='Original PR', exact=True)
    assert link.get_attribute('target') == '_blank' and link.get_attribute('rel') == 'noopener noreferrer'
    with page.expect_popup() as popup:
        link.click()
    popup.value.close()
    assert first.evaluate('(d)=>d.open') is True
    id = first.get_attribute('data-task-id')
    assert id is not None
    url = page.url
    page.locator('#task-direction').select_option('asc')
    page.wait_for_function('() => document.querySelector("#activity").textContent === ""')
    assert numbers() == [1, 2, 3, 4, 5, 6]
    assert page.locator('.task[data-task-id="' + id + '"]').evaluate('(d)=>d.open')
    page.get_by_role('button', name='Original task order', exact=True).click()
    expect(page.locator('#task-direction')).to_be_disabled()
    assert page.locator('#task-direction').input_value() == 'asc'
    page.locator('button.config-button').click()
    page.wait_for_selector('#config-content pre')
    page.locator('#task-sort').select_option('time')
    page.wait_for_function('() => document.querySelector("#activity").textContent === ""')
    assert page.locator('#config-dialog').evaluate('(d)=>d.open')
    page.get_by_role('button', name='Close config', exact=True).click()
    expect(page.locator('button.config-button')).to_be_focused()
    page.reload()
    page.wait_for_selector('.task')
    assert page.locator('#task-sort').input_value() == 'time'
    assert page.url == url


def test_known_model_without_effort_has_no_brackets_or_effort_cell(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """A model without a configured level occupies only the Model column."""
    page, origin, app, errors = browser_page

    def configure(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Publish a known model without configured reasoning effort or setup tags."""
        report['agent']['name'] = 'No effort'
        report['run']['tags'] = []
        report['inference'] = inference('openai/no-effort', None)
        identity = {'identity_version': 1, 'native_config': {'model': 'openai/no-effort'}}
        files['config_identity.json'] = json_bytes(identity)
        report['config']['identity_sha256'] = digest(json_bytes(identity))

    path = tmp_path / 'no-effort.tar.gz'
    report, _ = archive_fixture(path, run_id=str(uuid.uuid4()), configure=configure)
    imported = validate_archive(path, tmp_path / 'no-effort', Limits())
    app.state.service.publish(imported, 'alice', 'no-effort')
    page.goto(origin)
    page.get_by_role('link', name='No effort', exact=True).wait_for()
    row = page.locator('tr.run-row').filter(has=page.get_by_role('link', name='No effort', exact=True))
    assert row.locator('td').nth(3).inner_text() == 'openai/no-effort'
    assert row.locator('td').count() == 12
    assert 'effort' not in app.state.service.run(report['run']['id'])['tag_groups']
    assert page.locator('#filter-effort').count() == 0
    assert errors == []


def test_gitlab_original_link_and_run_report_label(browser_page: tuple[Page, str, Starlette, list[str]]) -> None:
    """The request kind controls MR wording without making the task title navigate."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')

    def response(route: Route) -> None:
        """Present a GitLab request through the same typed task response fields."""
        source = route.fetch()
        data = source.json()
        data['items'][0]['kind'] = 'merge_request'
        data['items'][0]['url'] = 'https://gitlab.com/mailman/mailman/-/merge_requests/1'
        route.fulfill(response=source, json=data)

    page.route('**/api/v1/runs/' + rid + '/tasks?*', response)
    page.goto(origin + '/run?run_id=' + rid)
    expect(page.locator('#page-kind')).to_have_text('Run Report')
    first = page.locator('.task').first
    first.locator('summary .title').click()
    expect(first.get_by_role('link', name='Original MR', exact=True)).to_have_attribute(
        'href', 'https://gitlab.com/mailman/mailman/-/merge_requests/1'
    )
    assert page.url.startswith(origin + '/run?')
    assert not errors


def test_original_link_survives_task_api_failure(browser_page: tuple[Page, str, Starlette, list[str]]) -> None:
    """Moving navigation inside the card must not make it depend on lazy detail loading."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    page.goto(origin + '/run?run_id=' + rid)
    first = page.locator('.task').first
    first.wait_for()
    identifier = first.get_attribute('data-task-id')
    assert identifier is not None
    page.route(
        '**/tasks/' + quote(identifier, safe='') + '?*',
        lambda route: route.fulfill(
            status=503, content_type='application/json', body='{"error":{"message":"Fixture task unavailable"}}'
        ),
    )
    first.locator('summary .title').click()
    expect(first.locator('.task-body')).to_contain_text('Fixture task unavailable')
    expect(first.get_by_role('link', name='Original PR', exact=True)).to_be_visible()
    first.locator('summary .title').click()
    first.locator('summary .title').click()
    expect(first.locator('.task-body')).to_have_count(1)
    expect(first.get_by_role('link', name='Original PR', exact=True)).to_be_visible()
    assert not errors
