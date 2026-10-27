"""Workspace/repository-scoped glob exclusions with exact segment boundaries."""

import re


def resolve_exclude(spec: str, workspace: str) -> str:
    """Resolve a task exclusion relative to its exported workspace."""
    if spec.startswith('<repo_root>/'):
        return spec[len('<repo_root>/') :]
    spec = spec.removeprefix('<workspace>/')
    prefix = '' if workspace == '.' else workspace + '/'
    return prefix + spec.lstrip('/')


def compile_exclude(pattern: str) -> re.Pattern[str]:
    """Compile the documented glob syntax into a path matcher."""
    pattern = pattern.rstrip('/')
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith('**/', index):
            parts.append('(?:.*/)?')
            index += 3
        elif pattern.startswith('**', index):
            parts.append('.*')
            index += 2
        elif pattern[index] == '*':
            parts.append('[^/]*')
            index += 1
        elif pattern[index] == '?':
            parts.append('[^/]')
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile('^' + ''.join(parts) + '(?:/.*)?$')
