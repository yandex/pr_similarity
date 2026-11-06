"""Basket layout and user administration remain usable on narrow screens."""

from typing import cast

from playwright.sync_api import Page, expect
from test_ui import BrowserPage

pytest_plugins = ['test_ui']


def login(page: Page, origin: str) -> None:
    """Authenticate the administrator and wait for basket settings."""
    page.goto(origin + '/settings')
    page.get_by_role('button', name='Log in', exact=True).click()
    page.locator('#account-login').fill('operator')
    page.locator('#account-password').fill('secret')
    page.locator('#account-submit').click()
    page.locator('.basket-layout-table').wait_for()


def test_drag_keyboard_flags_and_save(browser_page: BrowserPage) -> None:
    """Persist basket order and collapse flags after pointer and keyboard changes."""
    page, origin, app, errors = browser_page
    login(page, origin)
    rows = page.locator('tr[data-basket-hash]')
    original = rows.evaluate_all('els=>els.map(e=>e.dataset.basketHash)')
    expect(page.locator('.basket-layout-table th')).to_have_count(3)
    assert page.locator('table th').evaluate_all('els=>els.every(e=>getComputedStyle(e).textAlign===\'left\')')
    assert all(h not in page.locator('table').inner_text() for h in original)
    expect(page.locator('.basket-layout-description').first).to_have_text('Synthetic consumer fixture')
    page.get_by_role('checkbox', name='Collapse Basket A', exact=True).check()
    handle = page.get_by_role('button', name='Reorder Basket A', exact=True)
    target = cast(dict[str, float], rows.nth(1).bounding_box())
    handle.hover()
    page.mouse.down()
    page.mouse.move(target['x'] + target['width'] / 2, target['y'] + target['height'] / 2, steps=10)
    page.mouse.up()
    expect(rows.nth(1)).to_have_attribute('data-basket-hash', original[0])
    expect(page.get_by_role('checkbox', name='Collapse Basket A', exact=True)).to_be_checked()
    expect(page.get_by_role('button', name='Refresh caches', exact=True)).to_be_disabled()
    handle = page.get_by_role('button', name='Reorder Basket A', exact=True)
    handle.focus()
    page.keyboard.press('Home')
    expect(rows.first).to_have_attribute('data-basket-hash', original[0])
    expect(handle).to_be_focused()
    page.keyboard.press('ArrowDown')
    expect(rows.nth(1)).to_have_attribute('data-basket-hash', original[0])
    page.get_by_role('button', name='Save basket layout', exact=True).click()
    expect(page.get_by_role('button', name='Save basket layout', exact=True)).to_be_enabled()
    assert app.state.service.order == list(reversed(original))
    page.reload()
    expect(rows.first).to_have_attribute('data-basket-hash', original[1])
    expect(page.get_by_role('checkbox', name='Collapse Basket A', exact=True)).to_be_checked()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors


def test_pending_save_freezes_drag_and_conflict_keeps_reload(browser_page: BrowserPage) -> None:
    """Block reordering during save and require reload after a revision conflict."""
    page, origin, app, errors = browser_page
    login(page, origin)
    pending = []
    page.route(
        '**/api/v1/admin/baskets/layout',
        lambda route: pending.append(route) if route.request.method == 'PUT' else route.continue_(),
    )
    rows = page.locator('tr[data-basket-hash]')
    original = rows.evaluate_all('els=>els.map(e=>e.dataset.basketHash)')
    page.get_by_role('button', name='Save basket layout', exact=True).click()
    handle = page.locator('.basket-drag-handle').first
    expect(handle).to_be_disabled()
    handle.dispatch_event('keydown', {'key': 'End'})
    assert rows.evaluate_all('els=>els.map(e=>e.dataset.basketHash)') == original
    pending[0].fulfill(
        status=409,
        json={
            'error': {
                'message': 'Settings changed; reload before saving',
                'code': 'settings_changed',
            }
        },
    )
    expect(page.get_by_role('button', name='Reload settings', exact=True)).to_be_enabled()
    expect(handle).to_be_disabled()
    page.get_by_role('button', name='Reload settings', exact=True).click()
    expect(page.locator('.basket-drag-handle').first).to_be_enabled()
    assert not errors, errors


def test_users_compact_alignment_and_mobile(browser_page: BrowserPage) -> None:
    """Keep user tables aligned and responsive on mobile viewports."""
    page, origin, app, errors = browser_page
    login(page, origin)
    page.get_by_role('link', name='Users', exact=True).click()
    page.locator('.users-table').wait_for()
    assert cast(dict[str, float], page.locator('.users-table').bounding_box())['width'] <= 980
    assert page.locator('.users-table th,.users-table td').evaluate_all(
        'els=>els.every(e=>getComputedStyle(e).textAlign===\'left\')'
    )
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors
