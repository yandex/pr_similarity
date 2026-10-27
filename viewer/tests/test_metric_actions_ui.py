"""Actual browser metrics, accessible markers and guarded owner/admin deletion."""

from pathlib import Path

import pytest
import test_ui
from helpers import archive_fixture, inference
from playwright.sync_api import Page, expect
from starlette.applications import Starlette
from test_task_sort import prepare_tasks

from viewer.util import digest, json_bytes, parse_json, require_object
from viewer.validation.archive import validate_archive

browser_page = test_ui.browser_page


@pytest.mark.parametrize(
    'mode,sources,zeros,warning,copy',
    [
        ('api_key', {'opencode': 1}, 1, True, 'pricing configuration may be missing'),
        ('chatgpt_oauth', {'opencode': 1}, 1, False, 'subscription access'),
        ('api_key', {'reprice': 1}, 0, False, 'API-equivalent estimate'),
        (None, {'opencode': 1}, 0, False, 'Access mode is unknown'),
        ('api_key', {'opencode': 1, 'reprice': 1}, 1, True, 'not a uniform pricing basis'),
    ],
)
def test_cost_basis_markers(
    browser_page: tuple[Page, str, Starlette, list[str]],
    mode: str | None,
    sources: dict[str, int],
    zeros: int,
    warning: bool,
    copy: str,
) -> None:
    """Verify cost basis markers."""
    page, origin, _, _ = browser_page
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    result = page.evaluate(
        '''row => {
      const view = costView(row);
      const note = view.querySelector('.metric-note');
      return {text:view.textContent, message:note.getAttribute('aria-label'),
        warning:note.classList.contains('warning')};
    }''',
        {
            'auth_mode': mode,
            'cost': {
                'amount': 0,
                'known_subtotal': 0,
                'known_tasks': 1,
                'total_tasks': 1,
                'sources': sources,
                'native_zero_tasks': zeros,
            },
        },
    )
    assert copy in result['message'] and result['warning'] is warning
    assert result['text'].startswith('0.000000')


def test_browser_delete_rejects_overwrite_after_confirmation_view(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """Verify browser delete rejects overwrite after confirmation view."""
    page, origin, app, _ = browser_page
    svc = app.state.service
    rid = next(iter(svc.runs))
    page.goto(origin + '/run?run_id=' + rid)
    page.wait_for_selector('.run-header')
    browser_login(page, 'alice')
    remove = page.get_by_role('button', name='Delete Run', exact=True)
    expect(remove).to_be_visible()
    old = svc.runs[rid]['marker']['archive_sha256']
    frozen_basket = require_object(parse_json(svc.store.get(svc.runs[rid]['prefix'] + 'data/basket.json')))
    path = tmp_path / 'replacement.tar.gz'

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Replace the agent name while preserving the original frozen basket."""
        report['agent']['name'] = 'Replaced after page load'
        basket.clear()
        basket.update(frozen_basket)
        report['tasks'][0]['agent_task'] = basket['tasks'][0]['agent_task']
        files['basket.json'] = json_bytes(basket)
        report['basket']['sha256'] = digest(files['basket.json'])

    archive_fixture(path, run_id=rid, configure=change)
    svc.publish(
        validate_archive(path, tmp_path / 'replacement-data', svc.config.limits), 'alice', 'replacement', overwrite=True
    )
    assert svc.runs[rid]['marker']['archive_sha256'] != old
    page.once('dialog', lambda dialog: dialog.accept())
    remove.click()
    expect(page.locator('#error')).to_contain_text('Run changed')
    assert rid in svc.runs


def add_partial_run(app: Starlette, tmp_path: Path) -> str:
    """Publish a run with one measured task and one timeout lacking metrics."""

    def configure(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Combine known zero cost with unavailable timeout metrics and scoring."""
        report['agent']['name'] = 'Partial run'
        first, second = report['tasks']
        first['telemetry']['tokens'] = dict(cache_read=100, input=20, output=5, cache_write=0, reasoning=2, total=127)
        first['telemetry']['cost']['amount'] = 0
        first['execution']['duration_seconds'] = 234.8
        second['execution']['status'] = 'timeout'
        second['execution']['duration_seconds'] = None
        second['telemetry']['tokens'] = {k: None for k in first['telemetry']['tokens']}
        second['telemetry']['cost']['amount'] = None
        second['scoring'].update(
            status='unavailable',
            Mf_mlp=None,
            LCf_mlp=None,
            counts=None,
            files=[],
            error={'code': 'fixture_unavailable', 'message': 'Scoring unavailable in this fixture.'},
        )
        report['summary'].update(scored_tasks=1, timeout_tasks=1, error_tasks=0)
        report['agent']['auth_mode'] = 'chatgpt_oauth'
        identity = {'identity_version': 1, 'auth_mode': 'chatgpt_oauth', 'native_config': {'model': 'openai/Alpha'}}
        report['inference'] = inference('openai/Alpha', None)
        files['config_identity.json'] = json_bytes(identity)
        report['config']['identity_sha256'] = digest(json_bytes(identity))

    path = tmp_path / 'partial.tar.gz'
    report, _ = archive_fixture(path, prepare=prepare_tasks(2), configure=configure)
    svc = app.state.service
    svc.publish(validate_archive(path, tmp_path / 'partial-data', svc.config.limits), 'alice', 'partial')
    return report['run']['id']


def browser_login(page: Page, user: str) -> None:
    """Sign in through the account dialog and wait for authenticated controls."""
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill(user)
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    expect(page.get_by_role('button', name='Log out', exact=True)).to_be_visible()


def test_partial_metrics_tooltip_duration_and_layout(
    browser_page: tuple[Page, str, Starlette, list[str]], tmp_path: Path
) -> None:
    """Verify partial metrics tooltip duration and layout."""
    page, origin, app, _ = browser_page
    rid = add_partial_run(app, tmp_path)
    page.goto(origin)
    page.wait_for_selector('tr.run-row')
    row = page.locator('tr.run-row[data-run-id="' + rid + '"]')
    assert 'Total: 127(!)' in row.inner_text()
    note = row.locator('.token-total .metric-note')
    note.focus()
    expect(page.locator('#metric-tooltip')).to_contain_text('1 of 2 selected tasks')
    note.click()
    assert page.url == origin + '/'
    row.locator('td').nth(11).locator('.metric-note').focus()
    expect(page.locator('#metric-tooltip')).to_contain_text('subscription access')
    page.goto(origin + '/run?run_id=' + rid)
    page.wait_for_selector('.run-tags')
    assert '3m 55s' in page.locator('.run-header').inner_text()
    cells = page.locator('.run-tags td').evaluate_all('(cells)=>cells.map(c=>c.getBoundingClientRect().width)')
    assert 240 <= cells[0] <= 280 and cells[1] > cells[0]
    assert page.evaluate('[time(59.94),time(59.96),time(60),time(3599.6),time(5507.4),time(0),time(null)]') == [
        '59.9 s',
        '1m 0s',
        '1m 0s',
        '1h 0m 0s',
        '1h 31m 47s',
        '0.0 s',
        '—',
    ]
    page.set_viewport_size({'width': 320, 'height': 800})
    assert page.evaluate('document.documentElement.scrollWidth') <= 320
    browser = page.context.browser
    assert browser is not None
    touch = browser.new_context(has_touch=True, viewport={'width': 320, 'height': 800})
    try:
        mobile = touch.new_page()
        mobile.goto(origin + '/run?run_id=' + rid)
        mobile.locator('.run-header .token-total .metric-note').tap()
        expect(mobile.locator('#metric-tooltip')).to_contain_text('1 of 2 selected tasks')
    finally:
        touch.close()


def test_owner_delete_cancel_confirm_and_admin_foreign_run(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Verify owner delete cancel confirm and admin foreign run."""
    page, origin, app, _ = browser_page
    svc = app.state.service
    rid = next(iter(svc.runs))
    page.goto(origin + '/run?run_id=' + rid)
    page.wait_for_selector('.run-header')
    expect(page.get_by_role('button', name='Delete Run', exact=True)).to_have_count(0)
    browser_login(page, 'alice')
    remove = page.get_by_role('button', name='Delete Run', exact=True)
    expect(remove).to_be_visible()
    page.once('dialog', lambda dialog: dialog.dismiss())
    remove.click()
    assert rid in svc.runs
    page.once('dialog', lambda dialog: dialog.accept())
    remove.click()
    page.wait_for_url(origin + '/')
    assert rid not in svc.runs
    page.get_by_role('button', name='Log out', exact=True).click()
    expect(page.get_by_role('button', name='Log in', exact=True)).to_be_visible()
    rid = next(iter(svc.runs))
    page.goto(origin + '/run?run_id=' + rid)
    browser_login(page, 'operator')
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Delete Run', exact=True).click()
    page.wait_for_url(origin + '/')
    assert rid not in svc.runs
