# PR Similarity Basket Collector

[PR Similarity](../README.md) / Basket Collector

Build datasets of real coding tasks from merged GitHub pull requests or GitLab
merge requests. Task statements come from linked issues or an LLM. The result,
`basket.json`, contains the tasks and frozen Git revisions needed to evaluate a
coding agent against the original changes.

## Prerequisites

- **Linux**, the supported platform.
- **Python 3.12**, **Make**, and **Git**
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

## Quick start

Every `make setup` upgrades pip in this module's `.venv` before installing the
pinned build and runtime dependencies. It does not update the system Python.
If `.venv` already exists, setup asks `Reinstall dependencies? [Y/n]`. Enter or
`Y` repeats installation in the existing environment; `n` leaves it unchanged.
End-of-input cancels setup with an error. Setup never deletes `.venv`.

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

## Configuration

[examples/full.yaml](examples/full.yaml) lists every collector setting, with
comments for GitHub/GitLab, date ranges/explicit changes and issue/LLM generation.
The [operations guide](docs/operations.md#choose-your-basket) explains selection,
filtering and resume. Copy an example within `examples/` to preserve relative
paths, and set distinct output/cache paths for your own basket.

Each focused example is a complete config with a real repository and its own output
and cache directories. Choose one, adjust its dataset identity and settings,
and pass its path through `CONFIG`.

| Config | Collection and filtering | Task generation / LLM key |
|---|---|---|
| [github-llm-diff.yaml](examples/github-llm-diff.yaml) | Werkzeug #3147; small Python fix, source path required; linked issue optional | Diff → GPT-4.1 mini, Responses, temperature 0; `OPENAI_API_KEY` |
| [github-llm-diff-issue.yaml](examples/github-llm-diff-issue.yaml) | Werkzeug #3147; linked issue required, multiple issues allowed | Diff + issues → GPT-5 mini, Responses, low reasoning; `OPENAI_API_KEY` |
| [github-llm-issue.yaml](examples/github-llm-issue.yaml) | Werkzeug #3147; exactly one linked issue | Issue → GPT-4.1 mini, Chat Completions, top_p 0.9; `OPENAI_API_KEY` |
| [github-date-range.yaml](examples/github-date-range.yaml) | Werkzeug, April 2026; size/path filters, percentile trimming, seeded sample of up to 5 tasks | Diff → GPT-4.1 mini, Chat Completions, temperature 0.2; `OPENAI_API_KEY` |
| [github-pandas-date-range.yaml](examples/github-pandas-date-range.yaml) | pandas, January 2026; filtered, seeded sample of up to 10 tasks | Diff → GPT-4.1 mini, Chat Completions; `OPENAI_API_KEY` |
| [gitlab-mailman-date-range.yaml](examples/gitlab-mailman-date-range.yaml) | Mailman Core, January–June 2026; filtered, seeded sample of up to 10 tasks | Diff → GPT-4.1 mini, Chat Completions; `OPENAI_API_KEY` |
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
Keep these paths valid if you copy an example elsewhere. Use the full example
as the field reference and a focused example for your first run.

## Results and resume

A successful export writes `basket.json` under the configured `output_dir`.
Keep its bytes unchanged when comparing agents. Other files record collection,
filtering, task generation, omissions and checkpoints.

Ctrl+C saves progress; repeat the same command to continue. Exit code 3 requires
inspection: an existing basket may be the previous export. Follow the
[operations guide](docs/operations.md) before selecting retry, partial-export,
reselection or overwrite flags.

<details>
<summary>Compatibility</summary>

Published baskets are read-only inputs and can be reused without running the
collector again. Checkpoints are working state: if they are incompatible with
an updated installation, start a new output directory and retain the original
files. See the [saved-data note](../README.md#reading-data-from-earlier-releases).

</details>

## Development

`make help` lists stage commands and variables. `make test` and `make smoke` are
offline; `make smoke-live` uses real services and may make model calls.
[AGENTS.md](AGENTS.md) describes module boundaries;
[prsim-build-basket](../.agents/skills/prsim-build-basket/SKILL.md) is the operator
skill. Validate exports with the collector's own models and packaged schema;
benchmark and viewer independently validate the same frozen export contract.


The source tree separates the collector's responsibilities:

- `models/` defines persisted artifacts and source identities.
- `providers/` contains GitHub/GitLab clients, source adapters and LLM access.
- `vcs/` owns local Git inspection and authentication.
- `persistence/` owns atomic files, publication and checkpoint validation.
- `runtime/` owns attempts, transport execution, progress and diagnostics.
- `stages/` contains collection, filtering, generation and export operations.

`pipeline.py` coordinates these components; `config.py`, `filters.py`,
`selection.py` and `export_validation.py` keep their corresponding shared rules.
Schemas and the dependency lock resource remain at the package root.
