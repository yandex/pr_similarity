"""Background failures retain safe diagnostic origins and permit explicit retry."""

import logging
import re
from collections.abc import Callable

import pytest
from viewer.maintenance import Maintenance


def _wait_status(worker: Maintenance, status: Callable[[], dict], expected: str) -> dict:
    """Wait for observable job completion without racing its worker notification."""
    with worker.condition:
        assert worker.condition.wait_for(lambda: status()['status'] == expected, 5)
        return status()


@pytest.mark.parametrize('operation', ['recovery', 'refresh'])
def test_unexpected_failure_logs_origin_without_secret_and_accepts_retry(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Retain type and call origins while excluding exception text from logs and status."""
    calls = 0
    secret = 'synthetic-private-credential'

    def fail_at_origin() -> None:
        """Raise an unexpected error whose message must never enter a public diagnostic."""
        raise RuntimeError(secret)

    def perform() -> bool:
        """Fail once, then acknowledge the next explicitly requested job."""
        nonlocal calls
        calls += 1
        if calls == 1:
            fail_at_origin()
        return True

    def recover() -> None:
        """Invoke the same operation through the recovery callback contract."""
        perform()

    worker = Maintenance(recover, retry_seconds=0, attempts=2, refresh=perform)
    status = worker.status if operation == 'recovery' else worker.refresh_status
    request = worker.request_recovery if operation == 'recovery' else worker.request_refresh
    caplog.set_level(logging.WARNING, logger='viewer.maintenance')
    worker.start()
    try:
        request()
        failed = _wait_status(worker, status, 'failed')
        assert calls == 1
        assert failed['error_code'] == operation + '_failed'
        assert secret not in str(failed)
        assert 'RuntimeError' not in str(failed)
        request()
        succeeded = _wait_status(worker, status, 'succeeded')
        assert calls == 2
        assert succeeded['error_code'] is None
    finally:
        assert worker.close(timeout=5)
    assert 'RuntimeError' in caplog.text
    assert 'test_maintenance_diagnostics.py' in caplog.text
    assert 'perform' in caplog.text
    assert re.search(r'test_maintenance_diagnostics\.py:\d+:fail_at_origin', caplog.text)
    assert secret not in caplog.text
    assert 'raise RuntimeError(secret)' not in caplog.text
    records = [record for record in caplog.records if record.name == 'viewer.maintenance']
    assert records and all(record.exc_info is None and record.stack_info is None for record in records)
