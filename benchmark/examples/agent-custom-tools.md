# Documentation search tool

Setup installs `doc-search` on PATH. When repository documentation could clarify
an API or behavior relevant to the task, run `doc-search "literal query"` through
the shell tool. It returns at most 20 matching lines from Markdown/text under
`DOC_SEARCH_ROOT`, with file paths and line numbers. It may report that docs are
absent or have no literal match; then use normal repository search.

This tool only searches the original source snapshot. Continue solving the
provided task, follow repository instructions, and make only necessary edits.
Do not fetch the target PR, after-revision or human solution. Report tool failures
and tests honestly. Tool installation lives in HOME and needs no diff exclusion.
