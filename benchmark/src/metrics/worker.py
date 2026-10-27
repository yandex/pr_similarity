"""Private scoring process: no credentials, agent context or executable run inputs."""

import argparse
import logging
import sys
from pathlib import Path

from benchmark.errors import OPERATION_ERRORS
from benchmark.metrics.scoring import metric_receipt, score_pr
from benchmark.models.scoring import Diagnostic, ScoreResult
from benchmark.persistence.storage import canonical_bytes
from benchmark.persistence.triplets import read_triplets


def main() -> int:
    """Calculate fixed scores and return one validated JSON result."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--triplets', type=Path, required=True)
    parser.add_argument('--prefix', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, force=True)
    try:
        diagnostics: list[Diagnostic] = []
        bundle = read_triplets(args.triplets)
        score = score_pr(
            bundle.states, artifact_prefix=args.prefix, diagnostics=diagnostics, artifact_refs=bundle.references
        )
        print(
            canonical_bytes(ScoreResult(scoring=score, metric=metric_receipt(), diagnostics=diagnostics)).decode(),
            flush=True,
        )
        return 0
    except OPERATION_ERRORS:
        # The parent captures stderr in its DEBUG file, never the user's console.
        logging.getLogger(__name__).debug('Scoring worker failed', exc_info=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
