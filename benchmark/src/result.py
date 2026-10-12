"""Pure report rows, independent counters and arithmetic validation."""

import copy
import math

from benchmark.contracts import validate_document
from benchmark.errors import BenchmarkError
from benchmark.sources import SourceDescriptor
from benchmark.telemetry import unavailable_telemetry


def unavailable_scoring(status: str = 'pending', error: dict | None = None) -> dict:
    """Describe an uncomputed score without inventing numeric results."""
    if status == 'unavailable' and error is None:
        error = {'code': 'scoring_not_available', 'message': 'This attempt has no complete scored triplets.'}
    if error is not None:
        error = {key: error[key] for key in ('code', 'message')}
    return {'status': status, 'error': error, 'Mf_mlp': None, 'LCf_mlp': None, 'counts': None, 'files': []}


def pending_task(row: dict) -> dict:
    """Keep every basket row, including tasks which never started."""
    return {
        'id': row['id'],
        'repository': row['change_request']['repository'],
        'number': row['change_request']['number'],
        'url': row['change_request']['url'],
        'title': row['change_request']['title'],
        'kind': row['change_request']['kind'],
        'source': copy.deepcopy(row['source']),
        'diff_policy': row['diff_policy'],
        'before': row['revisions']['before'],
        'after': row['revisions']['after'],
        'before_tree': row['revisions']['before_tree'],
        'after_tree': row['revisions']['after_tree'],
        'workspace': row['workspace'],
        'agent_task': row['agent_task'],
        'processing_status': 'pending',
        'final_attempt': None,
        'setup': {'status': 'not_started', 'exit_code': None, 'duration_seconds': None},
        'execution': {'status': 'not_started', 'exit_code': None, 'duration_seconds': None},
        'infrastructure_error': None,
        'changes': {'status': 'unknown', 'files_count': None, 'diff': None},
        'scoring': unavailable_scoring(),
        'telemetry': unavailable_telemetry(),
        'artifacts': {
            'outcome': None,
            'triplets_manifest': None,
            'setup_log': None,
            'agent_events': None,
            'agent_log': None,
            'effective_config': {'status': 'not_started', 'path': None, 'message': None},
        },
    }


def summary(tasks: list[dict]) -> dict:
    """Count execution, changes and scoring as independent result axes."""
    result = {'total_tasks': len(tasks)}
    for state, key in [
        ('completed', 'completed_tasks'),
        ('pending', 'pending_tasks'),
        ('running', 'active_tasks'),
        ('retry_pending', 'retry_pending_tasks'),
        ('interrupted', 'interrupted_tasks'),
    ]:
        result[key] = sum(row['processing_status'] == state for row in tasks)
    result['infrastructure_error_tasks'] = sum(row['infrastructure_error'] is not None for row in tasks)
    for state, key in [('timeout', 'timeout_tasks'), ('error', 'error_tasks'), ('cancelled', 'cancelled_tasks')]:
        result[key] = sum(row['execution']['status'] == state for row in tasks)
    for state, key in [
        ('nonempty', 'nonempty_diff_tasks'),
        ('empty', 'empty_diff_tasks'),
        ('unknown', 'unknown_diff_tasks'),
    ]:
        result[key] = sum(row['changes']['status'] == state for row in tasks)
    result['scored_tasks'] = sum(row['scoring']['status'] == 'available' for row in tasks)
    return result


def validate_report(report: dict) -> None:
    """Validate semantic counts and aggregate scores after schema validation."""
    validate_document('report', report)
    if report['summary'] != summary(report['tasks']) or len(report['tasks']) != report['basket']['total_tasks']:
        raise BenchmarkError('invalid_report', 'Report summary does not match its complete PR queue.', exit_code=1)
    ids = [row['id'] for row in report['tasks']]
    if len(ids) != len(set(ids)):
        raise BenchmarkError('invalid_report', 'Report contains duplicate PR identities.', exit_code=1)
    for row in report['tasks']:
        SourceDescriptor.from_mapping(row['source'])
        score = row['scoring']
        if score['status'] != 'available':
            continue
        files = score['files']
        paths = [item['path'] for item in files]
        if paths != sorted(set(paths)):
            raise BenchmarkError('invalid_report', 'Scoring file paths must be sorted and unique.', exit_code=1)
        counts = {
            'human_files': sum(item['changed_human'] for item in files),
            'agent_files': sum(item['changed_agent'] for item in files),
            'matched_files': sum(item['changed_human'] and item['changed_agent'] for item in files),
            'missed_files': sum(item['changed_human'] and not item['changed_agent'] for item in files),
            'extra_files': sum(item['changed_agent'] and not item['changed_human'] for item in files),
            'human_loc': sum(item['human_loc'] for item in files),
            'agent_loc': sum(item['agent_loc'] for item in files),
            'loc_weight': sum(item['loc_weight'] for item in files),
        }
        if counts != score['counts']:
            raise BenchmarkError('invalid_report', 'Scoring counts do not match per-file data.', exit_code=1)
        for item in files:
            expected = max(1, item['human_loc']) if item['changed_human'] else 0
            if item['loc_weight'] != expected:
                raise BenchmarkError('invalid_report', 'Scoring LOC weights are inconsistent.', exit_code=1)
        mean = sum(item['mlp'] for item in files) / len(files) if files else 1.0
        weighted = (
            sum(item['mlp'] * item['loc_weight'] for item in files) / counts['loc_weight']
            if counts['loc_weight']
            else mean
        )
        if not math.isclose(mean, score['Mf_mlp'], abs_tol=1e-12) or not math.isclose(
            weighted, score['LCf_mlp'], abs_tol=1e-12
        ):
            raise BenchmarkError('invalid_report', 'Scoring aggregates do not match per-file results.', exit_code=1)
