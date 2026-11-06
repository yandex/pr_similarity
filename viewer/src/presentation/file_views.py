"""Bounded full-file views and raw change counts for both run-page readers."""

import difflib
from collections.abc import Callable, Sequence
from hashlib import sha256
from typing import Protocol


class FileExistence(Protocol):
    """Expose source-role existence to either package's renderer."""

    @property
    def base(self) -> bool:
        """Return whether the base source exists."""
        ...

    def to_wire(self) -> dict:
        """Return the original source-role existence mapping."""
        ...


class SourceFile(Protocol):
    """Expose selected source facts without importing either application's models."""

    @property
    def path(self) -> str:
        """Return the source-relative path."""
        ...

    @property
    def base(self) -> str | None:
        """Return the base artifact reference."""
        ...

    @property
    def human(self) -> str | None:
        """Return the human artifact reference."""
        ...

    @property
    def ai(self) -> str | None:
        """Return the agent artifact reference."""
        ...

    @property
    def changed_human(self) -> bool:
        """Return whether the human changed this file."""
        ...

    @property
    def changed_agent(self) -> bool:
        """Return whether the agent changed this file."""
        ...

    @property
    def mlp(self) -> int | float:
        """Return the selected file score."""
        ...

    @property
    def exists(self) -> FileExistence:
        """Return source-role existence facts."""
        ...


class TaskScoring(Protocol):
    """Expose the selected score status and source files."""

    @property
    def status(self) -> str:
        """Return the score availability status."""
        ...

    @property
    def files(self) -> Sequence[SourceFile]:
        """Return selected source files in report order."""
        ...


class ScoredTask(Protocol):
    """Expose the selected scoring projection used by the shared counter."""

    @property
    def scoring(self) -> TaskScoring:
        """Return the task's selected scoring projection."""
        ...


def full_file_lines(before: list[str], after: list[str], byte_limit: int, line_limit: int) -> dict:
    """Show every context line with bounded inline additions/deletions, without patch syntax."""
    rows = []
    size = 0
    truncated = False

    def add(kind: str, text: str, base_line: int | None, result_line: int | None) -> bool:
        """Append a display row within the byte and line limits."""
        nonlocal size, truncated
        width = len(text.encode('utf-8'))
        if len(rows) >= line_limit or size + width > byte_limit:
            truncated = True
            return False
        rows.append(
            {
                'kind': kind,
                'text': text.removesuffix('\n').removesuffix('\r'),
                'base_line': base_line,
                'result_line': result_line,
            }
        )
        size += width
        return True

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after).get_opcodes():
        if tag == 'equal':
            for i, j in zip(range(i1, i2), range(j1, j2)):
                if not add('context', after[j], i + 1, j + 1):
                    return {'lines': rows, 'truncated': truncated}
        else:
            for i in range(i1, i2):
                if not add('removed', before[i], i + 1, None):
                    return {'lines': rows, 'truncated': truncated}
            for j in range(j1, j2):
                if not add('added', after[j], None, j + 1):
                    return {'lines': rows, 'truncated': truncated}
    return {'lines': rows, 'truncated': truncated}


def file_view(
    row: SourceFile, read_source: Callable[[str, int], bytes], cap: int, line_limit: int, full: bool = False
) -> dict:
    """Render verified source bytes using identical online/offline limits."""
    file_id = sha256(row.path.encode()).hexdigest()
    sources = {}
    source_truncated = {}
    truncated = False
    for role in ('base', 'human', 'ai'):
        if getattr(row, role) is None:
            sources[role] = []
            source_truncated[role] = False
            continue
        data = read_source(getattr(row, role), cap + 1)
        source_truncated[role] = len(data) > cap
        truncated |= len(data) > cap
        text = data[:cap].decode('utf-8', errors='replace')
        lines = text.splitlines(keepends=True)
        if len(lines) > line_limit:
            lines = lines[:line_limit]
            source_truncated[role] = True
            truncated = True
        sources[role] = lines

    if full:

        def full_side(role: str) -> dict:
            """Render one role with its existence and truncation state."""
            result = full_file_lines(sources['base'], sources[role], cap, line_limit)
            result['truncated'] |= source_truncated['base'] or source_truncated[role]
            before, after = row.exists.base, getattr(row.exists, role)
            result['state'] = (
                ('added' if after else 'absent')
                if not before
                else (
                    'deleted'
                    if not after
                    else 'modified' if (row.changed_human if role == 'human' else row.changed_agent) else 'unchanged'
                )
            )
            result['ends_with_newline'] = not sources[role] or sources[role][-1].endswith('\n')
            return result

        return {
            'file_id': file_id,
            'path': row.path,
            'score': row.mlp,
            'exists': row.exists.to_wire(),
            'human_file': full_side('human'),
            'agent_file': full_side('ai'),
        }

    def side(role: str) -> dict:
        """Read one source role using its recorded existence and digest."""
        chunks = []
        length = 0
        cut = truncated
        for line in difflib.unified_diff(
            sources['base'], sources[role], fromfile='base/' + row.path, tofile=role + '/' + row.path
        ):
            if not line.endswith('\n'):
                line += '\n\\ No newline at end of file\n'
            data = line.encode()
            room = cap - length
            if len(data) > room:
                chunks.append(data[:room].decode(errors='ignore'))
                cut = True
                break
            chunks.append(line)
            length += len(data)
        if not chunks and row.exists.base != getattr(row.exists, role):
            chunks = ['[File ' + ('added' if getattr(row.exists, role) else 'deleted') + '; empty content]\n']
        content = ''.join(chunks).encode()
        if len(content) > cap:
            content = content[:cap]
            cut = True
        return {'text': content.decode(errors='ignore'), 'truncated': cut}

    return {
        'file_id': file_id,
        'path': row.path,
        'score': row.mlp,
        'exists': row.exists.to_wire(),
        'human_diff': side('human'),
        'agent_diff': side('ai'),
    }


def change_counts(task: ScoredTask, read_source: Callable[[str, int], bytes], cap: int, line_cap: int) -> dict:
    """Count raw line additions/removals for scored files, separately from normalized LOC weights."""
    if task.scoring.status != 'available':
        return {
            role: {'status': 'unavailable', 'added': None, 'removed': None, 'files': None, 'measured_files': 0}
            for role in ('human', 'agent')
        }
    result = {
        role: {'status': 'complete', 'added': 0, 'removed': 0, 'files': 0, 'measured_files': 0}
        for role in ('human', 'agent')
    }

    def read(ref: str | None) -> tuple[list[str], bool]:
        """Read a source role or indicate that it exceeds the display budget."""
        if ref is None:
            return [], False
        raw = read_source(ref, cap + 1)
        if len(raw) > cap:
            return [], True
        lines = raw.decode('utf-8', errors='replace').splitlines(keepends=True)
        return ([], True) if len(lines) > line_cap else (lines, False)

    for file in task.scoring.files:
        if not file.changed_human and not file.changed_agent:
            continue
        base, base_cut = read(file.base)
        for role, ref, changed in [('human', file.human, file.changed_human), ('agent', file.ai, file.changed_agent)]:
            if not changed:
                continue
            item = result[role]
            item['files'] += 1
            if base_cut:
                continue
            lines, cut = read(ref)
            if cut:
                continue
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, lines).get_opcodes():
                if tag != 'equal':
                    item['removed'] += i2 - i1
                    item['added'] += j2 - j1
            item['measured_files'] += 1
    for item in result.values():
        if item['measured_files'] < item['files']:
            item['status'] = 'partial' if item['measured_files'] else 'unavailable'
            if not item['measured_files']:
                item['added'] = item['removed'] = None
    return result
