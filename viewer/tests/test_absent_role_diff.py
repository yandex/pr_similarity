"""Real null triplet refs retain empty add/delete behavior in anonymous API and browser diffs."""

from pathlib import Path

import pytest
from helpers import archive_fixture
from playwright.sync_api import Page, expect
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_api import create_user
from viewer.archive import ImportedRun, validate_archive
from viewer.config import Limits
from viewer.util import digest, json_bytes, parse_json

pytest_plugins = ['test_api', 'test_ui']
_ROLES = ('base', 'human', 'ai')


def empty_role_archive(path: Path, exists: tuple[bool, bool, bool]) -> ImportedRun:
    """Create real empty files only for existing roles and preserve absent states as null."""

    def prepare(report: dict, basket: dict) -> None:
        """Declare add/delete behavior with consistent score counts for the empty fixture."""
        report['agent']['name'] = 'Absent role fixture'
        row = report['tasks'][0]['scoring']['files'][0]
        row['exists'] = dict(zip(_ROLES, exists))
        human_changed, agent_changed = exists[0] != exists[1], exists[0] != exists[2]
        score = float(human_changed and agent_changed)
        row.update(
            changed_human=human_changed,
            changed_agent=agent_changed,
            human_loc=0,
            agent_loc=0,
            loc_weight=int(human_changed),
            mlp=score,
        )
        report['tasks'][0]['scoring'].update(Mf_mlp=score, LCf_mlp=score)
        report['tasks'][0]['scoring']['counts'] = {
            'human_files': int(human_changed),
            'agent_files': int(agent_changed),
            'matched_files': int(human_changed and agent_changed),
            'missed_files': int(human_changed and not agent_changed),
            'extra_files': int(agent_changed and not human_changed),
            'human_loc': 0,
            'agent_loc': 0,
            'loc_weight': int(human_changed),
        }

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Freeze empty bytes and update exact manifest-owned role receipts."""
        task = report['tasks'][0]
        index_ref = task['artifacts']['triplets_manifest']
        index = parse_json(files[index_ref])
        row = task['scoring']['files'][0]
        for role in _ROLES:
            if row['exists'][role]:
                files[row[role]] = b''
                index['entries'][0]['files'][role].update(size_bytes=0, sha256=digest(b''))
        files[index_ref] = json_bytes(index)

    archive_fixture(path, prepare=prepare, change=change)
    return validate_archive(path, path.with_suffix('.data'), Limits())


@pytest.mark.parametrize(
    'exists', [(False, True, True), (True, False, False), (True, True, False), (False, False, True)]
)
def test_null_roles_have_readable_api_diff_and_three_pair_source_states(
    client: TestClient, tmp_path: Path, exists: tuple[bool, bool, bool]
) -> None:
    """All three source pairs resolve explicit absence separately from real empty bytes."""
    create_user(client)
    imported = empty_role_archive(tmp_path / 'empty.tar.gz', exists)
    assert isinstance(client.app, Starlette)
    svc = client.app.state.service
    svc.publish(imported, 'alice', 'empty')
    report = imported.report
    task = report['tasks'][0]
    row = task['scoring']['files'][0]
    response = client.get(
        '/api/v1/runs/' + report['run']['id'] + '/tasks/' + task['id'] + '/files/' + digest(row['path'].encode())
    )
    assert response.status_code == 200
    value = response.json()
    assert value['exists'] == dict(zip(_ROLES, exists))
    for role, key in [('human', 'human_diff'), ('ai', 'agent_diff')]:
        expected = (
            ''
            if row['exists']['base'] == row['exists'][role]
            else '[File ' + ('added' if row['exists'][role] else 'deleted') + '; empty content]\n'
        )
        assert value[key] == {'text': expected, 'truncated': False}
    for left, right in [('base', 'human'), ('base', 'ai'), ('human', 'ai')]:
        for role in (left, right):
            if row['exists'][role]:
                assert row[role] is not None and (imported.directory / row[role]).read_bytes() == b''
            else:
                assert row[role] is None


@pytest.mark.parametrize('exists, action', [((False, True, True), 'added'), ((True, False, False), 'deleted')])
def test_browser_renders_empty_add_delete_with_real_null_refs(
    browser_page: tuple[Page, str, Starlette, list[str]],
    tmp_path: Path,
    exists: tuple[bool, bool, bool],
    action: str,
) -> None:
    """Human and Agent panels render accepted absent-role archives without API or JavaScript errors."""
    page, origin, app, _ = browser_page
    imported = empty_role_archive(tmp_path / 'browser-empty.tar.gz', exists)
    app.state.service.publish(imported, 'alice', 'browser-empty')
    page.goto(origin + '/run?run_id=' + imported.report['run']['id'])
    page.locator('.task-toggle').click()
    page.locator('button.file').click()
    expect(page.locator('.diff-grid')).to_be_visible()
    assert page.locator('.diff-panel h4').all_text_contents() == ['Human diff', 'Agent diff']
    for panel in page.locator('.diff-panel pre').all():
        expect(panel).to_contain_text('[File ' + action + '; empty content]')
    assert page.locator('.diff-error').count() == 0
