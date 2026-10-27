"""Standard Python entrypoints and probes inside a private source sandbox."""

from benchmark.execution.python_runtime import PYTHON_ROOT, VENV_ROOT, PythonRuntime


def python_mounts(runtime: PythonRuntime) -> list[str]:
    """Mount private bin projections without one bind operation per host file."""
    return ['--ro-bind', str(runtime.root), PYTHON_ROOT, *runtime.bins.mounts(VENV_ROOT)]


PYTHON_PROBE = '''import bz2, json, lzma, site, sqlite3, ssl, sys, sysconfig, zlib

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

require(sys.version_info[:2] == (3, 12), 'Python 3.12 required')
require(sys.prefix == '/sandbox-home/.venv', 'Attempt prefix changed')
require(sys.base_prefix == '/benchmark-python', 'Base prefix changed')
require(sys.executable == '/sandbox-home/.venv/bin/python', 'Attempt executable changed')
require(not site.ENABLE_USER_SITE, 'User packages enabled')
require(all(not ('site-packages' in p or 'dist-packages' in p) or
            p.startswith('/sandbox-home/.venv/') for p in sys.path), 'Foreign packages visible')
import pip
require(pip.__file__.startswith('/sandbox-home/.venv/'), 'Foreign pip visible')
require(sysconfig.get_path('purelib').startswith('/sandbox-home/.venv/'), 'Foreign installation target')
print(json.dumps(dict(version=sys.version.split()[0], executable=sys.executable,
                     prefix=sys.prefix, base_prefix=sys.base_prefix)))
'''
