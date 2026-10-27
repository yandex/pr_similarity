"""Golden MLP values and existence-aware scoring/aggregation contracts."""

import json
from pathlib import Path

import pytest

from benchmark.errors import BenchmarkError
from benchmark.metrics import codebleu, scoring
from benchmark.metrics.scoring import (
    diagnostic_metrics,
    metric_receipt,
    score_pr,
    score_triplet,
)
from benchmark.models.scoring import DiagnosticTask
from benchmark.models.triplets import TripletStates
from benchmark.vcs.workspace import FileState


def state(text: str | None) -> FileState:
    """Create an existing or absent file state from optional fixture text."""
    return FileState(text is not None, text or '', None if text is None else '100644')


@pytest.mark.parametrize(
    'base,human,ai,expected',
    [
        (None, '', None, 0),
        (None, None, '', 0),
        (None, '', '', 1),
        ('', None, '', 0),
        ('', None, None, 1),
        ('content', None, '', 0),
        ('content', '', 'different', 0),
        ('before', 'human', '', 0),
        ('before', 'same  \r\n', 'same\n', 1),
    ],
)
def test_missing_empty_and_normalized_shortcuts(
    base: str | None, human: str | None, ai: str | None, expected: float
) -> None:
    """Verify missing empty and normalized shortcuts."""
    result = score_triplet(state(base), state(human), state(ai), 'file.py')
    assert result.score == expected
    assert result.branch == 'code'


def test_extra_empty_file_affects_only_unweighted_quality() -> None:
    """Verify extra empty file affects only unweighted quality."""
    result = score_pr(
        {
            'changed.py': TripletStates(base=state('before'), human=state('human'), ai=state('human')),
            'extra.py': TripletStates(base=state(None), human=state(None), ai=state('')),
        }
    )
    assert result.Mf_mlp == 0.5
    assert result.LCf_mlp == 1
    assert result.counts is not None
    assert result.counts.extra_files == 1
    assert result.files[1].loc_weight == 0


def test_empty_human_change_has_zero_loc_but_unit_weight() -> None:
    """Verify empty human change has zero loc but unit weight."""
    result = score_pr({'empty.py': TripletStates(base=state(None), human=state(''), ai=state(None))})
    assert result.counts is not None
    assert result.counts.human_loc == 0
    assert result.counts is not None
    assert result.counts.loc_weight == 1
    assert result.counts is not None
    assert result.counts.missed_files == 1
    assert result.Mf_mlp == result.LCf_mlp == 0


def test_wrong_existence_operation_is_matched_for_f1() -> None:
    """Verify wrong existence operation is matched for f1."""
    result = score_pr({'file.py': TripletStates(base=state('before'), human=state(None), ai=state(''))})
    metrics = diagnostic_metrics([DiagnosticTask(processing_status='completed', scoring=result)])
    assert result.counts is not None
    assert result.counts.matched_files == 1
    assert metrics.global_F1 == 1
    assert metrics.Mpr_LCf_mlp == metrics.LCpr_LCf_mlp == 0


def test_unchanged_normalized_triplet_is_not_scored() -> None:
    """Verify unchanged normalized triplet is not scored."""
    result = score_pr({'file.txt': TripletStates(base=state('same\n'), human=state('same \n'), ai=state('same\r\n'))})
    assert result.counts is not None
    assert result.files == [] and result.counts.human_files == 0
    assert result.Mf_mlp == result.LCf_mlp == 1
    assert diagnostic_metrics([]).global_F1 is None


@pytest.mark.parametrize('failed_pair', [1, 2, 3], ids=['base_human', 'base_agent', 'human_agent'])
def test_one_codebleu_pair_failure_falls_back_whole_triplet(monkeypatch: pytest.MonkeyPatch, failed_pair: int) -> None:
    """Verify one codebleu pair failure falls back whole triplet."""
    calls = []

    def components(_a: str, _b: str, _lang: str) -> dict | None:
        """Return perfect CodeBLEU components except for the selected failing pair."""
        calls.append(1)
        return (
            None
            if len(calls) == failed_pair
            else {
                'ngram_match_score': 1,
                'weighted_ngram_match_score': 1,
                'syntax_match_score': 1,
                'dataflow_match_score': 1,
            }
        )

    monkeypatch.setattr(scoring, 'compute_codebleu_components', components)
    result = score_triplet(state('x = 1\n'), state('x = 2\n'), state('x = 3\n'), 'x.py')
    assert result.branch == 'default'
    assert len(calls) == failed_pair
    assert result.diagnostics[0].code == 'codebleu_fallback'


@pytest.mark.parametrize('extension,branch', [('.yaml', 'struct'), ('.json', 'struct'), ('.txt', 'default')])
def test_structured_and_unsupported_routing(extension: str, branch: str) -> None:
    """Verify structured and unsupported routing."""
    result = score_triplet(state('a: 1'), state('a: 2'), state('b: 3'), 'file' + extension)
    assert result.branch == branch
    assert 0 <= result.score <= 1


_GOLDEN = json.loads((Path(__file__).parent / 'fixtures/scoring/golden-code.json').read_text())


@pytest.mark.parametrize('fixture', _GOLDEN, ids=[row['language'] for row in _GOLDEN])
def test_fixed_source_golden_code_scores(fixture: dict) -> None:
    """Verify fixed source golden code scores."""
    result = score_triplet(
        state(fixture['base']), state(fixture['human']), state(fixture['ai']), 'file' + fixture['extension']
    )
    assert result.branch == fixture['expected']['branch'] == 'code'
    assert result.score == pytest.approx(fixture['expected']['score'], abs=1e-14)
    assert result.pair_scores == pytest.approx(fixture['expected']['pair_scores'], abs=1e-14)


def test_missing_metric_package_does_not_become_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify missing metric package does not become default."""

    def unavailable(_name: str) -> None:
        """Simulate an absent package distribution during metric preflight."""
        raise codebleu.metadata.PackageNotFoundError()

    monkeypatch.setattr(codebleu.metadata, 'version', unavailable)
    codebleu.require_dependencies.cache_clear()
    try:
        with pytest.raises(BenchmarkError) as caught:
            score_triplet(state('before'), state('human'), state('ai'), 'x.txt')
        assert caught.value.code == 'metric_unavailable'
    finally:
        codebleu.require_dependencies.cache_clear()


def test_metric_receipt_identifies_contract_without_source_hashes() -> None:
    """Source packaging changes do not alter the declared metric identity."""
    receipt = metric_receipt()
    assert receipt.to_wire() == {'name': 'mlp', 'version': 'mlp-existence-v1', 'pythonhashseed': 42}


def test_two_request_quality_metrics_use_final_completed_attempts() -> None:
    """Verify two request quality metrics use final completed attempts."""
    first = score_pr({'one.py': TripletStates(base=state('x'), human=state('y'), ai=state('y'))})
    second = score_pr({'two.py': TripletStates(base=state('x\nz'), human=state('y\na\nb'), ai=state('x\nz'))})
    unfinished = DiagnosticTask(processing_status='running', scoring=first)
    metrics = diagnostic_metrics(
        [
            DiagnosticTask(processing_status='completed', scoring=first),
            DiagnosticTask(processing_status='completed', scoring=second),
            unfinished,
        ]
    )
    assert metrics.scored_tasks == 2
    assert metrics.Mpr_LCf_mlp == 0.5
    assert first.counts is not None and second.counts is not None
    weight = first.counts.loc_weight + second.counts.loc_weight
    assert first.counts is not None
    assert metrics.LCpr_LCf_mlp == first.counts.loc_weight / weight
    assert metrics.global_F1 == 2 / 3


def test_valid_zero_codebleu_does_not_trigger_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat valid zero components as measurements rather than parser failures."""
    monkeypatch.setattr(
        scoring,
        'compute_codebleu_components',
        lambda *_args: dict.fromkeys(
            ('ngram_match_score', 'weighted_ngram_match_score', 'syntax_match_score', 'dataflow_match_score'), 0.0
        ),
    )
    result = score_triplet(state('x = 1'), state('x = 2'), state('x = 3'), 'x.py')
    assert result.branch == 'code' and result.diagnostics == []
