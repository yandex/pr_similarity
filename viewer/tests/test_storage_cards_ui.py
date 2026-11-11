"""Storage usage categories remain visible throughout asynchronous inventory updates."""

import pytest
from playwright.sync_api import Route, expect
from test_layout_ui import login
from test_ui import BrowserPage

pytest_plugins = ['test_ui']


@pytest.mark.parametrize('terminal', ['ready', 'unavailable'])
def test_s3_category_cards_stay_visible_during_collection(browser_page: BrowserPage, terminal: str) -> None:
    """Show all measured categories without disclosure state across inventory polls."""
    page, origin, _, errors = browser_page
    state = {'status': 'collecting', 'bytes': 1024}
    requests = []
    labels = ['Publications', 'Run backups', 'Backup copies', 'Trash', 'Checkpoints and indexes']
    groups = ['publications', 'archives', 'backups', 'trash', 'indexes']

    def resources(route: Route) -> None:
        """Return a changing inventory with its last complete category measurements."""
        requests.append(route.request.url)
        route.fulfill(
            json={
                'object_storage': {
                    'status': state['status'],
                    'used_bytes': state['bytes'] * 5,
                    'capacity_bytes': None,
                    'object_count': 5,
                    'sampled_at': '2026-01-01T12:00:00Z',
                    'groups': {key: state['bytes'] for key in groups},
                }
            }
        )

    page.route('**/admin/resources*', resources)
    login(page, origin)
    cards = page.locator('.storage-category-cards .storage-card')
    expect(cards).to_have_count(5)
    expect(page.locator('.storage-breakdown details, details.storage-breakdown')).to_have_count(0)
    expect(page.locator('.storage-category-cards progress')).to_have_count(0)
    for label in labels:
        expect(page.get_by_role('heading', name=label, exact=True)).to_be_visible()
    expect(cards.first).to_contain_text('1 KiB')
    state['bytes'] = 2048
    expect(cards.first).to_contain_text('2 KiB')
    assert len(requests) >= 2
    for label in labels:
        expect(page.get_by_role('heading', name=label, exact=True)).to_be_visible()
    state['status'] = terminal
    main = page.locator('.storage-card').filter(has=page.get_by_role('heading', name='S3 storage', exact=True))
    if terminal == 'unavailable':
        expect(main).to_contain_text('Usage unavailable')
    else:
        expect(main).not_to_contain_text('Collecting usage')
    expect(cards).to_have_count(5)
    expect(cards.first).to_contain_text('2 KiB')
    count = len(requests)
    page.wait_for_timeout(1300)
    assert len(requests) == count
    for width in (1440, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        for label in labels:
            expect(page.get_by_role('heading', name=label, exact=True)).to_be_visible()
    assert not errors, errors


def test_s3_categories_do_not_invent_missing_measurements(browser_page: BrowserPage) -> None:
    """Hide absent category measurements and retain a measured zero for Other."""
    page, origin, _, errors = browser_page
    measured = False

    def resources(route: Route) -> None:
        """Finish an initially unmeasured inventory with one zero-byte category."""
        route.fulfill(
            json={
                'object_storage': {
                    'status': 'ready' if measured else 'collecting',
                    'used_bytes': 0 if measured else None,
                    'capacity_bytes': None,
                    'object_count': 1 if measured else None,
                    'groups': {'other': 0} if measured else {},
                }
            }
        )

    page.route('**/admin/resources*', resources)
    login(page, origin)
    expect(page.get_by_role('heading', name='S3 storage', exact=True)).to_be_visible()
    expect(page.locator('.storage-breakdown')).to_have_count(0)
    measured = True
    card = page.locator('.storage-category-cards .storage-card')
    expect(card).to_have_count(1)
    expect(card).to_contain_text('Other')
    expect(card).to_contain_text('0 B')
    expect(card).not_to_contain_text('Capacity not reported')
    assert not errors, errors
