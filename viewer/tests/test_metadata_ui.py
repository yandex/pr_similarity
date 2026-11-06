"""Rendered generated inference remains independent from exact owner labels."""

from playwright.sync_api import Page, expect
from starlette.applications import Starlette

pytest_plugins = ['test_ui']


def test_setup_prefixes_keep_generated_model_effort_and_origin(
    browser_page: tuple[Page, str, Starlette, list[str]],
) -> None:
    """Rendered labels and filter options separate owner Setup from frozen inference."""
    page, origin, app, _ = browser_page
    svc = app.state.service
    rid = next(key for key in svc.runs if svc.run(key)['display_name'] == 'Agent A')
    labels = ['model:custom', 'effort:low', 'Baseline', 'Baseline']
    svc.patch_metadata(rid, 'alice', {'tags': labels})
    page.goto(origin)
    row = page.locator('tr.run-row').filter(has=page.get_by_role('link', name='Agent A', exact=True))
    expect(row.locator('td').nth(3)).to_have_text('openai/Alpha [high]')
    assert row.locator('td').count() == 12
    assert row.locator('td').nth(4).locator('.tag').all_text_contents() == labels
    row.locator('td').nth(3).locator('.tag').focus()
    expect(page.locator('.unified-popover')).to_contain_text('Alpha')
    expect(page.locator('.unified-popover')).to_contain_text('high')
    page.keyboard.press('Escape')
    assert 'model:custom' in page.locator('#filter-setup option').all_text_contents()
    assert 'custom' not in page.locator('#filter-model option').all_text_contents()
    row.get_by_role('link', name='Agent A', exact=True).click()
    page.get_by_role('button', name='opencode.json', exact=True).click()
    expect(page.locator('#config-dialog')).to_be_visible()
    content = page.locator('#config-content pre').inner_text()
    assert 'reasoningEffort' in content and 'high' in content
    assert 'config_identity.json' not in content and 'inference' not in content
    page.keyboard.press('Escape')
    page.get_by_text('Run setup', exact=True).click()
    expect(page.locator('.mock-context')).to_contain_text('Agent timeout')
