---
name: prsim-run-benchmark
description: Use when preparing PR Similarity agent YAML, access or sandbox integrations, or executing an authorized benchmark run or resume.
---

# Run a PR Similarity benchmark

Read root/benchmark AGENTS.md and the [benchmark guide](../../../benchmark/README.md).
Use `benchmark/`, its `.venv` and `make help`. Resolve the complete frozen DATASET,
exact native provider/model/effort, pinned runtime/access mode, workers and run path.
Load config/basket without printing credential values before any authorized run.
Accept frozen basket v1/v2 without conversion; v1's upgrade notice is not a failed
validation. Report v2 coverage/omissions separately from execution failures and
run all exported tasks. Verify that a collector output is the intended publication:
a blocked retry may have left the previous basket in place. A new export requires
a new run; resume always uses the original basket bytes.

`agent.name` and top-level YAML `tags` freeze local labels at creation. Setup keeps
strings/order/duplicates as-is. Model displays `model [effort]` for a known
level, otherwise `model`. Configured native facts supply the label, independent
of user prefixes or basket generator names. Effective
config is separate runtime evidence and does not prove provider behavior. Unknown
or unsupported effort stays explicit; set desired supported effort deliberately.

API mode binds secret variable names. Subscription access uses `make auth` and a
private profile outside runs; let the user finish interactive login. Never use host
HOME/profiles or after-revision to repair isolation. Rule/skill/setup resources must
exist, have stable contents and remain outside scored source. Verify sandbox-visible
MCP/CLI tools, lifetime, source writes and exclusions against the pinned runtime.
Repository operator skills are not automatically supplied to the benchmarked agent.

The run freezes `agent/opencode.json` and declared resources once. Attempts use
private copies and retain their own captures; shared effective snapshots have
explicit seal refs. Resume rejects changed semantic inputs.

Preparation does not authorize model calls. For an authorized run use
`make run CONFIG=... DATASET=...`; retain the exact printed timestamp/agent/suffix
directory. For resume supply its RESUME and original whole basket/compatible
identity. Worker counts/technical paths may change; local name/Setup edits are
ignored. Intact selected native seals allow host-only scoring with the same native
UUID/timings and no new model call. A new task retry consumes a new sequence.

Read `report.json`, `run.log` and the selected attempt’s live `agent.log`.
Native JSON is `events.jsonl`; stderr/non-JSON is `runtime.log`.
Check receipts/coverage, not exit code alone. Return run path, actual counts,
independent execution/change/scoring states and final/all native/API cost coverage.
Generate HTML or publish only when explicitly requested. OAuth cleanup needs its
own authorization and stopped users of the profile.

Included 2026H1 v2 baskets live in `benchmark/examples/baskets/pandas/` and
`benchmark/examples/baskets/opensearch/`, with 1, 5, 10, 25 and 100 tasks each.
Use the [basket catalog](../../../benchmark/examples/baskets/README.md) to select
a size; baskets within each repository are disjoint, not nested. Historical v1
examples remain under `benchmark/examples/baskets/legacy/`.
