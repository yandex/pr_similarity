"""Standalone HTML stays offline, escapes untrusted task data and shows unknown usage."""

from pathlib import Path

import pytest
import yaml
from benchmark.config import load_config
from benchmark.errors import BenchmarkError
from benchmark.runner import run
from benchmark.storage import read_json, write_json
from benchmark.visualize import compare, visualize
from test_runner import config_and_basket, fake_attempt, provision_fixture


def sample(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Generate an actual registry/state run with frozen unsafe title and unknown usage."""
    source = tmp_path / name
    source.mkdir()
    _config, dataset = config_and_basket(source)
    config_path = source / 'config.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['agent']['name'] = 'fixture-agent' if name == 'one' else 'second-agent'
    config_path.write_text(yaml.safe_dump(config))
    basket = read_json(dataset)
    basket['tasks'][0]['change_request']['title'] = '</script><script>alert("untrusted")</script>'
    if name == 'two':
        basket['meta']['name'] = 'Different basket'
    write_json(dataset, basket)
    provision_fixture(monkeypatch)
    status, directory = run(
        load_config(config_path), dataset, output=source / 'runs', attempt_function=fake_attempt(['success'])
    )
    assert status == 0
    return directory


def test_visualize_is_standalone_safe_and_preserves_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify visualize is standalone safe and preserves report."""
    run = sample(tmp_path, 'one', monkeypatch)
    before = (run / 'report.json').read_bytes()
    monkeypatch.setattr('webbrowser.open', lambda url: (_ for _ in ()).throw(AssertionError('browser forbidden')))
    path = visualize(run, no_browser=True)
    document = path.read_text()
    assert '&lt;/script&gt;&lt;script&gt;' in document
    assert '<script>alert(' not in document
    assert 'cache read / cache write' in document and '—' in document
    assert '<script src=' not in document and '<link ' not in document
    assert 'Filter tasks' in document
    assert (run / 'report.json').read_bytes() == before


def test_comparison_preserves_order_and_uses_distinct_output_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify comparison preserves order and uses distinct output paths."""
    first, second = sample(tmp_path, 'one', monkeypatch), sample(tmp_path, 'two', monkeypatch)
    output = compare([second, first], no_browser=True)
    document = output.read_text()
    assert document.index('second-agent') < document.index('fixture-agent')
    assert 'Different baskets' in document
    assert compare([second, first], no_browser=True) != output
    with pytest.raises(BenchmarkError):
        compare([first], no_browser=True)
