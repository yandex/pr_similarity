# PR Similarity Basket Collector

[PR Similarity](../README.md) / Basket Collector

Build datasets of real coding tasks from merged GitHub pull requests or GitLab
merge requests. Task statements come from linked issues or an LLM. The result,
`basket.json`, contains the tasks and frozen Git revisions needed to evaluate a
coding agent against the original changes.

The collector uses **schema v2** for configuration and artifacts. When moving
from v1, start with a v2 config and a new output directory; v1 stage checkpoints
cannot be resumed by this collector. Keep existing frozen baskets unchanged.
Benchmark and viewer accept these exports and retain support for frozen v1
baskets; the collector itself only accepts v2.

## Quick start

Before you start, you need:

- **Linux**, the supported platform for the collector in v0.2. Other operating
  systems are outside this release's supported scope.
- **Python 3.12** (the current package requires this version), **Make**, and **Git**
  installed and available on `PATH`.
- **Access to the source repository** through both its hosting API and Git.
  For private repositories, your credentials must have project read access.
  Configure Git authentication on the host with a credential helper or askpass;
  the collector cannot answer interactive password prompts.
- A **GitHub token** (`GITHUB_TOKEN`) or **GitLab token** (`GITLAB_TOKEN`).
  GitHub needs it to discover linked issues; GitLab requires API read access,
  including merge-request diff versions, even for public projects.
- For LLM examples, an API key and access to the configured model.
  LLM requests may incur provider charges.
- Network access to the repository host and, for LLM examples, the LLM endpoint.

Run these commands from this directory:

```bash
make setup PYTHON=python3.12
make env
```

If your `python` is already version 3.12, `make setup` is enough. Add the tokens
and API key you need to the generated `.env` file or your environment. Keep
credential values out of YAML configs and version control. API tokens do not
configure Git authentication.

Start with [examples/github-llm-diff.yaml](examples/github-llm-diff.yaml): it
selects one merged Werkzeug fix and generates a task from its diff. Set
`dataset.maintainers` to your login and provide `GITHUB_TOKEN` and
`OPENAI_API_KEY`, then run:

```bash
make basket CONFIG=examples/github-llm-diff.yaml
```

Your basket is saved to `output/examples/github-llm-diff/basket.json`.
The other files in that directory show collection results, rejected changes,
failed tasks, and logs.

## Example configurations

Each example is a complete config with a real repository and its own output
and cache directories. Choose one, adjust its dataset identity and settings,
and pass its path through `CONFIG`.

| Config | Collection and filtering | Task generation / LLM key |
|---|---|---|
| [github-llm-diff.yaml](examples/github-llm-diff.yaml) | Werkzeug #3147; small Python fix, source path required; linked issue optional | Diff → GPT-4.1 mini, Responses, temperature 0; `OPENAI_API_KEY` |
| [github-llm-diff-issue.yaml](examples/github-llm-diff-issue.yaml) | Werkzeug #3147; linked issue required, multiple issues allowed | Diff + issues → GPT-5 mini, Responses, low reasoning; `OPENAI_API_KEY` |
| [github-llm-issue.yaml](examples/github-llm-issue.yaml) | Werkzeug #3147; exactly one linked issue | Issue → GPT-4.1 mini, Chat Completions, top_p 0.9; `OPENAI_API_KEY` |
| [github-date-range.yaml](examples/github-date-range.yaml) | Werkzeug, April 2026; size/path filters, percentile trimming, seeded sample of up to 5 tasks | Diff → GPT-4.1 mini, Chat Completions, temperature 0.2; `OPENAI_API_KEY` |
| [gitlab-llm-diff.yaml](examples/gitlab-llm-diff.yaml) | GitLab Runner !7494; one Go test fix under a specific directory | Diff → GPT-4.1 mini, Responses; `OPENAI_API_KEY` |
| [github-issue.yaml](examples/github-issue.yaml) | Werkzeug #3147; exactly one linked issue | Copy the issue text; no LLM key or calls |

GitHub examples need `GITHUB_TOKEN`; the GitLab example needs `GITLAB_TOKEN`.
The selected changes are [Werkzeug #3147](https://github.com/pallets/werkzeug/pull/3147)
and [GitLab Runner !7494](https://gitlab.com/gitlab-org/gitlab-runner/-/merge_requests/7494).

LLM settings belong inside `generation.llm`. Check model access before running;
if you change providers, choose a model supporting strict JSON Schema output.
See the model documentation for [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
and [GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini).

Paths inside a config are relative to its directory. The examples use `../.env`
and `../prompts/` to share this directory's credentials and prompt templates.
Keep these paths valid if you copy an example elsewhere. The existing
[config.yaml](config.yaml) is a generic template for your own repository;
it uses linked issue text by default.

## Resume and partial results

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
| `schema_version: 2` | Required configuration version |
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

<details>
<summary>Inspect the basket artifacts and explain missing tasks</summary>

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

</details>

## Development and assistant workflows

`make help` lists stage commands and variables. `make test` and `make smoke` are
offline; `make smoke-live` uses real services and may make model calls.
[AGENTS.md](AGENTS.md) describes module boundaries;
[prsim-build-basket](../.agents/skills/prsim-build-basket/SKILL.md) is the operator
skill. Validate v2 exports with the collector's own models and packaged schema;
benchmark and viewer independently validate the same frozen export contract.
