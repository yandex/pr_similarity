"""Observable aggregation rules; fixtures are independent of the producer runtime."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from viewer.metrics import aggregate, parse_tags, tag_groups, task_status


def task(status='OK', score=1, loc=1, cost=.1, seconds=10):
    row = json.loads((Path(__file__).parent / 'fixtures/completed.json').read_text())['tasks'][0]
    row['execution']['status'] = {'OK': 'completed', 'no-op': 'completed', 'timeout': 'timeout', 'fail': 'error'}[status]
    row['changes']['status'] = 'empty' if status == 'no-op' else 'nonempty'
    row['scoring'].update(Mf_mlp=score, LCf_mlp=score)
    row['scoring']['counts']['loc_weight'] = loc
    row['telemetry']['cost']['amount'] = cost
    row['execution']['duration_seconds'] = seconds
    return row


@pytest.mark.parametrize('status', ['OK', 'no-op', 'timeout', 'fail'])
def test_status(status):
    assert task_status(task(status)) == status


def test_status_priority_and_unknown():
    row = task('timeout'); row['infrastructure_error'] = {'code': 'setup'}
    assert task_status(row) == 'fail'
    row = task(); row['processing_status'] = 'interrupted'
    assert task_status(row) == 'fail'
    row = task(); row['changes']['status'] = 'unknown'
    assert task_status(row) == 'fail'


def test_selection_only_changes_aggregates():
    rows = [task('OK', .8, cost=1), task('timeout', .2, cost=2), task('fail', 0, cost=100)]
    snapshot = deepcopy(rows)
    result = aggregate(rows)
    assert result['score'] == pytest.approx(.5)
    assert result['cost']['amount'] == 3
    assert result['selected_tasks'] == 2
    assert result['total_tasks'] == 3
    assert rows == snapshot
    assert aggregate(rows, statuses=[])['cost']['amount'] is None


@pytest.mark.parametrize('pr,expected', [('Mpr', .5), ('FCpr', .75), ('LCpr', 2/3)])
@pytest.mark.parametrize('file', ['Mf', 'LCf'])
def test_six_aggregations(pr, expected, file):
    a, b = task(score=0, loc=1), task(score=1, loc=2)
    b['scoring']['counts']['human_files'] = 3
    assert aggregate([a,b], pr_aggregation=pr, file_aggregation=file)['score'] == pytest.approx(expected)


def test_empty_zero_weight_and_micro_f1():
    assert aggregate([])['score'] is None
    a = task(loc=0); a['scoring']['counts'].update(human_files=0, agent_files=0, matched_files=0)
    assert aggregate([a])['score'] is None
    assert aggregate([a])['f1_files'] == 1
    a['scoring']['counts'].update(human_files=3, agent_files=4, matched_files=2)
    assert aggregate([a])['f1_files'] == pytest.approx(4/7)
    a['scoring']['status'] = 'unavailable'
    assert aggregate([a])['scored_tasks'] == 0
    assert aggregate([a])['score'] is None


def test_reprice_priority_partial_mixed_and_unknown_tokens():
    a, b = task(cost=2), task(cost=3)
    b['telemetry']['api_equivalent'] = {'status':'complete','amount':1}
    result = aggregate([a,b])
    assert result['cost']['amount'] == 3
    assert result['cost']['sources'] == {'opencode':1, 'reprice':1}
    b['telemetry']['api_equivalent']['status'] = 'partial'
    result = aggregate([a,b])
    assert result['cost']['amount'] is None
    assert result['cost']['known_subtotal'] == 2
    assert result['cost']['known_tasks'] == 1
    assert result['tokens']['cache_read']['amount'] is None
    assert result['tokens']['input']['amount'] == 200


def test_median_known_times_not_retries_or_setup():
    rows = [task(seconds=i) for i in [1, 2, 100, None]]
    assert aggregate(rows)['time'] == {'median_seconds':2, 'total_seconds':None, 'known_subtotal_seconds':103, 'known_tasks':3, 'total_tasks':4}
    assert aggregate(rows[:2])['time']['median_seconds'] == 1.5


def test_tags_are_ordered_literal_and_pinned_independent():
    tags = ['model: A','effort: high','setup:x','sys:pinned','Model: B','model: A','model: ']
    assert parse_tags(tags) == {'model':[],'setup':tags}


def test_catalogue_snapshot_matches_every_selection():
    from viewer.metrics import aggregate_snapshot, STATUSES
    rows=[task(status,score=i/4,loc=i+1,cost=i,seconds=i) for i,status in enumerate(STATUSES)]
    snapshot=aggregate_snapshot(rows)
    for mask in range(16):
        statuses=[s for i,s in enumerate(STATUSES) if mask&(1<<i)]
        for pr in ('Mpr','FCpr','LCpr'):
            for file in ('Mf','LCf'):
                expected=aggregate(rows,statuses,pr,file)
                assert snapshot[mask]['scores'][pr+'/'+file]==expected['score']
                for key in ('f1_files','cost','tokens','time','selected_tasks','scored_tasks'):
                    assert snapshot[mask][key]==expected[key]


def test_mf_lcf_have_distinct_values_under_all_three_pr_weights():
    a,b=task(score=.2,loc=1),task(score=.8,loc=24)
    a['scoring']['LCf_mlp']=.4;b['scoring']['LCf_mlp']=.9
    b['scoring']['counts']['human_files']=3
    for pr,mf,lcf in [('Mpr',.5,.65),('FCpr',.65,.775),('LCpr',.776,.88)]:
        assert aggregate([a,b],pr_aggregation=pr,file_aggregation='Mf')['score']==pytest.approx(mf)
        assert aggregate([a,b],pr_aggregation=pr,file_aggregation='LCf')['score']==pytest.approx(lcf)


def test_total_agent_time_and_reported_token_total_respect_selection():
    a, b, c = task(seconds=3), task('timeout', seconds=7), task('fail', seconds=100)
    for row, total in zip((a, b, c), (123, 456, 999)):
        row['telemetry']['tokens']['total'] = total
    result = aggregate([a,b,c])
    assert result['time']['total_seconds'] == 10
    assert result['tokens']['total']['amount'] == 579
    assert aggregate([a,b,c], statuses=['OK'])['time']['total_seconds'] == 3
    b['execution']['duration_seconds'] = None
    b['telemetry']['tokens']['total'] = None
    assert aggregate([a,b])['time']['total_seconds'] is None
    assert aggregate([a,b])['tokens']['total']['amount'] is None
    assert aggregate([a,b], statuses=[])['time']['total_seconds'] is None


def test_generated_groups_are_separate_from_setup_prefixes() -> None:
    """Owner labels cannot impersonate generated inference metadata."""
    inference = {'configured': {'model': {'status': 'known', 'value': 'openai/selected'},
                                'effort': {'status': 'known', 'value': 'medium'}}}
    tags = ['model:custom', 'effort:high', 'Baseline', 'Baseline']
    assert tag_groups(tags, inference) == {'model': ['openai/selected [medium]'], 'effort': ['medium'], 'setup': tags}
