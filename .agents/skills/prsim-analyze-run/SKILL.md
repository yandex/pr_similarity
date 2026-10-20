---
name: prsim-analyze-run
description: Use when inspecting saved PR Similarity task behavior, logs, source triplets, scoring, usage or pricing, or comparing equivalent runs.
---

# Analyze a saved run

Read root/benchmark AGENTS.md, the [artifact contract](../../../benchmark/README.md#further-details)
and [pricing guide](../../../benchmark/README.md#estimate-cost-with-reprice).
Analysis is read-only. Rescore/reprice, HTML generation, publication and new model
calls are separate actions requiring the corresponding request.

Start at `report.json`: record frozen basket/run identity and actual coverage.
Read the run's frozen basket, not a newer collector output. Both v1/v2 are valid
inputs; never convert them during analysis. For v2, inspect `meta.coverage` and
`meta.omissions`, separating failed collector records and generation rejections
from agent execution failures. Exported tasks are the benchmark population.
Select by task `ordinal`/PR number and `directory`; follow its selected final
attempt and score/price refs in `_meta/state.json`. For an explicitly requested
older attempt, find its sequence in that registered task's current/history refs and
open `tasks/<key>/attempts/NNN/`. Never choose a success/newest directory instead
of the authoritative selection. Valid state can rebuild a report cache; damaged
state or legacy layouts are not reconstructed by guessing.

Read shared native input through `report.agent_input`: `agent/opencode.json`
and declared resources are frozen once. Effective refs point to unique
`agent/effective/<sha256>.json` files, owned by each selected attempt seal.
Shared bytes do not imply shared execution or successful capture.

Read live `agent.log` for timestamped text/tool targets/outcomes. Follow truncated
output's `events.jsonl` line; raw JSON retains unknown event types. Read stderr and
non-JSON stdout in `runtime.log`, setup in `setup.log`, harness tracebacks in
`_meta/invocations/<uuid>/benchmark.log`. Native `seal.json` and stream/usage receipts
show completeness; human formatting failure does not invalidate intact raw capture.
Sanitized SQLite is accounting, not a transcript. Quote only relevant safe excerpts.

Use `triplets/index.json` existence/mode/digest mappings and explicit file refs
to compare base, human and agent source states; absent is not empty. Separate
execution, native infrastructure, captured changes, score availability
and usage. Valid scores can survive timeout/provider errors; similarity is not test
correctness. Host scoring recovery selects operations without rewriting native outcome.

Use `report.inference` configured/effective values and origins. Model displays
`model [effort]` for a known level, otherwise `model`;
unknown is not a default. Preserve basket generator provenance separately. Setup
is ordered user YAML/server metadata as-is, including model-like strings.

Read `report.costs.final_attempts` and `all_attempts`, each with `native_reported`
and `api_equivalent` status/amount/known_subtotal/coverage. Retry spend differs from
final cost; newer unpriced attempts create gaps. `not_requested`, `not_started` and
null amounts are not measured zeros. API equivalence is not a subscription bill.
Return findings with ordinal/attempt, exact evidence paths and uncertainty. Preserve
immutable artifacts and state; do not hand-edit files to repair an experiment.
