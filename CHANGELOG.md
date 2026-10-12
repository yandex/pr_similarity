# Changelog

## 0.1.0

- Build frozen coding baskets from merged GitHub pull requests and GitLab merge requests.
- Run pinned OpenCode in before-only bubblewrap sandboxes and compare edits with human changes.
- Inspect source triplets, native events, readable logs, scores and usage; resume and rescore saved evidence.
- Save native configuration and declared resources once per run, with verified attempt references.
- Publish portable archives to a standalone viewer with accounts, run comparison and file diffs.
- Show native OpenCode configuration separately from benchmark metadata.

Collector and benchmark require Python 3.12. Agent execution requires Linux x64
with glibc and working bubblewrap user namespaces. Viewer requires Python 3.12
or newer; its deployment is separate. OpenCode examples pin 1.18.33.

Similarity measures agreement with human edits; it is not a test pass rate.
Executable-bit-only changes are outside the v1 metric scope. Older prerelease
run layouts are unsupported; automatic migration is not provided.
