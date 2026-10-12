"""Change detection module.

Responsibilities:
- Text normalization for comparison
- Detecting whether a file has changed relative to base
- Computing change magnitude proxy (churn)
"""

import difflib
import re


def normalize(text: str) -> str:
    """Normalize text for comparison.

    Transformations:
    - Convert CRLF to LF
    - Remove trailing whitespace from each line

    Args:
        text: Raw text content

    Returns:
        Normalized text string

    """
    text = text.replace('\r\n', '\n')
    lines = text.split('\n')
    lines = [line.rstrip() for line in lines]
    return '\n'.join(lines)


def detect_changed(base_text: str, new_text: str) -> bool:
    """Identify changed lines while preserving the fixed normalization rules."""
    return normalize(base_text) != normalize(new_text)


def tokenize(text: str) -> list[str]:
    """Tokenize text into words and punctuation.

    Uses Unicode-aware regex to split on word boundaries.
    All tokens are lowercased.

    Args:
        text: Text to tokenize

    Returns:
        List of lowercase tokens

    """
    tokens = re.findall(r'\w+|[^\s\w]', text, flags=re.UNICODE)
    return [t.lower() for t in tokens]


def count_diff_lines(base_text: str, new_text: str) -> int:
    """Count changed lines between base and new text.

    Returns:
        Number of added + removed lines as inferred by difflib opcodes.

    Notes:
        - Uses normalized lines (CRLF->LF and trailing whitespace trimmed)
        - Returns at least 1 for differing texts that produce zero line ops
          (e.g. some whitespace-only differences depending on inputs).

    """
    base_lines = normalize(base_text).splitlines()
    new_lines = normalize(new_text).splitlines()

    matcher = difflib.SequenceMatcher(None, base_lines, new_lines)

    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'replace':
            removed += i2 - i1
            added += j2 - j1
        elif tag == 'delete':
            removed += i2 - i1
        elif tag == 'insert':
            added += j2 - j1

    total = added + removed

    # Edge case: texts differ, but we ended up with no counted ops.
    if total == 0 and normalize(base_text) != normalize(new_text):
        return 1

    return total
