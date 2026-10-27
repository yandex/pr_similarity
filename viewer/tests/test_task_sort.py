"""Task ordering is global, stable and independent of catalogue aggregation filters."""

from copy import deepcopy
from pathlib import Path

import pytest
from helpers import PrepareCallback, archive_fixture, service_for
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient
from test_api import client as client
from test_api import create_user
from test_metrics import task

from viewer.models.evidence import TripletIndex
from viewer.presentation.queries import TaskQuery, public_task
from viewer.util import digest, json_bytes, parse_json
from viewer.validation.archive import validate_archive
from viewer.validation.basket_sources import task_identity

__all__ = [
    'client',
    'test_null_last_raw_precision_zero_and_stable_ties',
    'test_unavailable_score_is_null_and_file_aggregation_changes_sort',
    'prepare_tasks',
    'publish_tasks',
    'test_global_sort_before_pagination_default_order_and_report_binding',
    'test_same_report_overwrite_with_new_triplets_is_a_new_publication',
]


@pytest.mark.parametrize('key', ['score', 'total_tokens', 'time'])
def test_null_last_raw_precision_zero_and_stable_ties(key: str) -> None:
    """Verify null last raw precision zero and stable ties."""

    def row(id: str, value: object) -> dict:
        """Assign the same nullable sort value to score, tokens and duration."""
        return {'task_id': id, 'score': value, 'tokens': {'total': {'amount': value}}, 'time': {'total_seconds': value}}

    rows = [row('unknown', None), row('first', 0.50001), row('second', 0.50002), row('equal', 0.50001), row('zero', 0)]
    assert [r['task_id'] for r in TaskQuery(QueryParams('sort=' + key + '&direction=asc')).sorted(rows)] == [
        'zero',
        'first',
        'equal',
        'second',
        'unknown',
    ]
    assert [r['task_id'] for r in TaskQuery(QueryParams('sort=' + key + '&direction=desc')).sorted(rows)] == [
        'second',
        'first',
        'equal',
        'zero',
        'unknown',
    ]
    query = TaskQuery(QueryParams('sort=task_order&direction=desc'))
    assert query.direction == 'asc' and query.sorted(rows) == rows


def test_unavailable_score_is_null_and_file_aggregation_changes_sort() -> None:
    """Verify unavailable score is null and file aggregation changes sort."""
    a = task(score=0.2)
    a.id = 'a'
    a.scoring.LCf_mlp = 0.8
    b = task(score=0.7)
    b.id = 'b'
    b.scoring.LCf_mlp = 0.3
    q = TaskQuery(QueryParams('sort=score'))
    assert [r['task_id'] for r in q.sorted([public_task(a, 'Mf'), public_task(b, 'Mf')])] == ['b', 'a']
    assert [r['task_id'] for r in q.sorted([public_task(a, 'LCf'), public_task(b, 'LCf')])] == ['a', 'b']
    a.scoring.status = 'unavailable'
    a.scoring.Mf_mlp = 0.9
    assert public_task(a, 'Mf')['score'] is None


def prepare_tasks(count: int) -> PrepareCallback:
    """Create a callback that expands the fixture into independently identified tasks."""

    def prepare(report: dict, basket: dict) -> None:
        """Populate matching report and basket tasks with varied scores and missing metrics."""
        original = report['tasks'][0]
        source = basket['tasks'][0]
        rows = []
        sources = []
        old_root = original['artifacts']['triplets_manifest'].removesuffix('index.json').split('/triplets/')[0] + '/'

        def remap(value: object, new_root: str) -> object:
            """Recursively replace the original attempt prefix in nested fixture values."""
            if isinstance(value, str):
                return value.replace(old_root, new_root)
            if isinstance(value, dict):
                return {k: remap(v, new_root) for k, v in value.items()}
            if isinstance(value, list):
                return [remap(v, new_root) for v in value]
            return value

        for i in range(count):
            id = task_identity(basket['meta']['repository'], source['change_request']['kind'], i + 1)
            root = 'attempts/' + digest(id.encode()) + '/' + original['final_attempt']['id'] + '/'
            row = remap(deepcopy(original), root)
            assert isinstance(row, dict)
            entry = deepcopy(source)
            row['id'] = entry['id'] = id
            row['number'] = entry['change_request']['number'] = i + 1
            row['url'] = entry['change_request']['url'] = 'https://github.com/example/repo/pull/' + str(i + 1)
            score = i / (count - 1)
            row['scoring'].update(Mf_mlp=score, LCf_mlp=score)
            row['scoring']['files'][0]['mlp'] = score
            row['telemetry']['tokens']['total'] = [None, 0, 100, 20][i % 4]
            row['execution']['duration_seconds'] = [None, 0, 10, 5][i % 4]
            rows.append(row)
            sources.append(entry)
        rows[-1]['execution']['status'] = 'error'
        report['tasks'] = rows
        basket['tasks'] = sources
        report['basket']['total_tasks'] = count
        basket['meta']['counts'].update(input=count, succeeded=count)
        coverage = basket['meta']['coverage']
        coverage['discovery'].update(matched=count, selected=count, capped=False)
        for stage in ('collect', 'filter', 'generate'):
            coverage[stage].update(input=count, succeeded=count)
        coverage['selected'] = count
        selection = basket['meta']['selection']
        selection['selected_ids'] = [source['id'] for source in sources]
        selection['generation_keys'] = {
            source['id']: source['provenance']['generation']['input_sha256'] for source in sources
        }
        selection['fingerprint'] = digest(
            json_bytes(
                {
                    'tasks': [
                        [identity, selection['generation_keys'][identity]] for identity in selection['selected_ids']
                    ]
                }
            )
        )
        report['summary'].update(
            total_tasks=count, completed_tasks=count, nonempty_diff_tasks=count, scored_tasks=count, error_tasks=1
        )

    return prepare


def publish_tasks(client: TestClient, tmp_path: Path, count: int = 207) -> tuple[dict, dict]:
    """Publish a multi-task archive and return its report and manifest."""
    create_user(client)
    path = tmp_path / 'tasks.tar.gz'
    report, manifest = archive_fixture(path, prepare=prepare_tasks(count))
    svc = service_for(client)
    svc.publish(validate_archive(path, tmp_path / 'data', svc.config.limits), 'alice', 'tasks')
    return report, manifest


def test_global_sort_before_pagination_default_order_and_report_binding(client: TestClient, tmp_path: Path) -> None:
    """Verify global sort before pagination default order and report binding."""
    report, manifest = publish_tasks(client, tmp_path)
    rid = report['run']['id']
    base = '/api/v1/runs/' + rid
    original = client.get(base + '/tasks?direction=desc&limit=1').json()
    assert original['items'][0]['task_id'] == report['tasks'][0]['id'] and original['direction'] == 'asc'
    result = client.get(base + '/tasks?sort=score&direction=desc&offset=50&limit=1&statuses=').json()
    assert result['items'][0]['task_id'] == report['tasks'][-51]['id']
    assert result['total'] == 207 and result['report_sha256'] == manifest['report_sha256']
    for key in ('score', 'total_tokens', 'time'):
        rows = client.get(base + '/tasks?sort=' + key + '&direction=asc&limit=200').json()['items']
        value = (
            rows[0]['score']
            if key == 'score'
            else rows[0]['tokens']['total']['amount'] if key == 'total_tokens' else rows[0]['time']['total_seconds']
        )
        assert value == 0
    for query in ('sort=cost', 'direction=bad', 'sort=total_tokens&direction=bad'):
        assert client.get(base + '/tasks?' + query).status_code == 422
    assert client.get('/api/v1/baskets?sort=cost').status_code == 200
    task_id = report['tasks'][0]['id']
    stale = '?expected_report_sha256=' + 'f' * 64
    assert client.get(base + '/tasks' + stale).status_code == 409
    assert client.get(base + '/tasks/' + task_id + stale).status_code == 409
    detail = client.get(base + '/tasks/' + task_id).json()
    assert client.get(base + '/tasks/' + task_id + '/files/' + detail['files'][0]['file_id'] + stale).status_code == 409
    old_sha = manifest['report_sha256']
    path = tmp_path / 'overwrite.tar.gz'

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Change token totals to invalidate the previously observed report digest."""
        report['tasks'][0]['telemetry']['tokens']['total'] = 999

    archive_fixture(path, prepare=prepare_tasks(207), configure=change)
    svc = service_for(client)
    svc.publish(
        validate_archive(path, tmp_path / 'overwritten', svc.config.limits), 'alice', 'overwrite', overwrite=True
    )
    assert client.get(base + '/tasks?expected_report_sha256=' + old_sha).status_code == 409
    assert client.get(base).json()['report_sha256'] != old_sha


def test_same_report_overwrite_with_new_triplets_is_a_new_publication(client: TestClient, tmp_path: Path) -> None:
    """Verify same report overwrite with new triplets is a new publication."""
    create_user(client)
    svc = service_for(client)
    original = tmp_path / 'first.tar.gz'
    report, manifest = archive_fixture(original)
    svc.publish(validate_archive(original, tmp_path / 'first', svc.config.limits), 'alice', 'first')
    rid = report['run']['id']
    task = report['tasks'][0]
    base = '/api/v1/runs/' + rid
    old_publication = digest(original.read_bytes())

    def change(report: dict, basket: dict, files: dict[str, bytes]) -> None:
        """Replace agent source bytes and update their triplet receipt."""
        task = report['tasks'][0]
        row = task['scoring']['files'][0]
        data = b'print("replacement")\n'
        files[row['ai']] = data
        path = task['artifacts']['triplets_manifest']
        index = TripletIndex.model_validate(parse_json(files[path]))
        receipt = index.entries[0].files.ai
        assert receipt is not None
        receipt.size_bytes = len(data)
        receipt.sha256 = digest(data)
        files[path] = json_bytes(index.to_wire())

    replacement = tmp_path / 'replacement.tar.gz'
    _, new_manifest = archive_fixture(replacement, change=change)
    assert new_manifest['report_sha256'] == manifest['report_sha256']
    svc.publish(
        validate_archive(replacement, tmp_path / 'replacement', svc.config.limits), 'alice', 'replace', overwrite=True
    )
    query = '?expected_report_sha256=' + manifest['report_sha256'] + '&expected_publication_sha256=' + old_publication
    assert client.get(base + '/tasks' + query).status_code == 409
    assert client.get(base + '/tasks/' + task['id'] + query).status_code == 409
    file = client.get(base + '/tasks/' + task['id']).json()['files'][0]
    assert client.get(base + '/tasks/' + task['id'] + '/files/' + file['file_id'] + query).status_code == 409
    assert client.get(base + '/config' + query).status_code == 409
    assert client.get(base).json()['publication_sha256'] != old_publication
