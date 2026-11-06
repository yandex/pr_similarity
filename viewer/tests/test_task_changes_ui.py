"""Task change counters use cached requests and remain responsive on mobile."""

from typing import cast
from urllib.parse import quote

from playwright.sync_api import expect
from test_ui import BrowserPage

pytest_plugins = ['test_ui']


def test_changes_toolbar_and_cache(browser_page: BrowserPage) -> None:
    """Show separate human and agent counts and reuse them after task sorting."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    requests = []
    page.on('request', lambda r: requests.append(r.url) if 'view=changes' in r.url else None)
    page.goto(origin + '/run?run_id=' + rid)
    task = page.locator('.task').first
    task.locator('.task-toggle').click()
    stats = task.locator('.task-change-stats')
    expect(stats).not_to_contain_text('Loading')
    taskid = cast(str, task.get_attribute('data-task-id'))
    data = page.request.get(
        origin + '/api/v1/runs/' + rid + '/tasks/' + quote(taskid, safe='') + '?view=changes'
    ).json()
    for role, label in [('human', 'Human PR:'), ('agent', 'Agent Solution:')]:
        item = stats.locator('[data-side="' + role + '"]')
        expect(item).to_contain_text(label)
        expect(item.locator('.change-added')).to_have_text('+' + str(data[role]['added']))
        expect(item.locator('.change-removed')).to_have_text('−' + str(data[role]['removed']))
    button = task.get_by_role('link', name='Open Human PR', exact=True)
    assert button.get_attribute('target') == '_blank'
    assert cast(dict[str, float], stats.bounding_box())['x'] < cast(dict[str, float], button.bounding_box())['x']
    assert len(requests) == 1
    page.locator('#task-direction').select_option('asc')
    expect(page.locator('#activity')).to_have_text('')
    expect(page.locator('.task-change-stats')).not_to_contain_text('Loading')
    assert len(requests) == 1
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors
