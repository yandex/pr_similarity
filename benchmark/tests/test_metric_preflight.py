"""Parser installation failures are discovered before paying for native generation."""

from pathlib import Path

import codebleu.utils
import pytest
from benchmark import coordinator, runner
from benchmark.errors import BenchmarkError
from benchmark.metrics import codebleu as metric
from benchmark.storage import read_json
from test_runner import config_and_basket


def test_broken_grammar_fails_before_native_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed metadata does not prove that a grammar can be imported and used."""
    config, basket = config_and_basket(tmp_path)
    original = codebleu.utils.get_tree_sitter_language
    provisioned = []

    def grammar(lang: str) -> object:
        """Expose an installed grammar import failure to whole-run preflight."""
        if lang == 'python':
            raise ImportError('fixture grammar cannot be loaded')
        return original(lang)

    monkeypatch.setattr(codebleu.utils, 'get_tree_sitter_language', grammar)
    monkeypatch.setattr(coordinator, 'provision_opencode', lambda *_args, **_kwargs: provisioned.append(True))
    metric.require_dependencies.cache_clear()
    try:
        code, directory = runner.run(config, basket, output=tmp_path / 'runs')
        assert code == 1
        assert read_json(directory / 'report.json')['run']['error']['code'] == 'metric_unavailable'
        assert provisioned == []
    finally:
        metric.require_dependencies.cache_clear()


def test_preflight_checks_every_supported_grammar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The package profile is a whole-run prerequisite, including unused languages."""
    original = codebleu.utils.get_tree_sitter_language
    seen = []

    def grammar(lang: str) -> object:
        """Record every grammar requested by the frozen metric profile."""
        seen.append(lang)
        return original(lang)

    monkeypatch.setattr(codebleu.utils, 'get_tree_sitter_language', grammar)
    metric.require_dependencies.cache_clear()
    try:
        metric.require_dependencies()
        assert set(seen) == set(metric.EXT_TO_LANG.values())
    finally:
        metric.require_dependencies.cache_clear()


@pytest.mark.parametrize('error', [ImportError, RuntimeError, TypeError, AssertionError, ValueError])
def test_environment_or_api_failure_during_compute_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """A late installation/API failure is not a content-specific metric fallback."""
    metric.require_dependencies()

    def fail(**_kwargs: object) -> dict:
        """Raise the selected API failure after dependency preflight succeeds."""
        raise error('fixture failure after preflight')

    monkeypatch.setattr(metric, 'calc_codebleu', fail)
    metric.compute_codebleu_components.cache_clear()
    try:
        with pytest.raises(BenchmarkError):
            metric.compute_codebleu_components('x = 1', 'x = 2', 'python')
    finally:
        metric.compute_codebleu_components.cache_clear()


def test_real_invalid_syntax_keeps_triplet_fallback_contract() -> None:
    """Tree-sitter reports parse errors as nodes, without raising an exception."""
    metric.compute_codebleu_components.cache_clear()
    try:
        assert metric.compute_codebleu_components('def broken(:\n', 'x = 2', 'python') is None
    finally:
        metric.compute_codebleu_components.cache_clear()
