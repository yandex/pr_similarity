# Changelog

## 0.2.0

- Upgrade the basket collector to schema v2 with explicit population coverage,
  omissions, selection fingerprints and stage lineage. Resume failed records
  with separate retry, partial-export and reselection decisions; preserve the
  last published basket until a replacement is ready.
- Accept frozen basket v1 and v2 in benchmark and viewer, validating content,
  lineage and report bindings. Keep original basket bytes on resume and
  publication; legacy v1 inputs receive an upgrade notice.
- Include ten disjoint 2026H1 v2 baskets: pandas and OpenSearch, each with
  1, 5, 10, 25 and 100 tasks. Retain the original v1 examples under `legacy/`.
- Align the READMEs and repository operator skills with v2 collection,
  benchmark execution, artifact inspection and verified HTTPS publication.
- Record the product release version in the source export manifest and verify
  it against all three module versions, alongside file sizes and SHA-256 hashes.

Collector v1 checkpoints cannot be resumed by the v2 collector; start a new
output directory. Existing frozen v1 baskets remain supported. Product release,
basket schema and agent config versions are independent. Update benchmark and
viewer together before publishing v2 runs to an older deployment. Follow
[Upgrade from v0.1](README.md#upgrade-from-v01), including package reinstallation,
SQLite backup and rollback. Persistent hosting is covered by the
[Linux service recipe](viewer/README.md#persistent-linux-service).

Python 3.12 and Linux x64 agent execution remain the supported benchmark setup.
OpenCode examples retain 1.18.33; live smoke checks also cover 1.19.3.
macOS benchmark/viewer execution has not been validated for this release.

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
