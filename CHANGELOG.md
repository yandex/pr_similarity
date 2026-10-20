# Changelog

## 0.1.1

- Check whether a discovered bubblewrap supports the sandbox options before starting
  agents. With `sandbox.auto_install: true` (the default), an incompatible system
  build, such as Ubuntu 22.04's bubblewrap 0.6.1, is skipped with a warning and the
  benchmark uses its verified, pinned copy from the user cache.
- When automatic installation is disabled, stop with an error that identifies the
  incompatible executable and version and explains both remedies: install a
  compatible bubblewrap (upstream 0.8.0 or newer) or enable `sandbox.auto_install`.
  Do not announce a fallback when it is disabled.
- Detect supported options rather than relying on the version number, so compatible
  distribution backports remain usable. Explicitly configured custom executables
  remain the operator's choice; user namespace support is still required on the host.

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
