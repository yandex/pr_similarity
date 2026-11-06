"""Task metrics distinguish available, partial and unavailable measurements."""

from playwright.sync_api import expect
from test_ui import BrowserPage

pytest_plugins = ['test_ui']


def test_task_metric_states(browser_page: BrowserPage) -> None:
    """Keep metric popovers separate from task expansion for every availability state."""
    page, origin, app, errors = browser_page
    for name in ('Agent A', 'Agent B', 'Agent C'):
        rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == name)
        page.goto(origin + '/run?run_id=' + rid)
        task = page.locator('.task').first
        task.wait_for()
        assert task.locator('summary .badge').first.inner_text() in (
            'OK',
            'No-op',
            'Timeout',
            'Fail',
        )
        assert task.locator('[title],.metric-note').count() == 0
        summary = task.locator(':scope > summary')
        summary.locator('[data-metric="tokens"] button').click()
        assert not task.evaluate('e=>e.open')
        expect(page.locator('.unified-popover:visible')).to_have_count(1)
        page.keyboard.press('Escape')
        summary.locator('.task-toggle').click()
        expect(task.locator('.condition')).to_be_visible()
        assert task.locator('[title],.metric-note').count() == 0
        assert '(partial)' not in task.inner_text() and '(!)' not in task.inner_text()
        if name == 'Agent C':
            expect(summary.locator('[data-metric="tokens"] .usage-status')).to_have_text('Unavailable')
        summary.locator('[data-metric="tokens"] button').click()
        expect(page.locator('.unified-popover:visible')).to_contain_text('Final attempts only')
        page.keyboard.press('Escape')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.set_viewport_size({'width': 1280, 'height': 900})
    assert not errors, errors
