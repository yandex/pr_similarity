# PR Similarity Benchmark

[PR Similarity](../README.md) / Benchmark

Run OpenCode on frozen GitHub/GitLab coding tasks and compare its changes with
human solutions. Each run saves quality scores, token usage, diffs and logs;
you can view the results locally or publish them to a compatible viewer.

## Quick start

You need Linux x64 with glibc, Python 3.12, Make, Git, Bash and working
bubblewrap user namespaces. See [prerequisites](#prerequisites) for host setup.
Run from `benchmark/`:

```bash
make setup PYTHON=python3.12
make env
```

> [!WARNING]
> API-key runs may incur provider charges. OAuth runs consume subscription quota.
> Keep credentials out of YAML and version control. Model access, source Git
> access and viewer login use separate credentials.

### Choose model access

For a **ChatGPT subscription**, log in with the default configuration:

```bash
make auth CONFIG=examples/full.yaml AUTH_FLOW=headless
```

Complete the printed login flow in your browser. Then run one bundled task:

```bash
make run CONFIG=examples/full.yaml DATASET=examples/baskets/pandas/pandas-2026H1-1.json OUTPUT=./output
```

For an **API key**, put `OPENAI_API_KEY` in `.env` and choose an accessible model
in [examples/openai-api-key.yaml](examples/openai-api-key.yaml):

```bash
make run CONFIG=examples/openai-api-key.yaml DATASET=examples/baskets/pandas/pandas-2026H1-1.json OUTPUT=./output
```

### Select tasks and retain the run path

`DATASET` selects the tasks. Replace the example path with your frozen basket,
or choose another size from the [basket catalog](examples/baskets/README.md).
The same agent configuration works with different baskets.

> [!NOTE]
> Save the run directory printed by the command. `OUTPUT` is its parent directory;
> `RUN` and `RESUME` refer to one complete run. Replace `RUN_ID` in examples below
> with the printed directory name.

Ctrl+C saves progress and prints a resume command. Keep the original basket and
agent inputs when resuming.

## Results

Open `report.json` for the saved results, or create a browser report:

```bash
make visualize RUN=./output/RUN_ID
```

Open the generated `report.html`.
The header shows the generating benchmark version and `Local offline report`.
File dialogs open unified diffs; **Diff / Full file** switches both Human and Agent
panels to full source with highlighted changes. Both views work offline.
Add `OPEN_BROWSER=1` to open it in a browser on a desktop machine.
See [results and logs](docs/results.md), [resume and retry](docs/reference.md#resume-retries-progress-and-logging),
[pricing](docs/pricing.md) and [publication](docs/publication.md).
Scores measure similarity to human edits, not the project's test pass rate.

## Configuration

[examples/full.yaml](examples/full.yaml) is the complete harness example:
**GPT-6.1 Sol, medium effort, ChatGPT OAuth, `baseline` tag**. It lists every
harness field, including disabled optional features and API-key alternatives.
Use it as the default baseline; authenticate once with `make auth`.
Native OpenCode integrations have their own fields; see the
[configuration guide](docs/configuration.md) and [MCP/RAG guide](docs/integrations.md).

## Example configurations

| Config | Access | What to configure |
|---|---|---|
| [openai-api-key.yaml](examples/openai-api-key.yaml) | OpenAI API, native SDK | `OPENAI_API_KEY` and an available model |
| [chatgpt-oauth.yaml](examples/chatgpt-oauth.yaml) | ChatGPT subscription OAuth | Short baseline recipe; login with `make auth CONFIG=examples/chatgpt-oauth.yaml` |
| [openai-endpoint.yaml](examples/openai-endpoint.yaml) | Custom OpenAI endpoint, native SDK | Replace the example baseURL; set `OPENAI_API_KEY` and the model |
| [openai-tdd.yaml](examples/openai-tdd.yaml) | OpenAI API with the Superpowers TDD skill | `OPENAI_API_KEY` and the model; skill and agent instructions are included locally |
| [custom-tools.yaml](examples/custom-tools.yaml) | ChatGPT OAuth with a setup-installed documentation search CLI | Login with this CONFIG; setup script and rules are included locally |

Examples use one worker and a 30-minute agent timeout. They enable automatic
retry only for infrastructure failures within a one-retry budget; agent outcome
retries are opt-in. YAML paths resolve relative to the config file, `~` to your
home, and Make paths relative to this directory. `.env` is loaded from this
directory regardless of CONFIG location.

The TDD example customizes the agent through native `skills.paths` and
`instructions`. Its pinned [Superpowers skill](https://github.com/obra/superpowers/tree/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/test-driven-development)
follows test-first development for behavior changes. Use it with the same basket:

```bash
make run CONFIG=examples/openai-tdd.yaml DATASET=/path/to/basket.json
```

## Further reading

Start with [setup and login](#quick-start), then use the guide for your next task:

- [Configure setup, skills, rules and exclusions](docs/configuration.md#configure-your-agent)
- [Connect MCP or a RAG CLI in bubblewrap](docs/integrations.md#connect-mcp-and-rag)
- [Find a task's logs, diff and scores](docs/results.md#find-your-results)
- [Estimate cost with a pricing policy](docs/pricing.md#estimate-cost-with-reprice)
- [Check and remove reusable credentials](docs/credentials.md#credentials-after-a-run)
- [Publish and diagnose upload failures](docs/publication.md#publish-to-a-leaderboard)

## Development

`make help` lists commands, parameters and examples without setup or credentials.
See the [runtime and format reference](docs/reference.md) and the
[Python environment contract](docs/python.md). Tests and smoke require GCC/G++
for native compilation checks inside the sandbox.

```bash
make test
make smoke
```

These checks are offline; smoke also builds and installs a wheel outside the
checkout. Similarity and resource identity intentionally ignore executable
bits; chmod-only tasks are outside the metric scope.
## Advanced topics

<details>
<summary>Prerequisites and setup behavior</summary>

## Prerequisites

- **Linux x64 with glibc**, **Python 3.12** with venv/pip support, **GNU Make**,
  **Git** and **Bash**. Other platforms are outside the current runtime adapter.
- **Bubblewrap** (`bwrap`) with working unprivileged user namespaces. The runner
  checks the sandbox before starting agents. Containers and restricted hosts
  must allow user namespaces; installing the binary alone is insufficient.
  If the installed `bwrap` lacks required features, the runner can use its verified
  cached build when automatic installation is enabled.
- Temporary disk space for one private copy of the standard command directories
  per invocation, plus each attempt's source tree and venv. See the
  [Python environment contract](docs/python.md).
- Network access to **PyPI**, **GitHub** (dependencies and the pinned OpenCode
  release), your source Git repositories and the configured LLM endpoint.
- An **API key with model access**, or a **ChatGPT subscription with OpenCode
  OAuth access**. API-key runs use provider billing; subscription access is separate.
- A frozen **basket.json** from [basket_collector](../basket_collector/README.md).
  Configure host Git credentials for private source repositories; interactive
  Git password prompts are disabled. Repository API tokens are not required
  by the benchmark itself.

OpenCode does not need a separate installation: the runner downloads and caches
`agent.runtime` from your config, then records it with the run.

If `.venv` already exists, `make setup` asks `Reinstall dependencies? [Y/n]`.
Enter or Y repeats setup in that environment; n leaves it unchanged. A closed
stdin aborts setup. The environment is never deleted automatically. Each
confirmed setup upgrades its pip before installing the project dependencies.

</details>

<details>
<summary>Authentication and quota recovery</summary>

Follow OpenCode's printed URL/code instructions and finish login in your browser.
`headless` suits a remote terminal; `AUTH_FLOW=browser` uses the native
browser/callback flow when available on your host. Login and run must use the
same `auth.profile`. An ordinary host OpenCode login does not populate this
private benchmark profile. The runner refreshes credentials on the host. If it
reports `auth_required`, repeat `make auth` with that CONFIG, then resume.
OAuth requires no OpenAI API key; do not bind `OPENAI_API_KEY` in
`agent.credentials` in this mode.

Keep secret values out of YAML and version control. Model access, Git access
and viewer login are separate credentials. See [credential cleanup](docs/credentials.md#credentials-after-a-run)
if you want to remove the private login after your batch.

If ChatGPT reports `usage_limit_reached`, the benchmark stops issuing tasks,
saves active attempt evidence and prints a manual resume command. Wait until
quota is available before resuming; a new login is not required by this error.
See [quota-stop and task states](docs/reference.md#resume-retries-progress-and-logging).

</details>

<details>
<summary>Workers, Git-cache waits and saved evidence</summary>

Set `workers` in YAML for normal concurrency, or pass `WORKERS=5` to override it.
Workers have separate task snapshots, setup processes and OpenCode HOME, but
share host resources and provider quotas. Tasks using the same repository wait
for its Git-cache lock until it is available or you cancel with Ctrl+C. Waiting
does not consume retries; `git.timeout_seconds` limits individual Git commands,
not time in the queue. A live owner that stops making progress can hold up
waiters until cancellation.

The console prints the run directory and paths to `report.json` and the DEBUG
log. Open that directory in your IDE and start at `report.json`; task ordinals
lead to numbered attempts, live logs and source variants. Use the IDE's
**Compare Files** action for base/human/agent pairs, following the manifest's
existence flags. See [Find your results](docs/results.md#find-your-results) for the layout.
Ctrl+C saves an interrupted run and prints a resume command; repeated Ctrl+C
asks you to wait for saving.

`RUN_ID` below is a placeholder for the complete printed run directory name,
including its timestamp, agent label and suffix. Replace it with your actual path:
`OUTPUT=./output` is a parent directory, while `RUN=./output/RUN_ID` selects one
run. With `OUTPUT=./output/my-basket`, use `RUN=./output/my-basket/RUN_ID`.

</details>

<details>
<summary>Compatibility</summary>

Keep each run's original basket and agent settings for resume. Runs from an
incompatible installation cannot be resumed, repriced or rescored; retain their
files and use the installation that created them. Use the pinned OpenCode runtime
from an example; changing it requires checking the telemetry adapter before use.
State v2 runs remain supported. Recording an interruption upgrades mutable state
to v3 while preserving immutable run/native evidence. Use the updated viewer for
these archives; older tools cannot read the recovery contract.
See the [saved-data note](../README.md#reading-data-from-earlier-releases).

</details>
