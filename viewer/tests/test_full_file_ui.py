"""Render full source files and empty roles through the browser interface."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect
from test_absent_role_diff import empty_role_archive
from test_ui import BrowserPage

pytest_plugins = ['test_ui']


def test_full_file_api_and_browser(browser_page: BrowserPage, tmp_path: Path) -> None:
    """Display literal source, change markers and responsive full-file panels."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    page.goto(origin + '/run?run_id=' + rid)
    page.locator('.task-toggle').click()
    page.locator('button.file').click()
    page.get_by_role('button', name='Full file', exact=True).click()
    expect(page.locator('.full-file')).to_have_count(2)
    assert page.locator('.diff-panel h4').all_text_contents() == [
        'Human (+1 −0)',
        'Agent (+1 −0)',
    ]
    assert page.locator('.full-line').count() > 0
    expect(page.locator('.file-change-map')).to_have_count(2)
    for panel in page.locator('.full-file-panel').all():
        assert panel.locator('.file-change-mark.added').count() > 0
        assert panel.locator('.file-change-mark.removed').count() == 0
    assert page.locator('.diff-hunk,.diff-header').count() == 0
    assert page.locator('.full-line').first.evaluate('e=>e.getBoundingClientRect().height') < 30
    page.get_by_role('button', name='Close diff', exact=True).click()
    expect(page.locator('button.file')).to_be_focused()
    # Exercise unchanged context, HTML escaping and long-file numbers through the same renderer.
    page.evaluate('''()=>{const p=fullFilePanel('Test',{state:'unchanged',lines:[
        {kind:'context',text:'<script>window.BAD=true</script>',base_line:1,result_line:1},
        {kind:'added',text:'+++ literal code',base_line:null,result_line:2}],
        truncated:false,ends_with_newline:false});p.id='test-full-file';document.body.append(p)}''')
    expect(page.locator('#test-full-file .line-code').first).to_have_text('<script>window.BAD=true</script>\n')
    assert page.evaluate('window.BAD') is None
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors


@pytest.mark.parametrize(
    'exists,expected',
    [
        ((False, True, True), 'Empty file'),
        ((True, False, False), 'Deleted empty file'),
        ((False, False, True), 'File is absent'),
    ],
)
def test_empty_role_views(
    browser_page: BrowserPage, tmp_path: Path, exists: tuple[bool, bool, bool], expected: str
) -> None:
    """Display the source-role existence state for empty archived files."""
    page, origin, app, errors = browser_page
    imported = empty_role_archive(tmp_path / 'empty.tar.gz', exists)
    app.state.service.publish(imported, 'alice', 'empty')
    page.goto(origin + '/run?run_id=' + imported.report.run.id)
    page.locator('.task-toggle').click()
    page.locator('button.file').click()
    page.get_by_role('button', name='Full file', exact=True).click()
    expect(page.locator('.full-file').first).to_contain_text(expected)
    assert not errors, errors


def test_file_view_toggle_requests_and_selection(browser_page: BrowserPage) -> None:
    """Switch both panels through the API and keep the chosen mode on reopen."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    requests = []
    page.on('request', lambda request: requests.append(request.url) if '/files/' in request.url else None)
    page.goto(origin + '/run?run_id=' + rid)
    page.locator('.task-toggle').click()
    opener = page.locator('button.file')
    opener.click()
    diff = page.get_by_role('button', name='Diff', exact=True)
    full = page.get_by_role('button', name='Full file', exact=True)
    expect(diff).to_have_attribute('aria-pressed', 'true')
    expect(full).to_have_attribute('aria-pressed', 'false')
    expect(page.locator('.diff-lines')).to_have_count(2)
    expect(page.locator('.full-file')).to_have_count(0)
    assert 'view' not in parse_qs(urlparse(requests[-1]).query)
    full.focus()
    page.keyboard.press('Space')
    expect(full).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('.full-file')).to_have_count(2)
    assert parse_qs(urlparse(requests[-1]).query)['view'] == ['full']
    page.keyboard.press('Escape')
    expect(opener).to_be_focused()
    opener.click()
    expect(full).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('.full-file')).to_have_count(2)
    diff.click()
    expect(page.locator('.diff-lines')).to_have_count(2)
    expect(page.locator('.file-change-map')).to_have_count(0)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(diff).to_be_visible()
    expect(full).to_be_visible()
    expect(page.get_by_role('button', name='Close diff', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors


def test_file_view_retry_and_stale_response(browser_page: BrowserPage) -> None:
    """Retry the selected mode and reject late responses even if cancellation is ignored."""
    page, origin, app, errors = browser_page
    rid = next(rid for rid, meta in app.state.service.metadata.items() if meta['display_name'] == 'Agent A')
    page.goto(origin + '/run?run_id=' + rid)
    page.locator('.task-toggle').click()
    page.locator('button.file').click()
    expect(page.locator('.diff-lines')).to_have_count(2)
    page.evaluate('''() => {
        const read = api;
        window.api = (path, options) => path.includes('&view=full')
            ? Promise.reject(new Error('Temporary failure')) : read(path, options);
        window.restoreFileApi = () => {window.api = read};
    }''')
    page.get_by_role('button', name='Full file', exact=True).click()
    expect(page.locator('.diff-error')).to_have_text('Temporary failure')
    page.evaluate('restoreFileApi()')
    page.get_by_role('button', name='Retry diff', exact=True).click()
    expect(page.locator('.full-file')).to_have_count(2)
    page.get_by_role('button', name='Diff', exact=True).click()
    expect(page.locator('.diff-lines')).to_have_count(2)
    page.evaluate('''() => {
        const read = api;
        window.api = async (path, options) => {
            if (!path.includes('&view=full')) return read(path, options);
            const data = await read(path);
            return new Promise(resolve => {window.finishOldFileView = () => resolve(data)});
        };
    }''')
    page.get_by_role('button', name='Full file', exact=True).click()
    page.wait_for_function('() => typeof window.finishOldFileView === "function"')
    page.get_by_role('button', name='Diff', exact=True).click()
    expect(page.locator('.diff-lines')).to_have_count(2)
    page.evaluate('async () => {finishOldFileView(); await new Promise(requestAnimationFrame)}')
    expect(page.locator('.diff-lines')).to_have_count(2)
    expect(page.locator('.full-file')).to_have_count(0)
    assert not errors, errors
