"""Public snapshots cannot override their native facts or accepted operation selections."""

from pathlib import Path

import pytest
from helpers import archive_fixture

from viewer.config import Limits
from viewer.errors import ViewerError
from viewer.validation.archive import validate_archive


@pytest.mark.parametrize('field', ['score', 'costs', 'status', 'runtime', 'metric', 'name', 'tags', 'phase'])
def test_rehashed_inline_projection_cannot_change_saved_evidence(tmp_path: Path, field: str) -> None:
    """Consistent tar hashes do not make an inline projection a source of experiment truth."""

    def corrupt(report: dict, _basket: dict, _files: dict[str, bytes]) -> None:
        """Change only public snapshot fields after native and control facts were frozen."""
        if field == 'score':
            report['tasks'][0]['scoring'].update(Mf_mlp=0.4, LCf_mlp=0.4)
            report['tasks'][0]['scoring']['files'][0]['mlp'] = 0.4
        elif field == 'costs':
            for scope in report['costs'].values():
                for value in scope.values():
                    value['known_subtotal'] = '99'
        elif field == 'status':
            report['run']['status'] = 'failed'
            report['run']['error'] = {'code': 'forged', 'message': 'A forged control error.'}
        elif field == 'runtime':
            report['agent']['resolved_runtime']['binary_sha256'] = 'f' * 64
        elif field == 'metric':
            report['metric']['version'] = 'unsupported-metric-version'
        elif field == 'name':
            report['agent']['name'] = 'Different frozen agent'
        elif field == 'tags':
            report['run']['tags'] = ['forged-label']
        elif field == 'phase':
            report['tasks'][0]['progress']['phase'] = 'scoring'

    path = tmp_path / 'changed.tar.gz'
    archive_fixture(path, change=corrupt)
    with pytest.raises(ViewerError):
        validate_archive(path, tmp_path / 'data', Limits())
