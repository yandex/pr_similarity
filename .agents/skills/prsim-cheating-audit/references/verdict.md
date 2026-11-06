# Verdict and report

Write REPORT.md and verdict.json outside the source corpus. Use source-relative
paths plus raw file hashes and line/JSON/record anchors. A line in an index is a
navigation aid; cite the underlying event and verified attempt binding as well.
Do not change archived state, scores, names, tags or pins.

## Information-access outcome

- `confirmed_leakage`: at least one prohibited answer/future/hidden input was
  demonstrably obtained in, or carried into, the selected attempt; identify the
  affected tasks without extrapolating to the rest
- `suspicious`: a meaningful indicator exists, but content, eligibility or
  selected-attempt linkage is unresolved
- `no_evidence_with_limits`: the reviewed corpus supports an assessment and shows
  no confirmed prohibited-information access; disclose the inspected coverage and
  gaps, never certify clean execution
- `insufficient_evidence`: the available binding or content coverage cannot support
  the requested run-level assessment; absence of logs is not an affirmative suspicion

A known VCS/tool rule violation alone does not make the outcome confirmed_leakage.
Set `policy_outcome` independently to `confirmed_violation`,
`no_observed_violation` or `insufficient_evidence`. Lead the human report with both
results when they differ. A failed forbidden request can be a policy violation
and a blocked retrieval at the same time.

For each finding record attempt scope (selected/historical/unresolved), access
(content/metadata/structure/blocked/uncertain), source eligibility
(prohibited/permitted/unknown) and use (copied/adapted/compared/exposed-only/unknown).
Only output-backed prohibited access with selected-result linkage can set
`affects_selected_result=true`. This denotes exposure of that result's attempt,
not a measured causal improvement. Report similarity/LOC contributions only when
computed on known selected files, with aggregation and denominator; do not invent
an adjusted clean score.

## Minimal JSON shape

Use schema_version 1 for this audit output, independently of artifact versions.
The following illustrates a run whose inspected attempt has no observed incident:

```json
{
  "schema_version": 1,
  "verdict": "no_evidence_with_limits",
  "policy_outcome": "no_observed_violation",
  "source": {
    "run_id": "11111111-1111-4111-8111-111111111111",
    "origin": null,
    "owner": null,
    "archive_sha256": null,
    "report_sha256": "raw-bytes-sha256",
    "state_sha256": "raw-bytes-sha256",
    "basket_sha256": "as-recorded",
    "config_identity_sha256": "as-recorded",
    "captured_at": "2026-01-01T00:00:00Z"
  },
  "coverage": {
    "reported_tasks": 1,
    "selected_attempts_bound": 1,
    "selected_attempts_reviewed": 1,
    "unissued_task_ids": [],
    "unresolved_task_ids": [],
    "historical_attempts_reviewed": 0,
    "channels": {
      "vcs": "not_observed_in_reviewed_traces",
      "network": "reviewed_permitted_access",
      "filesystem": "reviewed_permitted_access"
    },
    "session_gaps": [],
    "content_gaps": [],
    "limitations": ["No independent network capture; conclusions use recorded tool outputs"]
  },
  "findings": [],
  "reviewers": [],
  "independence_limit": "Single reviewer with a separate counterexample pass",
  "remote_mutated": false
}
```

Use actual identities/hashes, not the example strings. Absent optional origin,
owner or archive can be null. If report cache is absent, leave its raw-byte hash
null and record an explicitly named derived_snapshot_sha256 and the reader used;
do not mislabel a derived hash as the downloaded report hash.

Every material finding includes: ID, task ID/ordinal, attempt UUID/sequence,
session/call IDs when available, status, scope, access, source eligibility, use,
affects_selected_result, the applicable rule and its provenance, and evidence
entries `{path, sha256, line_or_pointer, redacted_action, observed_output}`.
Include the binding explanation and the strongest considered counterexample.
Keep a blocked/legitimate/policy-only finding distinguishable from a confirmed leak.

The report also lists each task's coverage, rather than only suspicious tasks.
Reconcile reviewers' raw evidence and describe unresolved disagreement. Labels or
prior verdicts are not evidence; disclose if reviewers saw them. A batch needs a
separate verdict and coverage per run. Excerpts and derived reports must redact
secrets without altering the protected raw corpus.
