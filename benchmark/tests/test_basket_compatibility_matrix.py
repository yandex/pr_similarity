"""Published basket variants preserve exact frozen evidence across population combinations."""

import json
from pathlib import Path

import pytest

from benchmark.persistence.storage import canonical_bytes as encode
from benchmark.persistence.storage import sha256 as hash_json
from benchmark.validation.basket import load_basket


def document(provider: str, version: int, partial: bool, golden: bool, reused: bool) -> dict:
    """Build current populations with three failures and one selected rejection."""
    path = Path(__file__).parent / f'fixtures/basket-v{version}' / f'{provider}.json'
    data = json.loads(path.read_text())
    meta = data['meta']
    if partial:
        meta['partial'] = True
        if version == 1:
            meta['omitted_failed_ids'] = ['change:' + 'e' * 64]
        else:
            meta['omissions'] = [
                {
                    'id': 'change:' + code * 64,
                    'stage': stage,
                    'kind': kind,
                    'code': 'fixture',
                    'result_sha256': 'f' * 64,
                }
                for code, stage, kind in (
                    ('e', 'collect', 'failed'),
                    ('f', 'filter', 'failed'),
                    ('a', 'generate', 'failed'),
                    ('b', 'generate', 'rejected'),
                )
            ]
            coverage = meta['coverage']
            coverage['discovery'].update(matched=5, selected=5)
            coverage['collect'].update(input=5, succeeded=4, failed=1)
            coverage['filter'].update(input=4, succeeded=3, failed=1)
            coverage['generate'].update(input=3, succeeded=1, failed=1, rejected=1)
            coverage['selected'] = 3
            selection = meta['selection']
            selection['selected_ids'].extend(['change:' + 'a' * 64, 'change:' + 'b' * 64])
            selection['generation_keys'].update({identity: 'f' * 64 for identity in selection['selected_ids'][1:]})
            selection['fingerprint'] = hash_json(
                {
                    'tasks': [
                        [identity, selection['generation_keys'][identity]] for identity in selection['selected_ids']
                    ]
                }
            )
    if reused:
        data['tasks'][0]['provenance']['generation']['reused_from'] = 'e' * 64
    if golden:
        meta['settings']['include_golden_diff'] = True
        meta['settings_hash'] = hash_json(meta['settings'])
        data['tasks'][0]['golden_diff'] = 'diff --git a/new/empty.py b/new/empty.py\nnew file mode 100644\n'
    return data


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
@pytest.mark.parametrize(('version', 'reused'), [(1, False), (2, False), (2, True), (3, False), (3, True)])
@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('golden', [False, True])
def test_complete_compatibility_matrix(
    tmp_path: Path, provider: str, version: int, reused: bool, partial: bool, golden: bool
) -> None:
    """Format-specific metadata never drops optional evidence or changes source task bytes."""
    data = document(provider, version, partial, golden, reused)
    content = encode(data)
    path = tmp_path / 'basket.json'
    path.write_bytes(content)
    basket = load_basket(path)
    assert basket.content == content
    assert [row.to_wire() for row in basket.rows] == data['tasks']
    assert basket.failed_count == ((1 if version == 1 else 3) if partial else 0)
    assert basket.rejected_count == (1 if partial and version in (2, 3) else 0)
    assert encode(data) == content
