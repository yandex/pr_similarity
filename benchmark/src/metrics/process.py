"""Bounded cancellation of CPU/native parser work via an owned child process."""

import os
import sys
import threading
from pathlib import Path

from benchmark.errors import BenchmarkError
from benchmark.execution.process import run_process
from benchmark.metrics.scoring import metric_receipt
from benchmark.models.scoring import ScoreResult
from benchmark.persistence.storage import parse_json


def score_saved(
    directory: Path,
    prefix: str,
    log: Path,
    stop: threading.Event,
    *,
    grace: float = 10,
    secrets: list[str] | None = None,
    argv: list[str] | None = None,
) -> ScoreResult:
    """Score immutable triplets in an owned cancellable child process.

    Args:
        directory: Complete triplet directory with validated state receipts.
        prefix: Run-relative path used in the resulting report artifact references.
        stop: Cancellation intent shared with the task scheduler.
        log: Private output file that receives child diagnostics.
        grace: TERM deadline before forced process-group termination.
        secrets: Known in-memory values to redact from captured diagnostics.
        argv: Optional child command for bounded process-lifecycle fixtures.

    Returns:
        Fixed MLP scores, implementation receipt and per-file fallback diagnostics.

    Raises:
        BenchmarkError: If cancellation, child failure or a metric identity change occurs.

    """
    if stop.is_set():
        raise BenchmarkError(
            'operation_cancelled', 'Scoring was interrupted; resume can use the saved triplets.', exit_code=130
        )
    lines = []
    result = run_process(
        argv or [sys.executable, '-m', 'benchmark.metrics.worker', '--triplets', str(directory), '--prefix', prefix],
        env={'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONHASHSEED': '42'},
        cwd=directory,
        timeout=float('inf'),
        grace=grace,
        cancel=stop,
        log=log,
        secrets=secrets,
        on_stdout=lines.append,
        log_stdout=False,
    )
    if result.status != 'completed' and lines:
        with log.open('a', encoding='utf-8') as output:
            output.write('\n[failed scoring stdout]\n' + '\n'.join(lines) + '\n')
    if result.status == 'cancelled':
        raise BenchmarkError(
            'operation_cancelled', 'Scoring was interrupted; resume can use the saved triplets.', exit_code=130
        )
    if result.status != 'completed':
        raise BenchmarkError('scoring_failed', 'Scoring process failed; check its DEBUG log.', exit_code=1)
    try:
        value = ScoreResult.model_validate(parse_json('\n'.join(lines).encode()))
    except BenchmarkError:
        with log.open('a', encoding='utf-8') as output:
            output.write('\n[invalid scoring stdout]\n' + '\n'.join(lines) + '\n')
        raise
    if value.metric != metric_receipt():
        raise BenchmarkError(
            'metric_changed', 'Scoring implementation changed during the attempt; use rescore.', exit_code=1
        )
    return value
