"""Pinned parser profile and bounded component cache for the fixed MLP."""

import json
import logging
import math
import threading
from functools import cache, lru_cache
from importlib import import_module, metadata
from importlib.resources import files
from types import MappingProxyType, ModuleType

from benchmark.errors import BenchmarkError


def _dependency_module(name: str, symbol: str) -> ModuleType:
    """Preserve import-error classification while loading optional dependency APIs."""
    module = import_module(name)
    if not hasattr(module, symbol):
        raise ImportError(f'Cannot import {symbol} from {name}')
    return module


try:
    calc_codebleu = _dependency_module('codebleu', 'calc_codebleu').calc_codebleu
except ImportError:
    calc_codebleu = None

CODEBLEU_COMPONENTS = ('ngram_match_score', 'weighted_ngram_match_score', 'syntax_match_score', 'dataflow_match_score')
EXT_TO_LANG = MappingProxyType(
    {
        '.py': 'python',
        '.pyi': 'python',
        '.pyw': 'python',
        '.js': 'javascript',
        '.jsx': 'javascript',
        '.mjs': 'javascript',
        '.cjs': 'javascript',
        '.jsm': 'javascript',
        '.ts': 'javascript',
        '.tsx': 'javascript',
        '.mts': 'javascript',
        '.cts': 'javascript',
        '.java': 'java',
        '.go': 'go',
        '.c': 'c',
        '.h': 'c',
        '.cpp': 'cpp',
        '.cc': 'cpp',
        '.cxx': 'cpp',
        '.c++': 'cpp',
        '.cp': 'cpp',
        '.C': 'cpp',
        '.CPP': 'cpp',
        '.hpp': 'cpp',
        '.hh': 'cpp',
        '.hxx': 'cpp',
        '.h++': 'cpp',
        '.H': 'cpp',
        '.ipp': 'cpp',
        '.tpp': 'cpp',
        '.tcc': 'cpp',
        '.inl': 'cpp',
        '.cs': 'c_sharp',
        '.php': 'php',
        '.php3': 'php',
        '.php4': 'php',
        '.php5': 'php',
        '.phtml': 'php',
        '.rb': 'ruby',
        '.rbw': 'ruby',
        '.rake': 'ruby',
        '.gemspec': 'ruby',
        '.podspec': 'ruby',
        '.ru': 'ruby',
        '.thor': 'ruby',
        '.jbuilder': 'ruby',
        '.rs': 'rust',
    }
)
PROFILE = MappingProxyType(
    {
        'codebleu': '0.7.1',
        'tree-sitter': '0.23.2',
        'tree-sitter-python': '0.23.6',
        'tree-sitter-java': '0.23.5',
        'tree-sitter-javascript': '0.23.1',
        'tree-sitter-c-sharp': '0.23.0',
        'tree-sitter-php': '0.23.11',
        'tree-sitter-c': '0.23.4',
        'tree-sitter-cpp': '0.23.4',
        'tree-sitter-go': '0.23.4',
        'tree-sitter-ruby': '0.23.1',
        'tree-sitter-rust': '0.23.2',
    }
)
SOURCE_COMMIT = 'b0edb622f6a52fe9d1edc407be5061d3e1462a7f'
SOURCE_ARCHIVE_SHA256 = '22a4e902d3f20a49586c63a88d5a0443f28fe2e80d23c6d7152f05dc724909a6'


@cache
def _call_lock() -> threading.Lock:
    """Serialize non-thread-safe parser calls within this scoring process."""
    return threading.Lock()


@cache
def require_dependencies() -> None:
    """Missing or mismatched packages are infrastructure failures, never fallback."""
    try:
        for name, expected in PROFILE.items():
            if metadata.version(name) != expected:
                raise BenchmarkError(
                    'metric_unavailable', 'Metric dependency versions differ; run make setup.', exit_code=1
                )
        receipt = metadata.distribution('codebleu').read_text('direct_url.json')
        if receipt is None:
            raise BenchmarkError(
                'metric_unavailable', 'CodeBLEU source receipt is missing; run make setup.', exit_code=1
            )
        source = json.loads(receipt)
        if source.get('archive_info', {}).get('hashes', {}).get('sha256') != SOURCE_ARCHIVE_SHA256:
            raise BenchmarkError(
                'metric_unavailable', 'CodeBLEU source differs from the pinned archive; run make setup.', exit_code=1
            )

        if not callable(calc_codebleu):
            raise TypeError('CodeBLEU API cannot be loaded')
        get_tree_sitter_language = _dependency_module(
            'codebleu.utils', 'get_tree_sitter_language'
        ).get_tree_sitter_language
        parser_type = _dependency_module('tree_sitter', 'Parser').Parser

        for lang in sorted(set(EXT_TO_LANG.values())):
            if not files('codebleu').joinpath('keywords', lang + '.txt').read_text(encoding='utf-8').strip():
                raise ValueError('Grammar keywords are missing')
            parser = parser_type(get_tree_sitter_language(lang))
            if parser.parse(b'').root_node is None:
                raise ValueError('Grammar has no root node')
    except (ImportError, ValueError, TypeError, OSError, RuntimeError, AttributeError, AssertionError) as exc:
        raise BenchmarkError(
            'metric_unavailable', 'Metric dependencies are missing; run make setup.', exit_code=1
        ) from exc


@lru_cache(maxsize=4096)
def compute_codebleu_components(reference: str, candidate: str, lang: str) -> dict[str, float] | None:
    """Return None on a failed pair so the whole triplet switches to default."""
    require_dependencies()

    try:
        assert callable(calc_codebleu)
        with _call_lock():
            get_tree_sitter_language = _dependency_module(
                'codebleu.utils', 'get_tree_sitter_language'
            ).get_tree_sitter_language
            parser_type = _dependency_module('tree_sitter', 'Parser').Parser

            parser = parser_type(get_tree_sitter_language(lang))
            if any(parser.parse(text.encode('utf-8')).root_node.has_error for text in (reference, candidate)):
                logging.getLogger(__name__).debug('CodeBLEU content could not be parsed for %s', lang)
                return None
            result = calc_codebleu(
                references=[[reference]],
                predictions=[candidate],
                lang=lang,
                weights=(0.25, 0.25, 0.25, 0.25),
                tokenizer=None,
            )
        if not isinstance(result, dict):
            raise TypeError('CodeBLEU must return a component mapping.')
        components = {key: float(result[key]) for key in CODEBLEU_COMPONENTS}
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in components.values()):
            raise ValueError('invalid CodeBLEU component')
        return components
    except (ImportError, OSError) as exc:
        raise BenchmarkError(
            'metric_unavailable', 'A metric dependency cannot be loaded; run make setup.', exit_code=1
        ) from exc
    except (KeyError, TypeError, RuntimeError, AttributeError, AssertionError, ValueError) as exc:
        raise BenchmarkError('metric_failed', 'CodeBLEU API failed; check the scoring DEBUG log.', exit_code=1) from exc
