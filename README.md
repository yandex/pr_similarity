# PR Similarity

Build coding tasks from merged GitHub pull requests or GitLab merge requests,
run OpenCode on their original source revisions, and compare its edits with the
human changes. Inspect scores, diffs and execution evidence locally, or publish
a run to the web leaderboard.

The collector exports schema v2 baskets. Benchmark and viewer read both v1 and v2;
existing frozen v1 baskets remain supported with an upgrade notice. Update both
consumers before publishing v2 runs to an older deployment. See the
[upgrade procedure](#upgrade-from-v01) and the
[collector guide](basket_collector/README.md#resume-and-partial-results).
Collector v1 checkpoints are not resumable in v2; start a new output when moving
that workflow to v2. Product release, basket format and agent config versions are
independent; do not change a frozen basket's version field by hand.

| Module | Use it to | Start here |
|---|---|---|
| [`basket_collector`](basket_collector/README.md) | Select changes and freeze task statements and Git revisions | [Prepare a basket](basket_collector/README.md#quick-start) |
| [`benchmark`](benchmark/README.md) | Run an agent, score edits and inspect or publish the evidence | [Prepare a run](benchmark/README.md#quick-start), [inspect artifacts](benchmark/README.md#find-your-results) |
| [`viewer`](viewer/README.md) | Browse task scores and Human/Agent diffs, or host a service | [Use the leaderboard](viewer/README.md#use-the-leaderboard), [host a viewer](viewer/README.md#persistent-linux-service) |

## First run

Start with the [Benchmark quick start](benchmark/README.md#quick-start) if you
already have a frozen basket. It covers setup, model access and your first run.
Use the [Basket Collector](basket_collector/README.md#quick-start) to prepare tasks
and the [Viewer deployment](viewer/README.md#persistent-linux-service) when you want to host shared results.

In v0.2, **Linux is the supported platform** for running the collector, benchmark
and viewer server. Other operating systems are outside the release's supported
scope. You can browse a deployed viewer from any operating system.

You need Python 3.12, Make and Git. Agent execution specifically requires
**Linux x64 with glibc**, working bubblewrap user namespaces, and model access
through an API key or ChatGPT subscription OAuth. Each module owns its `.venv`;
run Make from the module directory. The viewer accepts Python 3.12 or newer.

1. Choose an [included basket](benchmark/examples/baskets/README.md), prepare one
   in `basket_collector/`, or get an existing frozen `basket.json`.
2. Set up `benchmark/` and choose an agent config. For subscription access,
   complete `make auth` before the first run.
3. Run that config on any basket by changing `DATASET`. The config describes the
   agent; `OUTPUT` selects the parent directory for new runs; `RUN` and `RESUME`
   select one existing run directory.
4. Open the printed run directory in your IDE. Read `report.json`, live logs and
   source-role files, then publish when you intend to share the evidence.

After following the [Benchmark quick start](benchmark/README.md#quick-start)
and completing OAuth login, run the included pandas basket:

```bash
cd benchmark
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=examples/baskets/pandas/pandas-2026H1-25.json OUTPUT=./output
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
existing basket needs no collector rerun; local results need no viewer service.
The collector's LLM generation uses API credentials. Benchmark ChatGPT OAuth
does not authorize the collector, source Git access or leaderboard publication.

## Upgrade from v0.1

Upgrade **benchmark and viewer before the first v2 publication**. Updating source
alone is insufficient: benchmark's strict editable installation retains the old
package file inventory and can miss the new basket v2 schema. Run `make setup`
in every updated module, even when its `.venv` already exists. Keep frozen
baskets, saved runs and publication journals unchanged. Collector v1 checkpoints
need a new output directory; they cannot be resumed with the v2 collector.

1. Record the installed source revision and service CONFIG path. Finish or stop
   active benchmark/collector work, pause publishing, and stop the viewer using
   its actual supervisor. For the [Linux service](viewer/README.md#persistent-linux-service):

   ```sh
   sudo systemctl stop prsim-viewer
   sudo systemctl is-active prsim-viewer  # expect inactive (exit 3)
   ```

   For a `make start` deployment, instead run `make stop CONFIG=/absolute/config.json`
   from its viewer directory as the runtime owner. Do not use `make stop` for a
   systemd-managed foreground service. Confirm the old process has exited.

2. Preserve the same CONFIG, runtime, admin token, TLS and credential configuration.
   Take a private SQLite backup using the backup API, as the runtime owner (adapt
   these absolute paths to your installation). Do not copy a live database alone:

   ```sh
   umask 077
   python3.12 - <<'PYTHON'
   import sqlite3
   from pathlib import Path
   runtime = Path('/var/lib/pr-similarity-viewer')
   backup = runtime / 'backup-before-v0.2'
   backup.mkdir(mode=0o700, exist_ok=False)
   with sqlite3.connect(f'file:{runtime / "viewer.sqlite"}?mode=ro', uri=True) as src:
       with sqlite3.connect(backup / 'viewer.sqlite') as dst:
           src.backup(dst)
   PYTHON
   ```

   Also preserve the runtime's admin-token file in your protected backup; never
   regenerate it as part of an upgrade. Keep a matching object-store snapshot or
   versioned recovery point. See [storage and recovery](viewer/README.md#storage-backup-and-recovery)
   for the division between SQLite, original archives and S3 objects. Record the
   existing account and a published v1 run URL/hash for the post-upgrade check.

3. Update the source to the v0.2.0 release using your checkout or release archive
   workflow, retaining the existing runtime and operator configs outside it.
   Run setup as the owner of each installation, from the repository root:

   ```sh
   (cd basket_collector && make setup PYTHON=python3.12)
   (cd benchmark && make setup PYTHON=python3.12)
   (cd viewer && make setup PYTHON=python3.12)
   ```

   On separate hosts run the corresponding command in each deployed module. For
   the service below, run viewer setup as `prsim-viewer` in
   `/opt/pr-similarity/viewer`. A recreated venv also needs this setup; do not copy
   virtual environments between paths. Check installed versions and both consumer
   schemas before restarting (run from the repository root):

   ```sh
   for module in basket_collector benchmark viewer; do
     "$module/.venv/bin/python" -I -c 'import importlib.metadata as m, sys; v=m.version("pr-similarity-os-"+sys.argv[1].replace("_","-")); print(sys.argv[1], v); assert v == "0.2.0"' "$module"
   done
   benchmark/.venv/bin/python -I -c 'from benchmark.contracts import document_validator; [document_validator("basket", v) for v in (1, 2)]; print("benchmark basket v1/v2 OK")'
   viewer/.venv/bin/python -I -c 'from viewer.schema import validator; [validator("basket", v) for v in (1, 2)]; print("viewer basket v1/v2 OK")'
   ```

4. Start the viewer with its original CONFIG and runtime:
   `sudo systemctl start prsim-viewer` (or `make start CONFIG=/absolute/config.json`
   for the local launcher). Follow the [deployment acceptance check](viewer/README.md#deployment-acceptance):
   sign in with the existing account, open the existing v1 run and compare its
   original archive hash, then publish/read a v2 run. Check task/config views too.
   Resume writes only after these checks. A schema-load or health check alone
   does not verify account preservation or S3 publication.

For rollback, stop publishers and the viewer first; preserve the failed upgrade's
runtime and object-store state for diagnosis. Restore the previous source revision
and rerun `make setup` in each reverted module. If the old viewer cannot read the
new SQLite schema, restore the pre-upgrade SQLite backup while stopped, with the
original owner and private permissions. Move the current database **and its
`-wal`/`-shm` sidecars** aside together before installing the backup; do not combine
an old database with newer WAL files. Keep the original admin token. If writes
occurred after the backup, restore the corresponding S3 recovery point as well;
restoring SQLite alone can lose ownership/receipts for newer publications. Start
the old viewer with its old CONFIG, verify the old account and v1 publication,
then resume compatible publishers. A v1-only viewer must not receive v2 runs.

## Configure and investigate

- [Agent settings, setup, skills and rules](benchmark/README.md#configure-your-agent)
- [MCP, RAG CLI tools and bubblewrap limits](benchmark/README.md#connect-mcp-and-rag)
- [Run files, logs, scores and retries](benchmark/README.md#find-your-results)
- [Reprice and OpenAI pricing policies](benchmark/README.md#estimate-cost-with-reprice)
- [Authentication cleanup](benchmark/README.md#credentials-after-a-run)
- [Basket filters and stage artifacts](basket_collector/README.md#choose-your-basket)
- [Publication and troubleshooting](benchmark/README.md#publish-to-a-leaderboard)

The Model label comes from the frozen native config: `model [effort]` when
effort is known, otherwise just `model`. Configured and captured effective
origins are shown separately. Basket generator provenance is a
separate source; a model name in the basket label does not configure the agent.
Set user Setup tags once in agent YAML, using top-level `tags`; their strings,
order and duplicates stay intact. Local run labels stay frozen on resume.
Viewer owner edits are separate, and an overwrite preserves the server's name
and Setup tags. See the [label and inference guide](benchmark/README.md#configure-your-agent).

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

## Prepare a source release

From the repository root, export to a new directory outside this checkout:

```bash
python3.12 tools/prepare_release.py --source . --destination /path/to/pr-similarity-release
python3.12 tools/check_release.py /path/to/pr-similarity-release
```

The export contains the three modules, examples, tests, documentation, repository
skills and legal files, plus a manifest of the product release version, file
sizes and SHA-256 hashes. The checker verifies that all module versions match
the manifest. The export excludes environments, credentials, local configs,
caches and run outputs.
Existing destinations are refused. This release distributes this source
tree; wheels built by development checks are not separate release artifacts.
