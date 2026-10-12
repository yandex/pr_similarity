"""Deterministic string features for the learned `mlp` metric."""

import collections
import difflib
from collections.abc import Sequence
from dataclasses import dataclass

from .text import normalize, tokenize


@dataclass(frozen=True)
class _PairComparison:
    """Reusable order-aware comparison for one pair and one granularity."""

    precision: float
    recall: float
    opcodes: Sequence[tuple[str, int, int, int, int]]
    units_a: list[str]
    units_b: list[str]


def _units(text: str, granularity: str) -> list[str]:
    """Split text into ordered units of the given granularity."""
    if granularity == 'tokens':
        return tokenize(text)
    if granularity == 'lines':
        return normalize(text).splitlines()
    raise ValueError(f'unknown granularity: {granularity!r}')


def _empty_precision_recall(seq_a: list[str], seq_b: list[str]) -> tuple[float, float] | None:
    """Return the trivial precision and recall value for empty inputs."""
    if not seq_a and not seq_b:
        return 1.0, 1.0
    if not seq_a or not seq_b:
        return 0.0, 0.0
    return None


def _pair_comparison(seq_a: list[str], seq_b: list[str]) -> _PairComparison:
    """Return a reusable autojunk SequenceMatcher comparison for one pair."""
    trivial = _empty_precision_recall(seq_a, seq_b)
    if trivial is not None:
        precision, recall = trivial
        return _PairComparison(precision, recall, [], seq_a, seq_b)
    if seq_a == seq_b:
        return _PairComparison(1.0, 1.0, [], seq_a, seq_b)

    matcher = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=True)
    match = sum(block.size for block in matcher.get_matching_blocks())
    return _PairComparison(
        precision=match / len(seq_b),
        recall=match / len(seq_a),
        opcodes=matcher.get_opcodes(),
        units_a=seq_a,
        units_b=seq_b,
    )


def _multiset_precision_recall(seq_a: list[str], seq_b: list[str]) -> tuple[float, float]:
    """Return order-insensitive multiset precision and recall over two sequences."""
    if not seq_a and not seq_b:
        return 1.0, 1.0
    if not seq_a or not seq_b:
        return 0.0, 0.0
    if seq_a == seq_b:
        return 1.0, 1.0
    counter_a = collections.Counter(seq_a)
    counter_b = collections.Counter(seq_b)
    intersection = sum((counter_a & counter_b).values())
    return intersection / len(seq_b), intersection / len(seq_a)


def _signed_delta(comparison: _PairComparison) -> tuple[list[str], list[str]]:
    """Return ordered deleted and added unit streams from a base-to-new comparison."""
    deleted: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in comparison.opcodes:
        if tag in ('replace', 'delete'):
            deleted.extend(comparison.units_a[i1:i2])
        if tag in ('replace', 'insert'):
            added.extend(comparison.units_b[j1:j2])
    return deleted, added


def _ordered_match_count(seq_a: list[str], seq_b: list[str]) -> int:
    """Return autojunk LCS-style ordered match count for two sequences."""
    if not seq_a or not seq_b:
        return 0
    if seq_a == seq_b:
        return len(seq_a)
    matcher = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=True)
    return sum(block.size for block in matcher.get_matching_blocks())


def _multiset_match_count(seq_a: list[str], seq_b: list[str]) -> int:
    """Return order-insensitive multiset intersection size."""
    if not seq_a or not seq_b:
        return 0
    if seq_a == seq_b:
        return len(seq_a)
    counter_a = collections.Counter(seq_a)
    counter_b = collections.Counter(seq_b)
    return sum((counter_a & counter_b).values())


def _signed_diff_precision_recall(
    human_deleted: list[str],
    human_added: list[str],
    ai_deleted: list[str],
    ai_added: list[str],
) -> tuple[float, float]:
    """Return order-aware signed diff precision and recall from add/delete streams."""
    human_total = len(human_deleted) + len(human_added)
    ai_total = len(ai_deleted) + len(ai_added)
    if human_total == 0 and ai_total == 0:
        return 1.0, 1.0
    if human_total == 0 or ai_total == 0:
        return 0.0, 0.0

    match = _ordered_match_count(human_deleted, ai_deleted) + _ordered_match_count(human_added, ai_added)
    return match / ai_total, match / human_total


def _signed_diff_multiset_precision_recall(
    human_deleted: list[str],
    human_added: list[str],
    ai_deleted: list[str],
    ai_added: list[str],
) -> tuple[float, float]:
    """Return order-insensitive signed diff precision and recall from add/delete streams."""
    human_total = len(human_deleted) + len(human_added)
    ai_total = len(ai_deleted) + len(ai_added)
    if human_total == 0 and ai_total == 0:
        return 1.0, 1.0
    if human_total == 0 or ai_total == 0:
        return 0.0, 0.0

    match = _multiset_match_count(human_deleted, ai_deleted) + _multiset_match_count(human_added, ai_added)
    return match / ai_total, match / human_total


def pair_features(text_a: str, text_b: str) -> dict[str, float]:
    """Return the six string features of one text pair in layer-1 order."""
    token_comparison = _pair_comparison(_units(text_a, 'tokens'), _units(text_b, 'tokens'))
    lines_comparison = _pair_comparison(_units(text_a, 'lines'), _units(text_b, 'lines'))
    mtoken_precision, mtoken_recall = _multiset_precision_recall(token_comparison.units_a, token_comparison.units_b)
    return {
        'token_precision': token_comparison.precision,
        'token_recall': token_comparison.recall,
        'lines_precision': lines_comparison.precision,
        'lines_recall': lines_comparison.recall,
        'mtoken_precision': mtoken_precision,
        'mtoken_recall': mtoken_recall,
    }


def triplet_features(
    base_text: str,
    human_text: str,
    ai_text: str,
) -> tuple[dict[str, dict[str, float]], tuple[float, float, float, float, float, float]]:
    """Return cached string pair features and diff features for a file triplet."""
    units_cache: dict[tuple[str, str], list[str]] = {}
    comparison_cache: dict[tuple[str, str, str], _PairComparison] = {}

    def cached_units(text: str, granularity: str) -> list[str]:
        """Reuse normalized units within this triplet feature calculation."""
        key = (text, granularity)
        if key not in units_cache:
            units_cache[key] = _units(text, granularity)
        return units_cache[key]

    def cached_comparison(text_a: str, text_b: str, granularity: str) -> _PairComparison:
        """Reuse an order-aware comparison of the selected unit pair."""
        key = (text_a, text_b, granularity)
        if key not in comparison_cache:
            comparison_cache[key] = _pair_comparison(
                cached_units(text_a, granularity),
                cached_units(text_b, granularity),
            )
        return comparison_cache[key]

    def pair_features_from_comparisons(
        token_comparison: _PairComparison,
        line_comparison: _PairComparison,
    ) -> dict[str, float]:
        """Extract the fixed pair feature vector from cached comparisons."""
        mtoken_precision, mtoken_recall = _multiset_precision_recall(
            token_comparison.units_a,
            token_comparison.units_b,
        )
        return {
            'token_precision': token_comparison.precision,
            'token_recall': token_comparison.recall,
            'lines_precision': line_comparison.precision,
            'lines_recall': line_comparison.recall,
            'mtoken_precision': mtoken_precision,
            'mtoken_recall': mtoken_recall,
        }

    token_base_human = cached_comparison(base_text, human_text, 'tokens')
    token_base_ai = cached_comparison(base_text, ai_text, 'tokens')
    token_human_ai = cached_comparison(human_text, ai_text, 'tokens')
    lines_base_human = cached_comparison(base_text, human_text, 'lines')
    lines_base_ai = cached_comparison(base_text, ai_text, 'lines')
    lines_human_ai = cached_comparison(human_text, ai_text, 'lines')

    token_human_deleted, token_human_added = _signed_delta(token_base_human)
    token_ai_deleted, token_ai_added = _signed_delta(token_base_ai)
    lines_human_deleted, lines_human_added = _signed_delta(lines_base_human)
    lines_ai_deleted, lines_ai_added = _signed_delta(lines_base_ai)

    pair_features_by_pair = {
        'base_human': pair_features_from_comparisons(token_base_human, lines_base_human),
        'base_ai': pair_features_from_comparisons(token_base_ai, lines_base_ai),
        'human_ai': pair_features_from_comparisons(token_human_ai, lines_human_ai),
    }
    diff_token_precision, diff_token_recall = _signed_diff_precision_recall(
        token_human_deleted,
        token_human_added,
        token_ai_deleted,
        token_ai_added,
    )
    diff_lines_precision, diff_lines_recall = _signed_diff_precision_recall(
        lines_human_deleted,
        lines_human_added,
        lines_ai_deleted,
        lines_ai_added,
    )
    diff_mtoken_precision, diff_mtoken_recall = _signed_diff_multiset_precision_recall(
        token_human_deleted,
        token_human_added,
        token_ai_deleted,
        token_ai_added,
    )

    return (
        pair_features_by_pair,
        (
            diff_token_precision,
            diff_token_recall,
            diff_lines_precision,
            diff_lines_recall,
            diff_mtoken_precision,
            diff_mtoken_recall,
        ),
    )
