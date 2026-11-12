# PR Similarity

Build coding tasks from merged GitHub pull requests or GitLab merge requests,
run OpenCode on their original source revisions, and compare its edits with the
human changes. Inspect scores, diffs and execution evidence locally, or publish
a run to the web leaderboard.

## First run

Run one task from a bundled basket with the default ChatGPT OAuth configuration.
You need Linux x64 with glibc, Python 3.12, Make, Git, Bash and working bubblewrap
user namespaces. The [benchmark prerequisites](benchmark/README.md#prerequisites)
cover host setup.

From the repository root:

```bash
cd benchmark
make setup PYTHON=python3.12
make env
make auth CONFIG=examples/full.yaml AUTH_FLOW=headless
```

Complete the login in your browser.

> [!NOTE]
> Runs require model access and consume subscription quota. For API-key access,
> follow the [benchmark quick start](benchmark/README.md#quick-start); API calls may incur charges.

Start the task:

```bash
make run CONFIG=examples/full.yaml DATASET=examples/baskets/pandas/pandas-2026H1-1.json OUTPUT=./output
```

> [!NOTE]
> The default agent uses GPT-6.1 Sol with medium reasoning and the `baseline` tag.

The command prints a run directory. Use that exact path to open the results:

```bash
make visualize RUN=./output/RUN_ID
```

Replace `RUN_ID` with the printed directory name, then open its `report.html`.
Use **Diff / Full file** to inspect the human and agent changes. The report works
offline. Scores measure similarity to human edits; they do not prove that tests pass.

## Choose your workflow

| Module | Use it to | Start here |
|---|---|---|
| [`basket_collector`](basket_collector/README.md) | Select changes and freeze task statements and Git revisions | [Prepare a basket](basket_collector/README.md#quick-start) |
| [`benchmark`](benchmark/README.md) | Run an agent, score edits and inspect or publish the evidence | [Prepare a run](benchmark/README.md#quick-start), [inspect artifacts](benchmark/docs/results.md#find-your-results) |
| [`viewer`](viewer/README.md) | Browse task scores and Human/Agent diffs, or host a service | [Use the leaderboard](viewer/docs/leaderboard.md#use-the-leaderboard), [host a viewer](viewer/docs/deployment.md#persistent-linux-service) |

A frozen basket can be reused across agents. Create your own with the collector,
or [publish a completed run](benchmark/docs/publication.md#publish-to-a-leaderboard)
when you want to share results through a viewer.

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

## Configure and investigate

- [Agent settings, setup, skills and rules](benchmark/docs/configuration.md#configure-your-agent)
- [MCP, RAG CLI tools and bubblewrap limits](benchmark/docs/integrations.md#connect-mcp-and-rag)
- [Run files, logs, scores and retries](benchmark/docs/results.md#find-your-results)
- [Reprice and OpenAI pricing policies](benchmark/docs/pricing.md#estimate-cost-with-reprice)
- [Authentication cleanup](benchmark/docs/credentials.md#credentials-after-a-run)
- [Basket filters and stage artifacts](basket_collector/docs/operations.md#choose-your-basket)
- [Publication and troubleshooting](benchmark/docs/publication.md#publish-to-a-leaderboard)

## Work with a coding assistant

[AGENTS.md](AGENTS.md) describes repository boundaries and checks;
[CLAUDE.md](CLAUDE.md) imports it for Claude. Each module has a short local guide.
Repository skills in `.agents/skills/` cover these operator workflows:

| Skill | Purpose |
|---|---|
| [prsim-build-basket](.agents/skills/prsim-build-basket/SKILL.md) | Build/resume a frozen basket and explain rejected or failed tasks |
| [prsim-run-benchmark](.agents/skills/prsim-run-benchmark/SKILL.md) | Configure access and agent resources, then run or resume |
| [prsim-analyze-run](.agents/skills/prsim-analyze-run/SKILL.md) | Follow report references to inspect a task, diff, score and execution evidence |
| [prsim-publish-run](.agents/skills/prsim-publish-run/SKILL.md) | Validate an archive, publish an authorized run and verify the receipt |
| [prsim-cheating-audit](.agents/skills/prsim-cheating-audit/SKILL.md) | Audit recorded VCS/network activity and prohibited answer access with attempt-bound evidence |

These skills operate the benchmark repository. To customize the agent being
evaluated, declare its own skills/rules in the benchmark config. See each
module's `make help` and README for development commands. Keep `.env`, private
OAuth profiles, local configs, caches and generated runs out of commits.

## License and contributions

The repository is licensed under [Apache 2.0](LICENSE), with third-party notices
in [NOTICES](NOTICES). The included Superpowers skill retains its own MIT license.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the CLA and development checks.

## Advanced topics

<a id="updating-an-installation"></a>
<details>
<summary>Updating an installation</summary>

Run `make setup` in each updated module to install its dependencies and refresh
its package inventory. Existing environments prompt `[Y/n]`; Enter/Y reinstalls,
n leaves them unchanged, and closed stdin aborts. Setup upgrades pip and never
deletes the environment automatically. Keep benchmark and viewer installations
in sync, and back up persistent SQLite state before a service upgrade as
described in the [deployment guide](viewer/docs/deployment.md).

</details>

<details>
<summary>Model labels and saved metadata</summary>

The Model label comes from the frozen native config: `model [effort]` when
effort is known, otherwise just `model`. Configured and captured effective
origins are shown separately. Basket generator provenance is a
separate source; a model name in the basket label does not configure the agent.
Set user Setup tags once in agent YAML, using top-level `tags`; their strings,
order and duplicates stay intact. Local run labels stay frozen on resume.
Viewer owner edits are separate, and an overwrite preserves the server's name
and Setup tags. See the [label and inference guide](benchmark/docs/configuration.md#configure-your-agent).

</details>

<a id="reading-data-from-earlier-releases"></a>
<details>
<summary>Compatibility with saved data</summary>

Published baskets and supported viewer archives remain readable without rewriting
their contents. A reader may warn when opening older data. Reading does not imply
that work can be continued: incompatible runs cannot be resumed, repriced or
rescored, and incompatible collector checkpoints need a new output directory.
Keep original files and use the producing installation when a reader rejects them.
For 0.3.5, update benchmark and viewer together. Existing viewer configurations
without `publication_storage` retain legacy mode. Before selecting `local`, stop
the service, preserve its runtime and complete the explicit
[legacy import](viewer/docs/reference.md#upgrading-an-existing-viewer).
Keep the account database, original archives, run UUIDs and receipts. Replacing
source files alone does not migrate stored user state. Pending publications from
a legacy client must be recovered with that producing installation; new clients
require `local-durable-v1` and never relabel an old request as locally durable.

See the [changelog](CHANGELOG.md) for the compatibility history.

</details>

<a id="maintain-the-run-page-interface"></a>
<details>
<summary>Maintain the shared run-page interface</summary>

Viewer static resources own the hosted and offline run-page design. After changing
that interface, shared metric/file projections or viewer display defaults, run:

```bash
python3.12 tools/sync_run_page.py
python3.12 tools/sync_run_page.py --check
```

This refreshes the benchmark's packaged copies. Its installed CLI remains
independent of viewer. Source-release checks reject drift between the two copies.
Run the owning module tests and the viewer's producer/local-page parity test after
changes; identical styling does not replace checks of selected metrics and evidence.

</details>

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
Release notes are in [CHANGELOG.md](CHANGELOG.md#0353).

</details>
