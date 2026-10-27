[PR Similarity](../../README.md) / [basket_collector](../README.md)

# Resume and partial results

- [Choose your basket](#choose-your-basket)
- [Inspect the basket artifacts and explain missing tasks](#inspect-the-basket-artifacts-and-explain-missing-tasks)
- [Read a published basket](#read-a-published-basket)

Ctrl+C saves progress. Run the same command with the same `CONFIG` to continue.
Each record has its own retry budget. An exhausted record becomes `failed`;
other records continue. Fix the reported cause, then retry failed records:

```bash
make basket CONFIG=examples/github-llm-diff.yaml RETRY_FAILED=1
```

To proceed with known omissions, use `ALLOW_PARTIAL=1`. If recovered records
change the selected tasks, use `RESELECT=1` to accept the new selection:

```bash
make basket CONFIG=examples/github-llm-diff.yaml ALLOW_PARTIAL=1
make basket CONFIG=examples/github-llm-diff.yaml RETRY_FAILED=1 RESELECT=1
```

Successful generation is reused when its complete inputs match. The last
published basket stays available until a new export is ready. Deterministic
`rejected` records are not repeated by `RETRY_FAILED`.

To intentionally recalculate the whole basket:

```bash
make basket CONFIG=examples/github-llm-diff.yaml OVERWRITE=1
```

`OVERWRITE=1` recalculates the command's stages: all stages for `basket`, or the
named stage for a direct command. It cannot be combined with `RETRY_FAILED` or
`RESELECT`. Retries and recalculation may incur new model charges.

`RESELECT` applies to `filter` and `basket`; `RETRY_FAILED` applies to every
record stage and `basket`. An empty basket is never exported. Exit code 3 means
saved work needs a retry or an explicit partial/selection decision, or that no
successful records remain. Inspect the reported stage and saved counts before
choosing a flag. `ALLOW_PARTIAL` does not retry failures or accept a changed
selection; `RESELECT` does not accept omissions. If both decisions are needed,
pass both flags deliberately. `RETRY_FAILED` does not apply to `export`.

The presence of `basket.json` after exit code 3 does not prove this invocation
exported a new basket: it may be the previous publication. Inspect stage status,
`selection.json`, and the export's lineage, coverage and omissions. Keep the
previous export intact while deciding how to continue.

## Choose your basket

The workflow is `collect → filter → generate → export`. You can inspect each
stage before continuing; `basket.json` is the frozen export boundary.
For a first run, choose a shipped example rather than filling the whole schema
from scratch. To keep its relative paths valid while customizing it:

```bash
cp examples/github-date-range.yaml examples/my-basket.yaml
```

Edit that file's dataset name/maintainers, repository, dates, filters and output
locations. Give experiments separate output/cache directories. Never put token
values in the YAML. Read the config once without API/Git/LLM calls:

```bash
.venv/bin/python - <<'PY'
from basket_collector.config import load_config
config = load_config('examples/my-basket.yaml')
print('name:', config.dataset.name)
print('output:', config.output_dir)
PY
```

| Config area | Decision |
|---|---|
| `schema_version` | Keep the value from the provided example |
| `dataset` | Dataset identity, description and maintainers |
| `output_dir`, `cache_dir`, `env_file` | Config-relative output/cache/credential-file paths |
| `source` | GitHub/GitLab host/project and API token variable name |
| `collect` | Explicit changes or date-range population, default-branch policy and discovery ceiling |
| `filter.git` | Git timeout, depth and transfer-byte limits for revision verification |
| `filter.scope`, `.size`, `.files`, `.issues` | Which changes are eligible before sampling |
| `filter.percentile` | Trim size tails after eligibility filters |
| `filter.selection` | Seeded shuffle and maximum output size |
| `filter.workers` | Concurrent Git/filter work; discovery has its own API limits |
| `generation` | Issue text or LLM-generated task; input context, templates, concurrency and API parameters |
| `export.include_golden_diff` | Whether to include the human diff in the frozen export |

`collect`, `filter` and `generation` each default to `max_retries: 3`,
`retry_delay_seconds: 10` and `max_retry_delay_seconds: 120`. Retries use
exponential backoff and respect server cooldowns. Source API request pacing
stays under `source`; native Git timeout is `filter.git.timeout_seconds`.

The merged date interval is half-open UTC: `merged_from` is included,
`merged_before` is excluded. Discovery may scan closed-change history beyond
that interval before filtering. A `collect.max_changes` ceiling can truncate
the population; choose it high enough for the period rather than treating it as
random sampling. `selection.limit: 25` means **up to 25**, not a guarantee.
Eligibility, percentile trimming and generation failures can reduce the count.

`filter.files.min_source_proportion` counts changed files, not changed lines.
Use CodeBLEU-supported extensions if your downstream metric needs them. Decide
whether to require exactly one issue, allow multiple, or allow none based on
`generation.context`. `generation.source: issue` copies issue text without model
calls; LLM generation uses `issue`, `diff` or `diff+issue` according to the config.
The collector currently uses API access for LLM generation; the benchmark's
ChatGPT OAuth profile is not a collector login.

For baseline comparisons, freeze one basket and reuse its exact bytes for every
agent. Do not regenerate task statements between agents. The benchmark keeps
human-after data outside the agent's before-only workspace even when the export
contains a golden diff.

### Inspect the basket artifacts and explain missing tasks

| File under `output_dir` | What to inspect |
|---|---|
| `collected.json` | Successful records, exclusions and failed discovery identities |
| `filtered.json` | All eligible candidates, rejections and failures |
| `selection.json` | Last accepted ordered selection and generation input fingerprints |
| `tasks.json` | Selected task outcomes, including failures and generation rejections |
| `basket.json` | Last published export, with `meta.coverage` and explicit `meta.omissions` |
| `.work/<stage>.json` | Unfinished stage/checkpoint state; not a completed export |
| `logs_<timestamp>_*/collector.log` | Per-command DEBUG diagnostics, even at console INFO |

Each stage has `meta.status`, `meta.counts`, settings/hash and predecessor lineage.
Read those before assuming the next stage is complete. Changing settings,
including collector worker settings, can invalidate reuse; benchmark worker
count has different resume semantics. Do not edit saved hashes/checkpoints.
Completed record processing can still await permission to select or export.
Historical revisions and the currently published basket are retained separately.

From this directory, replace the path with your config's output directory:

```bash
.venv/bin/python - ./output/examples/github-date-range <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path
root = Path(sys.argv[1])
for name in ('collected.json', 'filtered.json', 'tasks.json', 'basket.json'):
    path = root / name
    if not path.exists():
        print(name, 'not completed/present')
        continue
    doc = json.loads(path.read_text())
    print(name, doc['meta']['status'], doc['meta']['counts'])
    if name == 'filtered.json':
        reasons = Counter(item['reasons'][0]['code']
                          for item in doc['rejected'] if item['reasons'])
        print('first rejection reasons:', dict(reasons))
    if name == 'basket.json':
        print('exported tasks:', len(doc['tasks']))
        print('coverage:', doc['meta']['coverage'])
        print('omissions:', doc['meta']['omissions'])
PY
```

First rejection reasons are sequential: they explain where records first dropped
out, not independent counts of every predicate. Compare all four stages to
separate filtering/sampling losses from LLM generation failures. A partial export
can be usable but should be reported with its actual coverage.

Ctrl+C saves checkpoints. Resume with the same CONFIG. If an interrupted record
used its last attempt, `RETRY_FAILED=1` explicitly opens a new budget. Preserve
the final export and its source config after validation.



## Read a published basket

Use the separate frozen-input API to validate an existing export without opening
or modifying its collector working directory:

```python
from pathlib import Path

from basket_collector.persistence.baskets import read_basket

basket = read_basket(Path('published-basket.json'))
print(len(basket.document.tasks))
print(basket.sha256)  # SHA-256 of the original bytes in basket.content.
```

`models.artifacts.BasketDocument` and `persistence.storage.read_document` remain
current-stage APIs. Use `read_basket` for historical published exports; they are
not checkpoint inputs and are never upgraded on read.
