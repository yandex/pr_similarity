"""A discovered bubblewrap is only used when it accepts the required sandbox options."""

from pathlib import Path

import pytest
from benchmark.bubblewrap import CAPABILITY_ARGV, MINIMUM_VERSION, _capable, resolve_bubblewrap
from benchmark.errors import BenchmarkError
from benchmark.sandbox import Sandbox

OLD_BWRAP = """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'bubblewrap 0.6.1\\n'
  exit 0
fi
printf 'bwrap: Unknown option %s\\n' "$2" >&2
exit 1
"""

CAPABLE_BWRAP = """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'bubblewrap 0.8.0\\n'
  exit 0
fi
exit 0
"""

HOST_FAILURE_BWRAP = """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf 'bubblewrap 0.9.0\\n'
  exit 0
fi
printf 'bwrap: No permissions to create new namespace\\n' >&2
exit 1
"""

# A rejection may arrive on either stream while the other carries diagnostics.
MIXED_STREAM_BWRAP = """#!/bin/sh
printf 'bwrap: Unknown option --disable-userns\\n'
printf 'bwrap: warning: unrelated diagnostic\\n' >&2
exit 1
"""


def stub(directory: Path, name: str, content: str) -> Path:
    """Write one executable bubblewrap stub and return its path."""
    binary = directory / name
    binary.write_text(content)
    binary.chmod(0o700)
    return binary


@pytest.mark.parametrize(
    ('content', 'expected'),
    [(OLD_BWRAP, False), (CAPABLE_BWRAP, True), (HOST_FAILURE_BWRAP, True), (MIXED_STREAM_BWRAP, False)],
    ids=['missing-option', 'capable', 'host-failure', 'rejection-with-diagnostics'],
)
def test_probe_reads_the_option_support_from_both_streams(
    tmp_path: Path, content: str, expected: bool
) -> None:
    """Only a rejected option fails the probe; a host failure stays usable."""
    assert _capable(stub(tmp_path, 'bwrap', content), 10) is expected


def test_probe_rejects_an_executable_that_cannot_start(tmp_path: Path) -> None:
    """A candidate that cannot be executed is never selected."""
    broken = tmp_path / 'broken'
    broken.write_text('#!/bin/sh\nexit 0\n')
    broken.chmod(0o600)
    assert _capable(broken, 10) is False


def test_discovered_old_binary_is_rejected_before_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A too-old system bubblewrap never reaches the sandbox command."""
    old = stub(tmp_path, 'bwrap', OLD_BWRAP)
    monkeypatch.setattr('benchmark.bubblewrap.shutil.which', lambda name: str(old))
    notices = []
    with pytest.raises(BenchmarkError) as caught:
        resolve_bubblewrap('bwrap', tmp_path / 'cache', auto_install=False, on_notice=notices.append)
    assert caught.value.code == 'sandbox_missing'
    # The message names the discovered version and the required minimum.
    assert str(old) in caught.value.message and '0.6.1' in caught.value.message
    assert MINIMUM_VERSION in caught.value.message
    assert notices and '0.6.1' in notices[0]


def test_capable_system_binary_is_used_without_provisioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A current system bubblewrap is preferred over the pinned fallback."""
    capable = stub(tmp_path, 'bwrap', CAPABLE_BWRAP)
    monkeypatch.setattr('benchmark.bubblewrap.shutil.which', lambda name: str(capable))
    notices = []
    resolved = resolve_bubblewrap('bwrap', tmp_path / 'cache', auto_install=False, on_notice=notices.append)
    assert resolved == capable.resolve()
    assert notices == []


def test_custom_executable_is_never_probed_or_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly configured name stays the operator's choice."""
    custom = stub(tmp_path, 'my-bwrap', OLD_BWRAP)
    monkeypatch.setattr('benchmark.bubblewrap.shutil.which', lambda name: str(custom))
    assert resolve_bubblewrap(str(custom), tmp_path / 'cache', auto_install=False) == custom.resolve()


def test_absent_custom_executable_is_reported_without_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured name that resolves nowhere is never silently replaced."""
    monkeypatch.setattr('benchmark.bubblewrap.shutil.which', lambda name: None)
    with pytest.raises(BenchmarkError) as caught:
        resolve_bubblewrap('my-bwrap', tmp_path / 'cache', auto_install=True)
    assert caught.value.code == 'sandbox_missing'
    assert 'sandbox.executable' in caught.value.message


def test_missing_binary_reports_install_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No candidate at all produces actionable install guidance."""
    monkeypatch.setattr('benchmark.bubblewrap.shutil.which', lambda name: None)
    with pytest.raises(BenchmarkError) as caught:
        resolve_bubblewrap('bwrap', tmp_path / 'cache', auto_install=False)
    assert caught.value.code == 'sandbox_missing'
    assert MINIMUM_VERSION in caught.value.message


def test_probe_exercises_every_namespace_option_the_sandbox_uses(tmp_path: Path) -> None:
    """The probe covers the namespace and security flags the sandbox command emits.

    Mount and filesystem options are excluded: they create a sandbox rather than gate
    a version, so they are not what a discovery probe must establish.
    """
    source = tmp_path / 'source'
    source.mkdir()
    sandbox = Sandbox('/usr/bin/bwrap', source, source, tmp_path / 'home', tmp_path / 'runtime', Path('/usr/bin/true'))
    mount_options = {'--ro-bind', '--bind', '--symlink', '--proc', '--dev', '--tmpfs', '--chdir'}
    required = {
        option
        for option in sandbox.command(['/usr/bin/true'])
        if option.startswith('--') and option not in mount_options
    }
    assert required <= set(CAPABILITY_ARGV)
    assert '--disable-userns' in CAPABILITY_ARGV
