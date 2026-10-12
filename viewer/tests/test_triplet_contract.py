"""Direct schema-1 triplet ownership preserves absent states and original file paths."""

from copy import deepcopy

import pytest
from helpers import documents
from viewer.contracts import validate_report, validate_triplet_index
from viewer.errors import ViewerError
from viewer.triplet_paths import triplet_path
from viewer.util import digest


def contract() -> tuple[dict, dict, dict, dict]:
    """Build independent readable role receipts for an added empty Python file."""
    report, _, _ = documents()
    task = report['tasks'][0]
    task['directory'] = 'tasks/001-example-repo-pr-1-12345678'
    prefix = task['directory'] + '/attempts/001/triplets/'
    task['artifacts']['triplets_manifest'] = prefix + 'index.json'
    row = task['scoring']['files'][0]
    row.update(base=None, human=prefix + triplet_path(1, row['path'], 'human'), ai=None)
    row['exists'] = {'base': False, 'human': True, 'ai': False}
    index = {
        'version': 1,
        'run_id': report['run']['id'],
        'task_id': task['id'],
        'attempt_id': task['final_attempt']['id'],
        'source': {'before': task['before'], 'after': task['after']},
        'entries': [
            {
                'path': row['path'],
                'number': 1,
                'exists': row['exists'].copy(),
                'modes': {'base': None, 'human': '100644', 'ai': None},
                'files': {
                    'base': None,
                    'ai': None,
                    'human': {'path': triplet_path(1, row['path'], 'human'), 'size_bytes': 0, 'sha256': digest(b'')},
                },
            }
        ],
    }
    inventory = {row['human']: {'path': row['human'], 'size_bytes': 0, 'sha256': digest(b'')}}
    return report, task, index, inventory


def test_empty_present_file_and_absent_roles_have_distinct_refs() -> None:
    """Readable refs preserve extension and absent roles are genuinely null."""
    report, task, index, inventory = contract()
    validate_report(report)
    validate_triplet_index(index, task, inventory, run_id=report['run']['id'])


@pytest.mark.parametrize('field', ['run_id', 'task_id', 'attempt_id', 'source'])
def test_each_triplet_owner_is_bound(field: str) -> None:
    """A matching archive hash cannot legitimize another run, task, attempt or revision."""
    report, task, index, inventory = contract()
    index[field] = {'before': 'foreign', 'after': task['after']} if field == 'source' else 'foreign'
    with pytest.raises(ViewerError):
        validate_triplet_index(index, task, inventory, run_id=report['run']['id'])


@pytest.mark.parametrize(
    'mutation',
    [
        'old_root',
        'extra_root',
        'duplicate',
        'numbered',
        'extension',
        'digest',
        'size_bool',
        'absent_receipt',
        'absent_mode',
        'absent_file_bytes',
        'present_null',
        'traversal',
    ],
)
def test_invalid_states_or_receipts_are_rejected(mutation: str) -> None:
    """Schema number alone never admits the old numbered scheme or forged file evidence."""
    report, task, index, inventory = contract()
    entry = index['entries'][0]
    receipt = entry['files']['human']
    if mutation == 'old_root':
        del index['run_id']
    if mutation == 'extra_root':
        index['private'] = {}
    if mutation == 'duplicate':
        index['entries'].append(deepcopy(entry))
    if mutation == 'numbered':
        receipt['path'] = '000001/human'
    if mutation == 'extension':
        receipt['path'] = receipt['path'].replace('.py', '.txt')
    if mutation == 'digest':
        receipt['sha256'] = 'f' * 64
    if mutation == 'size_bool':
        receipt['size_bytes'] = False
    if mutation == 'absent_receipt':
        entry['files']['base'] = deepcopy(receipt)
    if mutation == 'absent_mode':
        entry['modes']['base'] = '100644'
    if mutation == 'present_null':
        entry['files']['human'] = None
    if mutation == 'absent_file_bytes':
        prefix = task['artifacts']['triplets_manifest'].removesuffix('index.json')
        inventory[prefix + triplet_path(1, entry['path'], 'base')] = {'size_bytes': 0, 'sha256': digest(b'')}
    if mutation == 'traversal':
        entry['path'] = '../empty.py'
    with pytest.raises(ViewerError):
        validate_triplet_index(index, task, inventory, run_id=report['run']['id'])


def test_report_cannot_route_roles_to_a_foreign_or_numbered_tree() -> None:
    """Report refs must use the registered directory and selected sequence."""
    report, _, _, _ = contract()
    for foreign in [
        'logs/agent.log',
        'tasks/002-foreign/attempts/001/triplets/human/new/empty.py',
        'tasks/001-example-repo-pr-1-12345678/attempts/001/triplets/000001/human',
    ]:
        modified = deepcopy(report)
        modified['tasks'][0]['scoring']['files'][0]['human'] = foreign
        with pytest.raises(ViewerError):
            validate_report(modified)
