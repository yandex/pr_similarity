# PR Similarity Basket Collector

[PR Similarity](../README.md) / Basket Collector

Build datasets of real coding tasks from merged GitHub pull requests or GitLab
merge requests. Task statements come from linked issues or an LLM. The result,
`basket.json`, contains the tasks and frozen Git revisions needed to evaluate a
coding agent against the original changes.

## Quick start

Before you start, you need:

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

## Continue or rebuild

Ctrl+C saves progress. Run the same command with the same `CONFIG` to continue.
For missing credentials, fix the environment and resume. For failed tasks,
fix the reported cause, then retry:

```bash
make basket CONFIG=examples/github-llm-diff.yaml RETRY_FAILED=1
```

After changing the config, rebuild:

```bash
make basket CONFIG=examples/github-llm-diff.yaml OVERWRITE=1
```

Retries may make new paid LLM requests; rebuilding repeats successful generation
as well. A run stops if a stage produces no usable changes. If only some tasks
fail, it can export a partial basket; inspect the output files before using it.

## Choose your basket

The workflow is `collect → filter → generate → export`. You can inspect each
stage before continuing; only the completed `basket.json` is the benchmark input.
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
| `dataset` | Dataset identity, description and maintainers |
| `output_dir`, `cache_dir`, `env_file` | Config-relative output/cache/credential-file paths |
| `source` | GitHub/GitLab host/project and API token variable name |
| `collect` | Explicit changes or date-range population, default-branch policy and discovery ceiling |
| `git` | Host Git timeout, depth and transfer-byte cap for revision verification |
| `filter.scope`, `.size`, `.files`, `.issues` | Which changes are eligible before sampling |
| `filter.percentile` | Trim size tails after eligibility filters |
| `filter.selection` | Seeded shuffle and maximum output size |
| `filter.workers` | Concurrent Git/filter work; discovery has its own API limits |
| `generation` | Issue text or LLM-generated task; input context, templates, concurrency and API parameters |
| `export.include_golden_diff` | Whether to include the human diff in the frozen export |

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
| `collected.json` | Discovered successful records, linked issues and collection exclusions |
| `filtered.json` | Retained records and `rejected[].reasons` |
| `tasks.json` | Generated tasks and `failed[]` records, attempts/errors |
| `basket.json` | Exported task statements, frozen revisions/workspaces/source descriptors |
| `.work/<stage>.json` | Unfinished stage/checkpoint state; not a completed export |
| `logs_<timestamp>_*/collector.log` | Per-command DEBUG diagnostics, even at console INFO |

Each stage has `meta.status`, `meta.counts`, settings/hash and predecessor lineage.
Read those before assuming the next stage is complete. Changing settings,
including collector worker settings, can invalidate reuse; benchmark worker
count has different resume semantics. Do not edit saved hashes/checkpoints.

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
PY
```

First rejection reasons are sequential: they explain where records first dropped
out, not independent counts of every predicate. Compare all four stages to
separate filtering/sampling losses from LLM generation failures. A partial export
can be usable but should be reported with its actual coverage.

Ctrl+C saves checkpoints. Resume with the same CONFIG. Fix credentials without
rebuilding successful work; retry failed generation deliberately with
`RETRY_FAILED=1`. For an intentional changed-config rebuild use `OVERWRITE=1`,
which can repeat paid requests, or choose a new output/cache path. Freeze and
retain the final export and its source config after validation.

</details>

## Development and assistant workflows

`make help` lists stage commands and variables. `make test` and `make smoke` are
offline; `make smoke-live` uses real services and may make model calls.
[AGENTS.md](AGENTS.md) describes module boundaries;
[prsim-build-basket](../.agents/skills/prsim-build-basket/SKILL.md) is the operator
skill. The [benchmark guide](../benchmark/README.md#quick-start) starts from the
completed export; it does not need intermediate collector files.
