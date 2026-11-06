"""Standard Python entrypoints and probes inside a private source sandbox."""

from benchmark.execution.python_runtime import PYTHON_ROOT, VENV_ROOT, PythonRuntime


def python_mounts(runtime: PythonRuntime) -> list[str]:
    """Mount private bin projections without one bind operation per host file."""
    return ['--ro-bind', str(runtime.root), PYTHON_ROOT, *runtime.bins.mounts(VENV_ROOT)]


PYTHON_PROBE = '''
"""Verify required standard modules and the private Python installation."""

import json
import site
import sys
import sysconfig
from importlib import import_module


def require(condition: bool, message: str) -> None:
    """Reject a sandbox Python that violates the frozen isolation contract."""
    if not condition:
        raise RuntimeError(message)


for name in ('bz2', 'lzma', 'sqlite3', 'ssl', 'zlib'):
    import_module(name)
require(sys.version_info[:2] == (3, 12), 'Python 3.12 required')
require(sys.prefix == '/sandbox-home/.venv', 'Attempt prefix changed')
require(sys.base_prefix == '/benchmark-python', 'Base prefix changed')
require(sys.executable == '/sandbox-home/.venv/bin/python', 'Attempt executable changed')
require(not site.ENABLE_USER_SITE, 'User packages enabled')
require(
    all(
        not ('site-packages' in path or 'dist-packages' in path) or path.startswith('/sandbox-home/.venv/')
        for path in sys.path
    ),
    'Foreign packages visible',
)
# Import pip only after checking that the interpreter cannot see host packages.
pip = import_module('pip')
require(pip.__file__ is not None and pip.__file__.startswith('/sandbox-home/.venv/'), 'Foreign pip visible')
require(sysconfig.get_path('purelib').startswith('/sandbox-home/.venv/'), 'Foreign installation target')
print(
    json.dumps(
        dict(version=sys.version.split()[0], executable=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix)
    )
)
'''
