"""Basket layout and user administration remain usable on narrow screens."""

from typing import cast

import pytest
from playwright.sync_api import Page, Route, expect
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
    assert all(h not in page.locator('.basket-layout-table').inner_text() for h in original)
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


def test_resources_refresh_only_on_reload_and_mock_hides_s3(browser_page: BrowserPage) -> None:
    """Keep activity polling and basket saves from refreshing resource snapshots."""
    page, origin, app, errors = browser_page
    requests = []
    page.on('request', lambda request: requests.append(request.url) if '/admin/resources' in request.url else None)
    login(page, origin)
    expect(page.locator('.storage-card h3', has_text='Server disk')).to_be_visible()
    expect(page.locator('.storage-card h3', has_text='S3 storage')).to_have_count(0)
    expect(page.locator('.storage-breakdown')).to_have_count(0)
    assert sum('refresh=1' in url for url in requests) == 1
    checkbox = page.get_by_role('checkbox', name='Hide Basket A', exact=True)
    checkbox.check()
    page.wait_for_timeout(2300)
    expect(checkbox).to_be_checked()
    assert sum('refresh=1' in url for url in requests) == 1
    page.get_by_role('button', name='Save basket layout', exact=True).click()
    expect(page.get_by_role('button', name='Save basket layout', exact=True)).to_be_enabled()
    expect(page.locator('.storage-card h3', has_text='Server disk')).to_be_visible()
    assert sum('refresh=1' in url for url in requests) == 1
    page.get_by_role('button', name='Reload settings', exact=True).click()
    expect(page.locator('.storage-card h3', has_text='Server disk')).to_be_visible()
    assert sum('refresh=1' in url for url in requests) == 2
    assert page.locator('.basket-layout-table').evaluate(
        '(e)=>e.compareDocumentPosition(document.querySelector(".settings-operations"))'
        '&Node.DOCUMENT_POSITION_FOLLOWING'
    )
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors


def test_activity_unknown_upload_length_and_s3_unknown_capacity(browser_page: BrowserPage) -> None:
    """Render unknown denominators without progress bars and preserve open details."""
    page, origin, _, errors = browser_page

    def stats(route: Route) -> None:
        """Supply unknown transfer length without changing other server statistics."""
        response = route.fetch()
        value = response.json()
        value['activity'] = {
            'items': [
                {
                    'kind': 'upload',
                    'name': 'New publication',
                    'id': 'operation-one',
                    'run_id': 'run-one',
                    'status': 'receiving',
                    'received_bytes': 1024,
                    'total_bytes': None,
                }
            ],
            'total': 1,
        }
        route.fulfill(response=response, json=value)

    page.route('**/admin/stats', stats)
    page.route(
        '**/admin/resources*',
        lambda route: route.fulfill(
            json={
                'object_storage': {
                    'status': 'ready',
                    'used_bytes': 2048,
                    'object_count': 2,
                    'capacity_bytes': None,
                    'groups': {'archives': 2048},
                }
            }
        ),
    )
    login(page, origin)
    expect(page.locator('.activity-table')).to_contain_text('1 KiB received')
    expect(page.locator('.activity-table progress')).to_have_count(0)
    page.locator('.activity-details summary').click()
    page.wait_for_timeout(2300)
    expect(page.locator('.activity-details')).to_have_attribute('open', '')
    storage = page.locator('.storage-card').filter(has=page.get_by_role('heading', name='S3 storage', exact=True))
    expect(storage).to_contain_text('2 KiB')
    expect(page.locator('.storage-card progress')).to_have_count(0)
    expect(storage).to_contain_text('Capacity not reported')
    assert not errors, errors


@pytest.mark.parametrize('status', ['failed', 'retrying'])
def test_recovery_warning_with_idle_refresh(browser_page: BrowserPage, status: str) -> None:
    """Keep interrupted-write recovery visible even without a refresh or activity row."""
    page, origin, _, errors = browser_page

    def stats(route: Route) -> None:
        """Return recovery that blocks writes while the catalogue refresh is idle."""
        response = route.fetch()
        value = response.json()
        value['recovery'] = {'status': status, 'error_code': 'storage_unavailable'}
        value['refresh_job'] = {'status': 'idle'}
        value['activity'] = {'items': [], 'total': 0, 'limit': 100}
        route.fulfill(response=response, json=value)

    page.route('**/admin/stats', stats)
    login(page, origin)
    summary = page.locator('.activity-summary')
    expect(summary).to_contain_text('Interrupted write: ' + status)
    expect(summary).to_contain_text('storage_unavailable')
    expect(summary).to_contain_text('Use Refresh caches to retry.')
    expect(summary).not_to_contain_text('No active operations')
    expect(page.get_by_role('button', name='Refresh caches', exact=True)).to_be_enabled()
    assert not errors, errors


def test_deleted_run_backup_has_no_run_link(browser_page: BrowserPage) -> None:
    """Show the deletion backup identity without linking to a deleted run page."""
    page, origin, _, errors = browser_page

    def stats(route: Route) -> None:
        """Expose a pending tombstone with the identity retained by its durable job."""
        response = route.fetch()
        value = response.json()
        value['activity'] = {
            'items': [
                {
                    'kind': 'backup',
                    'backup_kind': 'tombstone',
                    'id': '7',
                    'run_id': 'deleted-run',
                    'run_available': False,
                    'name': 'Research run',
                    'owner': 'alice',
                    'basket': 'Research basket',
                    'status': 'pending',
                    'archive_bytes': None,
                }
            ],
            'total': 1,
        }
        route.fulfill(response=response, json=value)

    page.route('**/admin/stats', stats)
    login(page, origin)
    table = page.locator('.activity-table')
    expect(table).to_contain_text('Research run')
    expect(table).to_contain_text('alice')
    expect(table).to_contain_text('Research basket')
    expect(table).to_contain_text('Deletion backup')
    expect(table.locator('a')).to_have_count(0)
    assert not errors, errors
