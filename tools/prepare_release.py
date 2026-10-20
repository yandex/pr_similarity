"""Export a new standalone source tree from the explicit portable file inventory."""

import argparse
import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

from check_release import MANIFEST, check_release, file_record, regular_file, source_inventory


def prepare_release(source: Path, destination: Path) -> dict:
    """Create a verified export without replacing an existing destination."""
    source = source.resolve()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Destination already exists; select a new directory.')
    if destination.resolve().is_relative_to(source):
        raise ValueError('Destination must be outside the source repository.')
    selected = source_inventory(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.' + destination.name + '-export-', dir=destination.parent))
    reserved = False
    try:
        for relative in selected:
            original = regular_file(source, relative)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            target.chmod(0o755 if file_record(source, relative)['executable'] else 0o644)
        version = tomllib.loads((staging / 'benchmark/pyproject.toml').read_text())['project']['version']
        manifest = {'manifest_version': 1, 'release_version': version,
                    'files': [file_record(staging, path) for path in selected]}
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n')
        result = check_release(staging)
        # Reserve the named destination before promotion; even an empty existing directory is never reused.
        destination.mkdir()
        reserved = True
        for path in staging.iterdir():
            os.rename(path, destination / path.name)
        staging.rmdir()
        return result
    except BaseException:
        # Only remove the directory reserved by this invocation, never a pre-existing destination.
        if reserved:
            shutil.rmtree(destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_release(args.source, args.destination), indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
