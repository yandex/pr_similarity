#!/bin/bash
# Install an example retrieval CLI outside the scored source tree.
set -euo pipefail
command -v python3 >/dev/null
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/doc-search" <<'PY'
#!/usr/bin/python3
"""Bounded literal search of the before snapshot's documentation."""
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('query', nargs='?')
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
if args.check:
    print('doc-search ready')
    raise SystemExit(0)
if not args.query:
    parser.error('query is required')
repo = Path('/repo').resolve()
root = Path(os.environ.get('DOC_SEARCH_ROOT', '/repo/docs')).resolve()
if not root.is_relative_to(repo):
    parser.error('DOC_SEARCH_ROOT must stay inside /repo')
if not root.is_dir():
    print('Documentation directory is absent; use repository search instead.')
    raise SystemExit(0)
query = args.query.casefold()
hits = 0
for number, path in enumerate(sorted(root.rglob('*'))):
    if number >= 2000:
        break
    if not path.is_file() or path.suffix.lower() not in ('.md', '.txt', '.rst'):
        continue
    if not path.resolve().is_relative_to(repo) or path.stat().st_size > 1_000_000:
        continue
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        continue
    for index, line in enumerate(lines, 1):
        if query in line.casefold():
            print(f'{path.relative_to(repo)}:{index}: {line[:300]}')
            hits += 1
            if hits >= 20:
                raise SystemExit(0)
if not hits:
    print('No literal matches; use repository search instead.')
PY
chmod 700 "$HOME/.local/bin/doc-search"
doc-search --check
