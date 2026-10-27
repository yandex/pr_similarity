---
name: prsim-build-basket
description: Use when configuring, building or resuming PR Similarity GitHub/GitLab baskets, or diagnosing collector rejection and task-generation failures.
---

# Build a PR Similarity basket

Read root/collector AGENTS.md and the [collector guide](../../../basket_collector/README.md),
particularly selection, generation and artifact inspection. Use `basket_collector/`,
its `.venv` and `make help`; start from a complete tracked YAML example.

Validate configuration and artifacts with their owning loaders. Follow the
[saved-data compatibility note](../../../README.md#reading-data-from-earlier-releases)
when opening existing output. Never edit frozen basket JSON to bypass validation.

Resolve source, period or explicit changes, generation mode, selection and output.
Preserve config-relative paths and credential variable names. Host Git access,
collector LLM access and benchmark ChatGPT OAuth are separate. A config-only ask
permits loader checks, not discovery, generation or paid regeneration.

For an authorized build, use `make basket CONFIG=...` or the requested stage.
Inspect existing stage/config identity before resume. Reconcile mismatches or use
a separate output; `OVERWRITE=1` is a requested rebuild, not an automatic repair.
Keep retries and model calls inside the authorized scope. Continue interrupted
work with the same CONFIG; changes to settings, including collector workers, can
invalidate reuse. Do not apply benchmark's worker-change resume rules here.

Use the [resume rules](../../../basket_collector/docs/operations.md#resume-and-partial-results):

- `RETRY_FAILED=1` opens a new budget for failed records in collect/filter/generate
  or basket, not export. Deterministic `rejected` records are not retried.
- `ALLOW_PARTIAL=1` accepts known omissions; it does not retry or accept a changed
  selection. Report the exact missing population before choosing it.
- `RESELECT=1` accepts a changed ordered selection in filter/basket. If authorized
  recovery changes selection, use `RETRY_FAILED=1 RESELECT=1`; retry permission
  alone does not imply permission to change the selected population.
- `OVERWRITE=1` recalculates the requested stages and cannot be combined with
  `RETRY_FAILED` or `RESELECT`. Never use it to bypass a pending decision.

Exit code 3 requires inspection: saved work may need retry, partial/selection
acceptance, or have no successful records. An empty basket is never exported.
Inspect `collected.json`, `filtered.json`, `selection.json`, `tasks.json` and
`.work/<stage>.json`; completed processing can still await selection/export.
The last published `basket.json` survives until a new export is ready, so its
presence does not prove the current invocation succeeded.

When debugging repeated Git failures such as
`fatal: shallow file has changed since we read it`, inspect the native DEBUG
error and identify the affected task's exact Git cache directory. Automatic
Git maintenance is a possible cause even with only one active task. Once no
operation is using that cache, a targeted diagnostic workaround is:

```bash
git --git-dir="<affected-task-cache>" config --local maintenance.auto false
git --git-dir="<affected-task-cache>" config --local gc.auto 0
```

Record the previous local values (including absent keys) before changing them.
The collector strips inherited `GIT_CONFIG_*` overrides, so environment-only
settings do not apply. Keep this workaround limited to the affected cache;
do not disable maintenance globally or across all task caches. With retry
authorization, retry the failed filter record using the same CONFIG and
`RETRY_FAILED=1`, preserving successful records and the selection rules above.
Verify the resulting record status; a successful Git retry can still produce a
normal filter rejection. Report the cache/settings changed and arrange to
restore the previous values when the workaround is no longer needed, with no
active cache users. This is not a cache-retention policy: large collections
still need maintenance and cleanup. Do not delete caches/checkpoints or use
`OVERWRITE=1` merely to bypass this error.

Verify completed exports and actual counts: collected → filtered → generated →
exported. Selection limit is a maximum; first rejection reasons are sequential.
Separate filter rejection, failed records at each stage and generation rejection.
Validate exports with collector models/schema; inspect `meta.coverage` and
`meta.omissions` alongside stage status/counts and selection fingerprints.
Preserve the basket name and generator provenance exactly as exported. A model
name in that label does not set the benchmark agent Model/Effort; absent generator
metadata stays unknown. Never hand-edit frozen JSON to bypass validation.

Return config, frozen basket path, actual exported task count, stage status and
remaining gaps. An existing completed basket needs no regeneration to benchmark.

The [frozen basket catalog](../../../benchmark/examples/baskets/README.md) contains
ready-to-use pandas/OpenSearch inputs; no collector rerun is needed.
Existing runs retain their original basket bytes. See
[full.yaml](../../../basket_collector/examples/full.yaml) for every collector
configuration field and the supported alternatives.
