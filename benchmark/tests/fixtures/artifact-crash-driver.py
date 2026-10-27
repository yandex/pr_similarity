"""Pause real public execution at durable boundaries for owned-process SIGKILL tests."""

import os
import sys
import threading
from pathlib import Path

from benchmark.config import load_config
from benchmark.execution.opencode import EventTracker
from benchmark.execution.provisioning import Runtime
from benchmark.json_types import JsonObject
from benchmark.models import Report
from benchmark.models.evidence import Outcome
from benchmark.models.runtime import ProvisioningReceipt
from benchmark.persistence import run_store
from benchmark.persistence.storage import bytes_sha256, write_json
from benchmark.runtime import attempt, coordinator

ROOT = Path(sys.argv[1])
BOUNDARY = sys.argv[2]


def descendants(parent: int) -> list[dict]:
    """Record only this driver's descendant process identities without reading environments."""
    result = []
    pending = [parent]
    while pending:
        current = pending.pop()
        for task in Path(f'/proc/{current}/task').glob('*/children'):
            try:
                children = task.read_text().split()
            except OSError:
                continue
            for child in children:
                pid = int(child)
                if any(item['pid'] == pid for item in result):
                    continue
                try:
                    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                except OSError:
                    continue
                result.append({'pid': pid, 'start_time': int(fields[19])})
                pending.append(pid)
    return result


def pause(directory: Path) -> None:
    """Expose the exact owned run and wait for the supervising test to kill this process group."""
    write_json(
        ROOT / 'paused.json',
        {
            'boundary': BOUNDARY,
            'run_directory': str(directory),
            'pid': os.getpid(),
            'descendants': descendants(os.getpid()),
        },
    )
    threading.Event().wait()


def install() -> None:
    """Instrument test-only process boundaries while retaining every production execution path."""
    if BOUNDARY == 'reserved':
        original = attempt._Attempt.prepare

        def prepare(self: attempt._Attempt) -> None:
            """Pause only after coordinator reservation and before native preparation."""
            pause(self.run_dir)
            original(self)

        attempt._Attempt.prepare = prepare
    elif BOUNDARY == 'seal':
        original_score = attempt._Attempt.score

        def score(self: attempt._Attempt) -> None:
            """Pause after immutable native sealing and before host scoring starts."""
            pause(self.run_dir)
            original_score(self)

        attempt._Attempt.score = score
    elif BOUNDARY == 'outcome':
        original_accept = coordinator.Coordinator.accept

        def accept(self: coordinator.Coordinator, identifier: str, returned: Outcome | None) -> str:
            """Pause with a complete outcome before the writer accepts its state reference."""
            assert returned is not None
            pause(self.run_dir)
            return original_accept(self, identifier, returned)

        coordinator.Coordinator.accept = accept
    elif BOUNDARY == 'state':
        original_write = run_store.write_json

        def write(path: Path, value: object, *, preserve_order: bool = False) -> None:
            """Pause after completed state commit and before updating the derived report cache."""
            if (
                path.name == 'report.json'
                and isinstance(value, Report)
                and value.tasks[0].processing_status == 'completed'
            ):
                pause(path.parent)
            original_write(path, value, preserve_order=preserve_order)

        run_store.write_json = write
    else:
        assert BOUNDARY == 'native'
        original_event = EventTracker.accept_event

        def event(self: EventTracker, value: JsonObject) -> None:
            """Observe an actually live namespaced native child before killing its host parent."""
            original_event(self, value)
            if value.get('type') == 'step_start':
                pause(next((ROOT / 'runs').iterdir()))

        EventTracker.accept_event = event


def main() -> None:
    """Run public orchestration with only the network provisioning adapter replaced."""
    binary = ROOT / 'controlled-opencode'
    digest = bytes_sha256(binary.read_bytes())
    runtime = Runtime(
        binary,
        ProvisioningReceipt(
            selector='opencode:v1.18.33',
            actual_version='1.18.33',
            asset_name='offline-native-fixture.py',
            platform='linux-x64-offline-fixture',
            archive_sha256=digest,
            binary_sha256=digest,
        ),
    )
    coordinator.provision_opencode = lambda *_args, **_kwargs: runtime
    install()
    coordinator.run(load_config(ROOT / 'config.yaml'), ROOT / 'basket.json', output=ROOT / 'runs', progress='off')


if __name__ == '__main__':
    main()
