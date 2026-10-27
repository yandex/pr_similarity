# PR Similarity

Build coding tasks from merged GitHub pull requests or GitLab merge requests,
run OpenCode on their original source revisions, and compare its edits with the
human changes. Inspect scores, diffs and execution evidence locally, or publish
a run to the web leaderboard.

| Module | Use it to | Start here |
|---|---|---|
| [`basket_collector`](basket_collector/README.md) | Select changes and freeze task statements and Git revisions | [Prepare a basket](basket_collector/README.md#quick-start) |
| [`benchmark`](benchmark/README.md) | Run an agent, score edits and inspect or publish the evidence | [Prepare a run](benchmark/README.md#quick-start), [inspect artifacts](benchmark/docs/results.md#find-your-results) |
| [`viewer`](viewer/README.md) | Browse task scores and Human/Agent diffs, or host a service | [Use the leaderboard](viewer/docs/leaderboard.md#use-the-leaderboard), [host a viewer](viewer/docs/deployment.md#persistent-linux-service) |

## First run

Start with the [Benchmark quick start](benchmark/README.md#quick-start) if you
already have a frozen basket. It covers setup, model access and your first run.
Use the [Basket Collector](basket_collector/README.md#quick-start) to prepare tasks
and the [Viewer deployment](viewer/docs/deployment.md#persistent-linux-service) when you want to host shared results.

**Linux is the supported platform** for running the collector, benchmark
and viewer server. You can browse a deployed viewer from any operating system.

You need Python 3.12, Make and Git. Agent execution specifically requires
**Linux x64 with glibc**, working bubblewrap user namespaces, and model access
through an API key or ChatGPT subscription OAuth. Each module owns its `.venv`;
run Make from the module directory. Agent attempts get a separate clean Python
environment based on the benchmark setup interpreter; see the
[Python environment contract](benchmark/docs/python.md#python-inside-an-attempt). The viewer accepts Python 3.12 or newer.

1. Prepare a basket in `basket_collector/`, or choose a published frozen
   basket from the [catalog](benchmark/examples/baskets/README.md).
   Existing baskets retain their original bytes and hashes.
2. Set up `benchmark/` and choose an agent config. For subscription access,
   complete `make auth` before the first run.
3. Run that config on any basket by changing `DATASET`. The config describes the
   agent; `OUTPUT` selects the parent directory for new runs; `RUN` and `RESUME`
   select one existing run directory.
4. Open the printed run directory in your IDE. Read `report.json`, live logs and
   source-role files, then publish when you intend to share the evidence.

After following the [Benchmark quick start](benchmark/README.md#quick-start)
and completing OAuth login, run your newly exported basket:

```bash
cd benchmark
make run CONFIG=examples/full.yaml DATASET=/path/to/new-export/basket.json OUTPUT=./output
```

The command prints the new run path. Use that exact path for viewing, resume and
publication; the output directory itself is not a run.

```text
GitHub / GitLab → basket_collector → basket.json
                                      ↓
                                  benchmark → output/<time>-<agent>-<suffix>/report.json
                                                     ↓
                                       IDE files or HTTPS publish → viewer
```

The modules communicate through files and the viewer's publication API. An
existing supported basket needs no collector rerun; local results need no viewer service.
The collector's LLM generation uses API credentials. Benchmark ChatGPT OAuth
does not authorize the collector, source Git access or leaderboard publication.

## Configuration examples

All application configuration examples live under their module's `examples/` directory. Start with
a complete reference, or choose a focused recipe linked from the module README.
These are separate configurations for three separate programs.

| Module | Complete example | Guide |
|---|---|---|
| Collector | [full.yaml](basket_collector/examples/full.yaml) | [Selection and generation](basket_collector/docs/operations.md#choose-your-basket) |
| Benchmark | [full.yaml](benchmark/examples/full.yaml) — GPT-6.1 Sol medium, ChatGPT OAuth, `baseline` tag | [Agent configuration](benchmark/docs/configuration.md) |
| Viewer | [full.json](viewer/examples/full.json) | [Server settings and limits](viewer/docs/configuration.md) |

Collector/benchmark YAML paths resolve from the config file; viewer paths resolve
from the process working directory. Make paths are relative to the module directory.
Keep private copies and credentials out of version control. The installed collector
and benchmark CLIs default to a local `config.yaml`; the source-tree Make interface
uses `examples/full.yaml`. Pass `CONFIG` explicitly when selecting another recipe.

## Updating an installation

Run `make setup` in each updated module to install its dependencies and refresh
its package inventory. Existing environments prompt `[Y/n]`; Enter/Y reinstalls,
n leaves them unchanged, and closed stdin aborts. Setup upgrades pip and never
deletes the environment automatically. Keep benchmark and viewer installations
in sync, and back up persistent SQLite state before a service upgrade as
described in the [deployment guide](viewer/docs/deployment.md).

<a id="reading-data-from-earlier-releases"></a>
<details>
<summary>Compatibility with saved data</summary>

Published baskets and supported viewer archives remain readable without rewriting
their contents. A reader may warn when opening older data. Reading does not imply
that work can be continued: incompatible runs cannot be resumed, repriced or
rescored, and incompatible collector checkpoints need a new output directory.
Keep original files and use the producing installation when a reader rejects them.
See the [changelog](CHANGELOG.md) for the compatibility history.

</details>

## Configure and investigate

- [Agent settings, setup, skills and rules](benchmark/docs/configuration.md#configure-your-agent)
- [MCP, RAG CLI tools and bubblewrap limits](benchmark/docs/integrations.md#connect-mcp-and-rag)
- [Run files, logs, scores and retries](benchmark/docs/results.md#find-your-results)
- [Reprice and OpenAI pricing policies](benchmark/docs/pricing.md#estimate-cost-with-reprice)
- [Authentication cleanup](benchmark/docs/credentials.md#credentials-after-a-run)
- [Basket filters and stage artifacts](basket_collector/docs/operations.md#choose-your-basket)
- [Publication and troubleshooting](benchmark/docs/publication.md#publish-to-a-leaderboard)

The Model label comes from the frozen native config: `model [effort]` when
effort is known, otherwise just `model`. Configured and captured effective
origins are shown separately. Basket generator provenance is a
separate source; a model name in the basket label does not configure the agent.
Set user Setup tags once in agent YAML, using top-level `tags`; their strings,
order and duplicates stay intact. Local run labels stay frozen on resume.
Viewer owner edits are separate, and an overwrite preserves the server's name
and Setup tags. See the [label and inference guide](benchmark/docs/configuration.md#configure-your-agent).

## Work with a coding assistant

[AGENTS.md](AGENTS.md) describes repository boundaries and checks;
[CLAUDE.md](CLAUDE.md) imports it for Claude. Each module has a short local guide.
Repository skills in `.agents/skills/` cover four operator workflows:

| Skill | Purpose |
|---|---|
| [prsim-build-basket](.agents/skills/prsim-build-basket/SKILL.md) | Build/resume a frozen basket and explain rejected or failed tasks |
| [prsim-run-benchmark](.agents/skills/prsim-run-benchmark/SKILL.md) | Configure access and agent resources, then run or resume |
| [prsim-analyze-run](.agents/skills/prsim-analyze-run/SKILL.md) | Follow report references to inspect a task, diff, score and execution evidence |
| [prsim-publish-run](.agents/skills/prsim-publish-run/SKILL.md) | Validate an archive, publish an authorized run and verify the receipt |

These skills operate the benchmark repository. To customize the agent being
evaluated, declare its own skills/rules in the benchmark config. See each
module's `make help` and README for development commands. Keep `.env`, private
OAuth profiles, local configs, caches and generated runs out of commits.

## License and contributions

The repository is licensed under [Apache 2.0](LICENSE), with third-party notices
in [NOTICES](NOTICES). The included Superpowers skill retains its own MIT license.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the CLA and development checks.

<details>
<summary>Prepare a source release</summary>

From the repository root, check the release tools with
`python3.12 -m unittest discover -s tools/tests`. Export to a new directory
outside this checkout:

```bash
python3.12 tools/prepare_release.py --source . --destination /path/to/pr-similarity-release
python3.12 tools/check_release.py /path/to/pr-similarity-release
```

The export contains the three modules, examples, tests, documentation, repository
skills and legal files, plus a manifest of the product release version, file
sizes and SHA-256 hashes. The checker verifies that all module versions match
the manifest. Public module guides in `docs/` and all configuration examples
are part of the same inventory. The export excludes environments, credentials, local configs,
caches and run outputs.
Existing destinations are refused. This release distributes this source
tree; wheels built by development checks are not separate release artifacts.

The exported directory is a complete standalone source distribution, ready to
use as the Git repository contents without further source changes. Its generated
`release-manifest.json` records the product version, file hashes, sizes and
executable flags. Keep the manifest with the distribution to verify its contents.
Release notes are in [CHANGELOG.md](CHANGELOG.md#030).

</details>
