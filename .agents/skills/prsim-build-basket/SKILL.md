---
name: prsim-build-basket
description: Use when configuring, building or resuming PR Similarity GitHub/GitLab baskets, or diagnosing collector rejection and task-generation failures.
---

# Build a PR Similarity basket

Read root/collector AGENTS.md and the [collector guide](../../../basket_collector/README.md),
particularly selection, generation and artifact inspection. Use `basket_collector/`,
its `.venv` and `make help`; start from a complete tracked YAML example.

Resolve source, period or explicit changes, generation mode, selection and output.
Preserve config-relative paths and credential variable names. Host Git access,
collector LLM access and benchmark ChatGPT OAuth are separate. A config-only ask
permits loader checks, not discovery, generation or paid regeneration.

For an authorized build, use `make basket CONFIG=...` or the requested stage.
Inspect existing stage/config identity before resume. Reconcile mismatches or use
a separate output; `OVERWRITE=1` is a requested rebuild, not an automatic repair.
Keep retries and model calls inside the authorized scope.

Verify completed exports and actual counts: collected → filtered → generated →
exported. Selection limit is a maximum; first rejection reasons are sequential.
Separate filter rejection from generation failure and retain partial coverage.
Preserve the basket name and generator provenance exactly as exported. A model
name in that label does not set the benchmark agent Model/Effort; absent generator
metadata stays unknown. Never hand-edit frozen JSON to bypass validation.

Return config, frozen basket path, actual exported task count, stage status and
remaining gaps. An existing completed basket needs no regeneration to benchmark.
