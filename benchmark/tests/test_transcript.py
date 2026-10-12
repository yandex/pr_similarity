"""Native evidence and readable transcripts have independent failure domains."""

import json
from pathlib import Path

import pytest
from benchmark.opencode import EventTracker
from benchmark.process import ProcessResult
from benchmark.transcript import Transcript


def test_all_json_objects_and_runtime_streams_are_separate(tmp_path: Path) -> None:
    """Unknown event types remain evidence, and diagnostics retain their source."""
    tracker = EventTracker()
    transcript = Transcript(tmp_path, tracker, task_label='Task 003/100', attempt_sequence=2, model='fixture/model')
    transcript.accept('stdout', '{"type":"future_event","payload":"unknown"}')
    transcript.accept('stdout', '{"type":"text","part":{"text":"Final answer\\nsecond line"}}')
    transcript.accept('stderr', 'runtime diagnostic')
    transcript.accept('stdout', 'not JSON')
    receipt = transcript.finish(ProcessResult('completed', 0, 1))
    assert len((tmp_path / 'events.jsonl').read_text().splitlines()) == 2
    assert 'future_event' not in (tmp_path / 'agent.log').read_text()
    assert 'Final answer' in (tmp_path / 'agent.log').read_text()
    runtime = (tmp_path / 'runtime.log').read_text()
    assert '[stderr]' in runtime and '[stdout]' in runtime
    assert receipt['events_complete'] and receipt['runtime_complete']


def test_formatter_failure_cannot_hide_later_provider_error(tmp_path: Path) -> None:
    """A presentation exception does not switch off native error classification."""
    tracker = EventTracker()
    transcript = Transcript(tmp_path, tracker)

    def broken(event: dict, raw_line: int | None) -> None:
        """Inject a formatter defect after classification and raw storage."""
        raise ValueError('fixture formatter failure')

    transcript.render_event = broken
    transcript.accept('stdout', '{"type":"text","sessionID":"root","part":{"text":"hello"}}')
    transcript.accept(
        'stdout', '{"type":"error","sessionID":"root","error":{"name":"APIError","data":{"statusCode":401}}}'
    )
    receipt = transcript.finish(ProcessResult('completed', 0, 1))
    assert tracker.errors[0]['data']['statusCode'] == 401
    assert len((tmp_path / 'events.jsonl').read_text().splitlines()) == 2
    assert receipt['events_complete'] and not receipt['transcript_complete']


def test_tool_output_truncation_links_exact_raw_line(tmp_path: Path) -> None:
    """Readable tail keeps action targets and points to the unabridged native event."""
    transcript = Transcript(tmp_path, EventTracker(), output_limit=32)
    event = {
        'type': 'tool_use',
        'part': {
            'tool': 'read',
            'state': {'status': 'completed', 'input': {'filePath': 'src/main.py'}, 'output': 'x' * 100},
        },
    }
    transcript.accept('stdout', json.dumps(event))
    transcript.finish(ProcessResult('completed', 0, 1))
    human = (tmp_path / 'agent.log').read_text()
    assert 'read' in human and 'src/main.py' in human and 'completed' in human
    assert 'truncated' in human and 'events.jsonl:1' in human
    assert 'x' * 100 in (tmp_path / 'events.jsonl').read_text()


def test_raw_sink_failure_preserves_provider_classification(tmp_path: Path) -> None:
    """Capture failure is explicit, while later native errors still stop unsafe retries."""
    tracker = EventTracker()
    transcript = Transcript(tmp_path, tracker)
    (tmp_path / 'events.jsonl').unlink()
    (tmp_path / 'events.jsonl').mkdir()
    transcript.accept('stdout', '{"type":"text","sessionID":"root","part":{"text":"hello"}}')
    transcript.accept(
        'stdout', '{"type":"error","sessionID":"root","error":{"name":"APIError","data":{"statusCode":503}}}'
    )
    receipt = transcript.finish(ProcessResult('completed', 0, 1))
    assert tracker.errors[0]['data']['statusCode'] == 503
    assert not receipt['events_complete'] and 'event_capture_failed' in receipt['warnings']


def test_json_escaped_secrets_and_controls_are_safe_in_every_sink(tmp_path: Path) -> None:
    """JSON escaping cannot bypass known-secret redaction in nested event values."""
    secret = 'secret"value'
    transcript = Transcript(tmp_path, EventTracker(), model=secret, secrets=[secret])
    transcript.accept('stdout', json.dumps({'type': 'text', 'part': {'text': secret + '\x1b[31m final'}}))
    transcript.accept('stderr', secret + '\x1b[31m')
    transcript.finish(ProcessResult('completed', 0, 1))
    for name in ('agent.log', 'runtime.log', 'events.jsonl', 'stream_receipt.json'):
        content = (tmp_path / name).read_text()
        assert secret not in content
        assert json.dumps(secret)[1:-1] not in content
    assert '\x1b' not in (tmp_path / 'agent.log').read_text()


def test_nonfinite_diagnostic_is_runtime_and_streams_cannot_reopen(tmp_path: Path) -> None:
    """Invalid native JSON stays diagnostic and sealing rejects later writes."""
    transcript = Transcript(tmp_path, EventTracker())
    transcript.accept('stdout', '{"type":"future","value":NaN}')
    transcript.finish(ProcessResult('completed', 0, 1))
    assert (tmp_path / 'events.jsonl').read_text() == ''
    assert 'NaN' in (tmp_path / 'runtime.log').read_text()
    with pytest.raises(ValueError, match='sealed'):
        transcript.accept('stdout', '{"type":"text"}')


def test_receipt_failure_still_returns_classified_native_error(tmp_path: Path) -> None:
    """A failed required sidecar cannot conceal the already-observed provider outcome."""
    tracker = EventTracker()
    transcript = Transcript(tmp_path, tracker)
    (tmp_path / 'stream_receipt.json').mkdir()
    transcript.accept('stdout', '{"type":"error","error":{"name":"APIError"}}')
    receipt = transcript.finish(ProcessResult('error', 0, 1))
    assert len(tracker.errors) == 1
    assert 'stream_receipt_failed' in receipt['warnings']
    assert not receipt['events_complete'] and not receipt['runtime_complete']
