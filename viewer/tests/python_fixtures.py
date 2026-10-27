"""Independent consumer fixtures for the v0.3.0 Python provenance contract."""

from viewer.util import digest, json_bytes


def python_document() -> dict:
    """Describe a synthetic measured base without importing benchmark models."""
    entry = {'path': 'bin/python3.12', 'sha256': 'a' * 64, 'size_bytes': 1}
    signature = digest(f'{entry['path']}\0{entry['sha256']}\0{entry['size_bytes']}\n'.encode())
    return {
        'version': '3.12.0',
        'implementation': 'cpython',
        'platform': 'fixture',
        'origin': 'benchmark_venv',
        'content_sha256': signature,
        'files': [entry],
        'preflight': 'passed',
    }


def python_reference() -> dict:
    """Bind the standalone synthetic runtime document to its archive location."""
    content = json_bytes(python_document())
    return {
        'path': '_meta/python.json',
        'sha256': digest(content),
        'size_bytes': len(content),
    }


def python_attempt(runtime: dict) -> dict:
    """Describe both successful probe stages of a controlled consumer fixture."""
    probe = {
        'version': '3.12.0',
        'executable': '/sandbox-home/.venv/bin/python',
        'prefix': '/sandbox-home/.venv',
        'base_prefix': '/benchmark-python',
    }
    return {'runtime': runtime, 'before_setup': probe, 'before_agent': probe.copy()}
