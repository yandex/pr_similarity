[PR Similarity](../../README.md) / [benchmark](../README.md)

# Estimate cost with reprice

`reprice` recalculates saved request usage without contacting the model. It stores
the policy, usage inputs, calculation result and receipt, then selects the estimate
for each attempt. Original OpenCode-reported costs remain separate.
For a ChatGPT subscription run, this is an **API-equivalent estimate**, not the
subscription bill or a measured share of its monthly price.

Use the complete [GPT-6.1 Sol policy](../examples/pricing.openai-gpt-6.1-sol.yaml):

```bash
make reprice RUN=./output/RUN_ID PRICING=examples/pricing.openai-gpt-6.1-sol.yaml
```

For a fresh run or resume with automatic pricing:

```bash
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=/path/to/basket.json REPRICE=1 PRICING=examples/pricing.openai-gpt-6.1-sol.yaml
```

Choose the policy matching the model actually used; that command alone does not
change the agent model. The OAuth baseline already uses `gpt-6.1-sol`; when
selecting another model, update its config and pricing policy together. `PRICING` is a Make/CLI argument, not
an agent YAML field. `PRICING` on run requires `REPRICE=1`.

### Create an OpenAI policy and verify coverage

A policy needs `schema_version`, an audit-friendly `id` and `source`, `currency:
USD`, a `service_tier` label, and exact provider/model rows. The schema is
[pricing policy schema](../src/schemas/benchmark-pricing-v1.schema.json).
Record the date/tier in the policy ID/comments and link the source. Check current
rates when making a new policy; saved policies do not update themselves.

The example records **Standard API rates checked 2026-10-08**. At up to 272,000
input-context tokens, USD per million tokens is 2.00 input, 0.10 cache read, 2.50
cache write and 10.00 output. Above that threshold, the whole request uses 4.00,
0.20, 5.00 and 15.00 respectively. Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
and the [September 29 model release](https://developers.openai.com/api/docs/changelog).
This is one tier and date, not a universal price for any OpenAI endpoint/account.

```yaml
schema_version: 1
id: openai-standard-gpt-6.1-sol-2026-10-08
source: https://developers.openai.com/api/docs/pricing
currency: USD
service_tier: standard
models:
  - provider: openai
    model: gpt-6.1-sol
    rates:
      input: '2.00'
      cache_read: '0.10'
      cache_write: '2.50'
      output: '10.00'
    long_context:
      threshold: 272000
      rates:
        input: '4.00'
        cache_read: '0.20'
        cache_write: '5.00'
        output: '15.00'
```

Rates are **quoted decimal strings per 1M tokens**, not floats, cents, or prices
per token. All four categories must be present; use `null` only for a genuinely
unknown rate, not an unpriced category that you want treated as free. A zero
category count contributes zero even with a null rate; a nonzero count with null
rate creates a gap. Models without a documented long-context tariff use
`long_context: null`. Model/provider matching is exact: aliases, dated model IDs
and delegated models need their own rows. No alias or tier resolution is implicit.
`service_tier` records the operator's chosen basis; it does not force a runtime
request tier or automatically verify which tier was billed.

The pinned OpenCode adapter exports disjoint input/cache categories. Billing
output includes both native output and reasoning tokens. The context threshold
uses input + cache read + cache write, with **strictly greater than** semantics:
272,000 is short context, 272,001 uses long rates for the entire request. Do not
add reasoning output twice or price only the tokens above the threshold.

Inspect actual billing pairs and validate a policy offline from `benchmark/`:

```bash
.venv/bin/python - ./output/RUN_ID examples/pricing.openai-gpt-6.1-sol.yaml <<'PY'
import json
import sys
from pathlib import Path
from benchmark.models.base import ArtifactReceipt
from benchmark.persistence.artifact_refs import read_reference_json
from benchmark.pricing.policy import load_policy
from benchmark.persistence.run_store import RunStore
run, policy_path = map(Path, sys.argv[1:])
policy = load_policy(policy_path)
covered = {(r.provider, r.model) for r in policy.data.models}
seen = set()
store = RunStore.open(run)
for task in store.state.tasks:
    owners = [*task.history, task] if task.attempt is not None else task.history
    for owner in owners:
        if owner.seal is None:
            continue
        native = read_reference_json(run, owner.seal)
        usage = native['task']['telemetry']['usage_ledger']
        if usage is None:
            continue
        owned = {r['path']: r for r in native['artifact_receipts']}
        if usage['path'] not in owned:
            continue
        ledger = read_reference_json(run, ArtifactReceipt.model_validate(owned[usage['path']]))
        seen.update((r['provider'], r['model']) for r in ledger['requests'])
print('policy:', policy.data.id, 'sha256:', policy.sha256)
print('recorded pairs:', sorted(seen))
print('missing policy rows:', sorted(seen - covered))
PY
```

No missing rows means model coverage, not complete usage. Incomplete requests,
unknown counters/context or missing rates still prevent a full estimate. Read
`report.costs.final_attempts` and `report.costs.all_attempts`: each separates
`native_reported` from `api_equivalent`, with a nullable `amount`, lossless
`known_subtotal`, USD currency and attempt/request `coverage`. A newer attempt
without a selected price creates a coverage gap even if an older reprice exists.
No accepted pricing is `not_requested`; no native attempt is `not_started`.
A known subtotal is only the measured/priced part, never a complete unknown total.

`operations/reprice/<uuid>/` stores `request.json`, `policy.yaml`, `calculator/`,
`input_state.json`, `input_report.json`, `result.json` and `receipt.json`.
The full result includes all state-registered attempts and lossless Decimal
subtotals; final task rows retain only their selected attempt estimate. Raw
native cost and usage stay unchanged. Equivalent policy/usage/calculator inputs
create an immutable no-op record without changing accepted state/report.
Pricing gaps return nonzero while retaining saved native outcomes and scores.

If a publication is already pending, recovering it sends its original archived
report bytes even after a local reprice. Confirm that pending upload first, then
publish the updated report as a separately authorized overwrite. Do not delete
pending archives or edit the journal to change the in-flight upload's contents.
