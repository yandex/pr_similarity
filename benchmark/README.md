# PR Similarity Benchmark

[PR Similarity](../README.md) / Benchmark

Run OpenCode on frozen GitHub/GitLab coding tasks and compare its changes with
human solutions. Each run saves quality scores, token usage, diffs and logs;
you can view the results locally or publish them to a compatible viewer.

Start with [setup and login](#quick-start), then use the guide for your next task:

- [Configure setup, skills, rules and exclusions](#configure-your-agent)
- [Connect MCP or a RAG CLI in bubblewrap](#connect-mcp-and-rag)
- [Find a task's logs, diff and scores](#find-your-results)
- [Estimate cost with a pricing policy](#estimate-cost-with-reprice)
- [Check and remove reusable credentials](#credentials-after-a-run)
- [Publish and diagnose upload failures](#publish-to-a-leaderboard)

## Prerequisites

- **Linux x64 with glibc**, **Python 3.12** with venv/pip support, **GNU Make**,
  **Git** and **Bash**. Other platforms are outside the current runtime adapter.
- **Bubblewrap** (`bwrap`) with working unprivileged user namespaces. The runner
  checks the sandbox before starting agents. Containers and restricted hosts
  must allow user namespaces; installing the binary alone is insufficient.
- Network access to **PyPI**, **GitHub** (dependencies and the pinned OpenCode
  release), your source Git repositories and the configured LLM endpoint.
- An **API key with model access**, or a **ChatGPT subscription with OpenCode
  OAuth access**. API-key runs use provider billing; subscription access is separate.
- A frozen **basket.json** from [basket_collector](../basket_collector/README.md).
  Configure host Git credentials for private source repositories; interactive
  Git password prompts are disabled. Repository API tokens are not required
  by the benchmark itself.

OpenCode does not need a separate installation: the runner downloads the exact
`agent.runtime` version from GitHub and caches it. The examples pin **1.18.33**,
which is the version validated for DB telemetry.

## Quick start

Run from this directory:

```bash
make help
make setup PYTHON=python3.12
make env
```

### Choose model access

For an **API key**, add `OPENAI_API_KEY` to the generated `.env` or your
environment. Choose an accessible model in
[examples/openai-api-key.yaml](examples/openai-api-key.yaml), then run:

```bash
make run CONFIG=examples/openai-api-key.yaml DATASET=/path/to/basket.json OUTPUT=./output
```

For a **ChatGPT subscription**, choose a supported model in
[examples/chatgpt-oauth.yaml](examples/chatgpt-oauth.yaml) and log in once:

```bash
make auth CONFIG=examples/chatgpt-oauth.yaml AUTH_FLOW=headless
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=/path/to/basket.json OUTPUT=./output
```

Follow OpenCode's printed URL/code instructions and finish login in your browser.
`headless` suits a remote terminal; `AUTH_FLOW=browser` uses the native
browser/callback flow when available on your host. Login and run must use the
same `auth.profile`. An ordinary host OpenCode login does not populate this
private benchmark profile. The runner refreshes credentials on the host. If it
reports `auth_required`, repeat `make auth` with that CONFIG, then resume.
OAuth requires no OpenAI API key; do not bind `OPENAI_API_KEY` in
`agent.credentials` in this mode.

Keep secret values out of YAML and version control. Model access, Git access
and viewer login are separate credentials. See [credential cleanup](#credentials-after-a-run)
if you want to remove the private login after your batch.

### Select tasks and retain the run path

Use the included [OpenSearch-10 or pandas-25 baskets](examples/baskets/README.md)
to run without collecting or generating new tasks:

```bash
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=examples/baskets/pandas-25.json OUTPUT=./output
```

Or replace `/path/to/basket.json` with a completed collector export. One config
works with several baskets: tasks are selected by `DATASET`, not YAML. With the
collector quick-start basket:

```bash
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=../basket_collector/output/examples/github-llm-diff/basket.json OUTPUT=./output WORKERS=1
```

Set `workers` in YAML for normal concurrency, or pass `WORKERS=5` to override it.
Workers have separate task snapshots, setup processes and OpenCode HOME, but
share host resources and provider quotas.

The console prints the run directory and paths to `report.json` and the DEBUG
log. Open that directory in your IDE and start at `report.json`; task ordinals
lead to numbered attempts, live logs and source variants. Use the IDE's
**Compare Files** action for base/human/agent pairs, following the manifest's
existence flags. See [Find your results](#find-your-results) for the layout.
Ctrl+C saves an interrupted run and prints a resume command; repeated Ctrl+C
asks you to wait for saving.

`RUN_ID` below is a placeholder for the complete printed run directory name,
including its timestamp, agent label and suffix. Replace it with your actual path:
`OUTPUT=./output` is a parent directory, while `RUN=./output/RUN_ID` selects one
run. With `OUTPUT=./output/my-basket`, use `RUN=./output/my-basket/RUN_ID`.

## Example configurations

| Config | Access | What to configure |
|---|---|---|
| [openai-api-key.yaml](examples/openai-api-key.yaml) | OpenAI API, native SDK | `OPENAI_API_KEY` and an available model |
| [chatgpt-oauth.yaml](examples/chatgpt-oauth.yaml) | ChatGPT subscription OAuth | Login once with `make auth CONFIG=examples/chatgpt-oauth.yaml`, then use the same CONFIG for run |
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

## Commands and checks

`make help` lists commands, parameters and examples without setup or credentials.
Additional workflows and implementation details are in the collapsible sections below.

```bash
make test
make smoke
```

These checks are offline; smoke also builds and installs a wheel outside the
checkout. Similarity and resource identity intentionally ignore executable
bits; chmod-only tasks are outside the v1 metric scope.

## Configure your agent

There are two layers: the outer YAML configures this harness, while
`agent.config.opencode_json` is native OpenCode configuration. The harness
validates its own fields and secret bindings; native fields must still be
accepted by the pinned runtime. Check the attempt's effective-config reference for the merged config actually
discovered by OpenCode, stored under `agent/effective/`.

| Setting | Purpose |
|---|---|
| `agent.name`, `agent.runtime` | Agent label and exact OpenCode version |
| `agent.auth` | API-key mode or ChatGPT OAuth with a private profile |
| `agent.config.opencode_json.model` | Native `provider/model` ID |
| `agent.config.opencode_json.provider` | Endpoint, SDK and model options, including reasoning effort |
| `agent.config.opencode_json.permission` | Native permissions; examples allow unattended execution |
| `agent.config.opencode_json.skills.paths` | Local skill folders to copy into the sandbox |
| `agent.config.opencode_json.instructions` | Additional rules/instructions to copy into the sandbox |
| `agent.config.opencode_json.mcp` | Native local/remote MCP connections |
| `agent.setup.script`, `timeout_seconds` | Bash setup file and its separate time budget |
| `agent.timeout_seconds` | Agent time budget after setup |
| `agent.credentials` | Sandbox variable name → host secret variable name |
| `agent.env` | Nonsensitive environment values forwarded to setup and agent |
| `agent.diff_excludes` | Source paths omitted from captured agent changes |
| `agent.instructions_file` | Replacement task-prompt prefix; null keeps the benchmark instructions |

Configure user labels in the same YAML:

```yaml
schema_version: 1
tags:
  - baseline
  - tdd
agent:
  name: my-api-agent
  runtime: opencode:v1.18.33
  config:
    opencode_json:
      model: openai/gpt-5.6-sol
      permission:
        '*': allow
  credentials:
    OPENAI_API_KEY: OPENAI_API_KEY
```

Top-level YAML `tags` is the sole Setup-tag input. Tag strings,
order and duplicates are preserved. A tag such as `model:custom` remains a user
Setup tag. The generated Model label is `provider/model [effort]` when effort
is known, otherwise `provider/model`. An internal effort tag is present only
for a known level; `tags: []` does not remove the configured Model label.
`agent.name` and Setup tags are frozen when the run is created; edits to those
labels in YAML do not rename an existing run on resume. Viewer owner metadata is
separate, and publication overwrite preserves that server name and Setup tags.

`report.inference` distinguishes configured native values from captured
shared effective snapshots, with source references and known, unknown,
unsupported or mixed status. Effective config confirms runtime configuration;
it does not prove the provider applied an option to an HTTP request. The basket
name and generator model remain frozen collector provenance and never supply
the agent Model label. Pin the runtime, select an available exact provider/model,
set desired effort explicitly and keep declared rule/skill/setup files unchanged
for resume. Runtime resources must exist and be readable before preparation.

For reasoning effort, set options under the exact native model key:

```yaml
# Fragment inside agent.config.opencode_json:
model: openai/gpt-5.6-sol
provider:
  openai:
    models:
      gpt-5.6-sol:
        options:
          reasoningEffort: medium
```

Effort is read from the selected model and SDK, including provider aliases:

| SDK | Selected model `options` |
|---|---|
| `@ai-sdk/openai`, `@ai-sdk/openai-compatible` | `reasoningEffort: medium` |
| `@ai-sdk/anthropic` | `effort: medium` |
| `@openrouter/ai-sdk-provider` | `reasoning: {effort: medium}` |

The selected primary agent (`default_agent`, otherwise the default build agent)
can override the model and options. Request options apply in this order: model,
selected agent, selected variant. Custom variants use their frozen definitions;
recognized built-in effort levels keep their selector as the source.
Unresolved variants remain unsupported instead of inventing a level. Unselected
models/agents and provider connection options do not supply effort. A numeric
thinking budget alone is not translated into a level. These are configured
facts, not proof that the provider applied them.

If you change the model, change both keys. Provider options differ; start from a
complete access-mode example. Responses-compatible endpoints use the native SDK
shown in [openai-endpoint.yaml](examples/openai-endpoint.yaml).

<details>
<summary>Skills, rules, setup and diff exclusions</summary>

[openai-tdd.yaml](examples/openai-tdd.yaml) is a complete skill/rules example:
`skills.paths` points to the vendored Superpowers TDD skill, and `instructions`
points to [agent-tdd.md](examples/agent-tdd.md). Your skill folder needs a valid
`SKILL.md`; keep supporting files inside it. Paths are relative to the YAML file.

Explicitly declared local skills, instructions and file-based plugins are copied
under `/benchmark-runtime/resources/`, outside the scored `/repo`, with rewritten
paths and archived contents. Their hashes participate in resume identity. These
resources need no diff exclusions. Host-global skills/rules in your ordinary HOME
are not inherited. OpenCode can still discover config/rules already present in
the frozen repository. A listed skill is available; actual use needs event/log
or agent evidence. Operator skills in this repository's `.agents/` are separate
from skills supplied to the agent being evaluated.

Setup is a script file, not an inline command string:

```yaml
# Fragment inside agent, for a config stored in benchmark/:
setup:
  script: ./examples/setup-custom-tools.sh
  timeout_seconds: 300
env:
  DOC_SEARCH_ROOT: /repo/docs
credentials:
  RAG_TOKEN: MY_HOST_RAG_TOKEN
```

For a config in `benchmark/examples/`, use `./setup-custom-tools.sh`. The script
runs with Bash inside the sandbox, with the basket's workspace as cwd, before
every fresh attempt, including retries. Its contents are archived and hashed.
Failure or timeout prevents the agent from starting. Setup and agent receive
credentials through the child environment; never print them or embed them in
source, rules or setup files.

Setup and OpenCode are separate processes in separate bubblewrap invocations.
Writes to `$HOME` and `/repo` persist within the attempt. An `export` in setup does
not change the agent's environment: use YAML `env`/`credentials`. `/tmp` is a fresh
tmpfs each invocation. A setup background process is not a persistent agent
service: use native local MCP or a separately operated network service. Private
HOME and source are discarded after the attempt; saved evidence remains. A new
retry starts again from before and reruns setup.

Prefer `$HOME/.local/bin`, `$HOME/.venv` and `$HOME/index` for tools, dependencies
and retrieval indexes. The baseline is recorded **before setup**: writes under
`/repo` count as agent changes. New Git-ignored files are not captured, but ignore
rules do not hide edits to already tracked files.

If an integration must write generated files inside the source tree, declare
narrow exclusions and inspect the resulting `capture.json`:

```yaml
# Fragment inside agent:
diff_excludes:
  - '<repo_root>/.benchmark-tools'
  - '<workspace>/.rag-cache/**'
```

`<repo_root>/` means the exported repository root; `<workspace>/` and unprefixed
patterns mean the basket-selected working directory. These are source paths,
not host paths. `*` matches within a path segment, `**` across segments, `?` one
character; a directory pattern covers its descendants. `capture.json` records
excluded paths and the matching rules. Avoid excluding a whole source subtree
to suppress extra edits.

Exclusions affect agent changes only. Human-reference files remain in scoring:
a human change to an excluded file can count as missed because its AI state is
base. Exclusions do not hide files from the agent or restrict reads.

For a baseline, keep `setup: null`, `instructions_file: null`, no added native
skills/instructions/MCP, and `diff_excludes: []`. Hold model, effort and retry
policy constant when comparing it with an integration.

</details>

## Connect MCP and RAG

Native OpenCode MCP config and sandbox-visible CLI tools are supported. Network
access is shared with the host, while filesystem access is limited:

| Inside bubblewrap | Access / lifetime |
|---|---|
| `/repo` | Writable before-only source; cwd is the basket workspace |
| `/repo/.git` | Read-only fresh local baseline, without original PR history |
| `/sandbox-home` (`$HOME`) | Writable private state shared by setup/agent within the attempt |
| `/benchmark-runtime` | Read-only config, setup and copied declared resources |
| `/benchmark-bin/opencode` | Read-only pinned OpenCode executable |
| `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64` where present | Read-only host system tools/libraries |
| Selected DNS/TLS files from `/etc` | Read-only name resolution and certificate support |
| `/tmp` | Fresh writable tmpfs for each individual invocation |

There is **no YAML option for arbitrary host bind mounts**, a host HOME, Docker
socket, custom PATH, or a persistent setup daemon. The launcher owns `HOME`,
`PATH`, `XDG_*`, `OPENCODE_*`, `GIT_*` and `LD_*`; config bindings cannot replace
them. A host tool under your HOME, host virtualenv or host `/tmp` is not visible.
Install it into the private HOME, package nonsensitive files as declared local
resources, or fetch pinned inputs in setup. Copied local resources reject symlinks.

<details>
<summary>Remote MCP, local MCP and a retrieval CLI</summary>

**Remote MCP.** Merge this into `agent.config.opencode_json` and supply your own
service endpoint:

```yaml
# Fragment inside agent.config.opencode_json:
mcp:
  retrieval:
    type: remote
    url: https://your-retrieval-service.example/mcp
    enabled: true
    oauth: false
    headers:
      Authorization: '{env:RAG_AUTHORIZATION}'
    timeout: 10000
```

```yaml
# Fragment inside agent:
credentials:
  RAG_AUTHORIZATION: MY_HOST_RAG_AUTHORIZATION
```

The host variable holds the **entire header value**, including `Bearer ` when
required. The secret field must be a complete `{env:VARIABLE}` reference; a
literal prefix plus interpolation is rejected by the harness. Store the host
variable in the environment or `benchmark/.env`. MCP `timeout` is milliseconds;
harness timeouts are seconds. This template needs your service and credentials.
The shared network also reaches host loopback services. DNS, TLS, routing and
service authentication must work from the host.

Model ChatGPT OAuth and MCP OAuth are separate. The runner does not import your
host `mcp-auth.json` or automate an interactive MCP login for each attempt. Use a
service accepting environment-supplied credentials for an unattended batch, or
your own supported noninteractive setup flow. Keep private profiles out of
archived skills and source files.

**Local MCP.** Setup must install the server and its dependencies into the
attempt's private HOME. Then OpenCode starts its stdio process inside bubblewrap:

```yaml
# Fragment inside agent.config.opencode_json:
mcp:
  retrieval:
    type: local
    command: ['/sandbox-home/.local/bin/my-mcp', '--stdio']
    enabled: true
    environment:
      RAG_TOKEN: '{env:RAG_TOKEN}'
    timeout: 10000
```

Replace the executable/arguments with your installed server and bind `RAG_TOKEN`
through `agent.credentials`. `command` is executable plus arguments, not a shell
expression: `$HOME` is not expanded there. Use `/sandbox-home/...` or an
executable on the fixed PATH. For a Python package, setup can create a venv at
`$HOME/.venv` and install exact package versions using its interpreter's `-m pip`,
provided system Python has venv/pip support. The pinned OpenCode binary does not
supply Node/npm/Python toolchains required by arbitrary servers.

An MCP connection can fail while the model still runs. Verify connection status
in setup, for example with `/benchmark-bin/opencode mcp list`, and inspect agent
logs/events for actual calls. A config dump or successful model execution alone
does not prove retrieval worked.

**RAG through a CLI.** The complete [custom-tools.yaml](examples/custom-tools.yaml)
uses [setup-custom-tools.sh](examples/setup-custom-tools.sh) to install `doc-search`
under `$HOME/.local/bin`, already on the fixed PATH.
[agent-custom-tools.md](examples/agent-custom-tools.md) tells the agent how to
invoke it. The stdlib tool searches Markdown/text in the before snapshot: it
illustrates CLI plumbing, rather than providing a semantic RAG backend. Setup
checks the command inside bubblewrap and does not write into source.

```bash
make auth CONFIG=examples/custom-tools.yaml AUTH_FLOW=headless
make run CONFIG=examples/custom-tools.yaml DATASET=/path/to/basket.json WORKERS=1
```

For your CLI, replace the installer with a pinned package/binary installation
and a bounded health check. Put endpoints in `agent.env` and secrets in
`agent.credentials`; the CLI inherits them. Fetch a versioned index into
`$HOME/index`, verify its digest, and describe its query interface in native rules
or a skill. Files mentioned only in setup shell commands or MCP `command` are
not automatically copied from the host. Declare supported local resources or
install/download those files explicitly.

Resume hashes local setup/rules/skills, but cannot freeze a remote service's
contents or an unpinned installer. Record backend/index versions and evidence
of actual use for comparisons. Retrieval data must exclude the target PR's
solution and after-revision.

These fragments follow the pinned
[OpenCode 1.18.33 MCP schema](https://github.com/anomalyco/opencode/blob/v1.18.33/packages/core/src/v1/config/mcp.ts)
and [skills schema](https://github.com/anomalyco/opencode/blob/v1.18.33/packages/core/src/v1/config/skills.ts).
Newer runtime documentation may describe a different config shape.

</details>

## Find your results

For human inspection, open the **printed run directory in your IDE**, then read
**`report.json`**.
Every task has a stable `ordinal` and `directory`; the number matches the task
folder prefix and live progress row. Final attempt, scoring and pricing refs
come from `_meta/state.json`. The report is its readable projection; an older
successful retry does not replace the selected row.

```text
output/<time>-<agent>-<suffix>/
├── run.json, basket.json, config_identity.json   frozen registry and inputs
├── report.json, run.log                         current results and human lifecycle
├── agent/
│   ├── opencode.json                            frozen portable native input
│   ├── resources/                               declared skills, instructions and plugins
│   ├── task_instructions.txt, setup.sh           common prefix and optional setup
│   └── effective/<sha256>.json                   unique captured runtime configurations
├── _meta/
│   ├── state.json                               authoritative selections; do not edit
│   └── invocations/<uuid>/
│       ├── invocation.json, config.json          command metadata and input snapshot
│       └── benchmark.log                        DEBUG diagnostics and tracebacks
├── tasks/003-<repository>-pr-<number>-<suffix>/
│   └── attempts/002/
│       ├── start.json, seal.json, outcome.json   reservation, native capture and result
│       ├── agent.log                            live timestamped human transcript
│       ├── events.jsonl, runtime.log            native JSON; stderr/non-JSON diagnostics
│       ├── stream_receipt.json                  raw-stream completeness and warnings
│       ├── prompt.txt, launch.json               task input and shared-input references
│       ├── effective-config.receipt.json         optional runtime capture diagnostics
│       ├── setup.log, agent.diff, capture.json   setup and source capture
│       ├── triplets/index.json                  source paths, modes, existence and digests
│       ├── triplets/001/src/main_base.py         before source
│       ├── triplets/001/src/main_human.py        human after source
│       ├── triplets/001/src/main_agent.py        captured agent source
│       ├── scoring.json, scoring.log            initial host scoring
│       └── telemetry/                           usage.json, sanitized DB and receipt
└── operations/
    ├── rescore/<uuid>/                          immutable input snapshots/result/receipt
    ├── reprice/<uuid>/                          policy/calculator/input snapshots/result/receipt
    └── publish/<uuid>/                          payload.tar.gz, request.json, receipt.json
```

A task folder is allocated only when its numbered attempt starts. Future live
log locations appear in the report before the native child is issued. Start a
normal editor/tail on `agent.log`; it shows text, tool targets and outcomes.
Truncated tool output links to the full `events.jsonl` line. Raw JSON includes
unknown native event types; `runtime.log` labels stderr and non-JSON stdout.
Empty/missing logs do not prove a successful agent or complete capture: check
execution, `stream_receipt.json`, `seal.json` and telemetry receipt.

`run.json` and `report.json` reference the same frozen `agent_input`. Shared
resources are saved once; each worker copies them into its private sandbox.
Native resource paths in `agent/opencode.json` are relative to the `agent/`
directory and become `/benchmark-runtime/resources/...` when launched. The JSON
keeps native permission rule order. Complete `prompt.txt` stays task-specific.

Identical effective configurations share one content-bound file. Each attempt
still records its own capture status and seal reference; missing capture remains
unknown. Different runtime configs stay separate, including repository defaults.
Scoring and config diagnostic logs contain stderr; valid machine results are
saved once as JSON. Failed/malformed stdout remains available for diagnosis.

The viewer's config button shows one safe native input, including model options,
skills and instructions. Timeout, retries and other benchmark controls appear on
the run page. Public projection can refuse unsafe/unsupported native values with
an explicit reason; the original input remains in the owner's full archive.
Pre-release runs without shared `agent_input` are unsupported by this release;
keep their original files and inspect them with the tool version that made them.

For a file comparison, use `triplets/index.json` to check existence, then select
two role files and your IDE's **Compare Files** action: `base` ↔ `human` for the
reference change, `base` ↔ `agent` for agent edits, or `human` ↔ `agent` for agreement.
An absent role is distinct from a real empty file; do not invent an empty file.
A file’s variants share one directory, with `_base`, `_human`, `_agent` before
the original extension. Each source file has its own numbered entry directory,
containing the original repository parent path; this avoids file/directory
collisions. `index.json` preserves the full original path and exact refs. Very
long stems are shortened with a stable digest to respect filename limits.
Run-relative refs survive moving the whole run. The disposable source checkout/private HOME is not retained or archived.

<details>
<summary>List selected task states and live locations</summary>

Run from `benchmark/`, replacing the run-directory placeholder:

```bash
.venv/bin/python - ./output/RUN_ID <<'PYCODE'
import json
import sys
from pathlib import Path
run = Path(sys.argv[1])
report = json.loads((run / 'report.json').read_text())
print('run:', report['run']['status'], 'summary:', report['summary'])
for task in report['tasks']:
    attempt = task['final_attempt']
    sequence = None if attempt is None else attempt['sequence']
    print(task['ordinal'], task['repository'], task['number'], 'attempt:', sequence,
          task['execution']['status'], task['changes']['status'], task['scoring']['status'])
    for name, relative in task.get('locations', {}).items():
        print(' ', name, run / relative)
PYCODE
```

Read-only JSON/log/editor inspection needs no viewer, server, plugin or writer
lock. It does not create an invocation or new DEBUG folder. HTML/compare commands
remain optional, explicit presentations; running an agent creates no Markdown or
HTML report automatically.

</details>

<details>
<summary>Read task states, scores and retry history</summary>

| Field | Meaning |
|---|---|
| `ordinal`, `directory`, `progress` | Stable navigation and current observed phase |
| `processing_status` | Coordinator state; completed is not a correct-solution claim |
| `setup.status`, `execution.status` | Native preparation and process outcome, independently |
| `infrastructure_error` | Native preparation/provider/capture failure, if present |
| `changes.status` | Captured diff is nonempty, empty or unknown |
| `scoring.status`, `Mf_mlp`, `LCf_mlp` | Availability, mean file similarity and LOC-weighted similarity |
| `telemetry` | Saved final-attempt usage; unknown values remain null |
| `costs.final_attempts`, `costs.all_attempts` | Separate native/API estimates, known subtotal and coverage |

Timeouts and native/provider errors can retain valid source and scores. MLP is
agreement with human edits on `[0, 1]`, not a test pass rate. Compare identical
baskets and model/effort/setup, and report scored coverage. Setup, executable-bit
changes, earlier retries and unavailable scores require their own interpretation.

`seal.json` closes native execution, diff, source and usage before host scoring.
Initial `outcome.json` stays immutable. Rescore and host-only recovery create
operation results and switch selected refs; they do not rewrite old outcomes,
reset native timings or consume a native retry. Uncommitted operation results
are ignored. A damaged state is not reconstructed from the newest file; a valid
state can rebuild a missing report cache. Preserve the whole run and use explicit
RESUME with the original full basket and compatible agent inputs.

Sanitized SQLite is accounting rather than a transcript. Effective config and
copied skills prove availability/configuration, not a skill invocation, MCP call
or provider behavior. Follow raw events/human logs for those claims.

</details>

## Estimate cost with reprice

`reprice` recalculates saved request usage without contacting the model. It stores
immutable policy/calculator, input snapshots, result and receipt, then selects
API-equivalent fields through state for current and historical attempts. Original OpenCode-reported costs remain separate.
For a ChatGPT subscription run, this is an **API-equivalent estimate**, not the
subscription bill or a measured share of its monthly price.

Use the complete [GPT-6.1 Sol policy](examples/pricing.openai-gpt-6.1-sol.yaml):

```bash
make reprice RUN=./output/RUN_ID PRICING=examples/pricing.openai-gpt-6.1-sol.yaml
```

For a fresh run or resume with automatic pricing:

```bash
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=/path/to/basket.json REPRICE=1 PRICING=examples/pricing.openai-gpt-6.1-sol.yaml
```

Choose the policy matching the model actually used; that command alone does not
change the example's agent model. Update the config's `model` and its model-option
key to `gpt-6.1-sol` if you want that model. `PRICING` is a Make/CLI argument, not
an agent YAML field. `PRICING` on run requires `REPRICE=1`.

<details>
<summary>Create an OpenAI policy and verify coverage</summary>

A policy needs `schema_version`, an audit-friendly `id` and `source`, `currency:
USD`, a `service_tier` label, and exact provider/model rows. The schema is
[benchmark-pricing-v1.schema.json](src/schemas/benchmark-pricing-v1.schema.json).
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
from benchmark.artifact_refs import read_reference
from benchmark.pricing import load_policy
from benchmark.run_store import RunStore
run, policy_path = map(Path, sys.argv[1:])
policy = load_policy(policy_path)
covered = {(r['provider'], r['model']) for r in policy.data['models']}
seen = set()
store = RunStore.open(run)
for task in store.state['tasks']:
    owners = [*task['history'], task] if task['attempt'] is not None else task['history']
    for owner in owners:
        if owner['seal'] is None:
            continue
        native = read_reference(run, owner['seal'])
        usage = native['task']['telemetry']['usage_ledger']
        if usage is None:
            continue
        owned = {r['path']: r for r in native['artifact_receipts']}
        if usage['path'] not in owned:
            continue
        ledger = read_reference(run, owned[usage['path']])
        seen.update((r['provider'], r['model']) for r in ledger['requests'])
print('policy:', policy.data['id'], 'sha256:', policy.sha256)
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

</details>

## Credentials after a run

The persistent ChatGPT profile and an attempt's private HOME have different
lifetimes. In the default OAuth example, reusable login credentials live at:

```text
~/.local/share/prsimbm/chatgpt/data/opencode/auth.json
```

The profile intentionally survives the run for login reuse and token refresh.
It is outside the output directory and is never added to the publication archive.
The runner forwards access-only OAuth credentials to the worker environment,
with the refresh token left empty. Each attempt uses a temporary private HOME;
normal completion/cancellation disposes of it. A host crash or forced kill can
leave temporary files, so directory cleanup is not equivalent to token revocation.

Check **file presence and permissions**, without printing the file's contents:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import stat
profile = Path('~/.local/share/prsimbm/chatgpt').expanduser()
auth = profile / 'data/opencode/auth.json'
print('profile exists:', profile.exists())
print('auth file exists:', auth.exists())
if auth.exists():
    print('auth file mode:', oct(stat.S_IMODE(auth.stat().st_mode)))
PY
```

Substitute your configured profile if it differs. The private profile directory
is mode 700 and native credentials are created with an owner-only umask. Never
`cat` the credentials into a terminal log, issue or chat.

If you want no reusable local login after a batch, wait until **all runs using
this profile have stopped**, then deliberately remove only its auth file:

```bash
rm -i -- ~/.local/share/prsimbm/chatgpt/data/opencode/auth.json
```

The interactive command asks before removing the local credential file. Check
its absence with the presence-only snippet above. The next run needs `make auth`
again. This is local deletion, not server-side revocation. The harness has no
`make logout` or automatic profile deletion option.

API keys from `.env`/your environment and operator-supplied viewer `TOKEN_FILE`
also remain where you put them; the harness does not delete those inputs. Viewer
login passwords/grants are held in memory by the publisher, not written into its
operation journal. Known credential values are redacted from subprocess streams;
usage SQLite is exported without credential tables. This is not a promise that
arbitrary setup tools cannot copy secrets elsewhere. Before sharing a run, review
its prompts, source, setup/resources and logs; checking only for `auth.json` is
not a complete secret audit.

## Publish to a leaderboard

Use the **run directory**, not its OUTPUT parent. Ask the viewer administrator
for an ordinary account on the target instance; ChatGPT login is separate.
`URL` must be an HTTPS origin, with no path/query. A trailing `/` is accepted.
The mock viewer's loopback HTTP UI is browseable, but the publisher requires HTTPS.

After the writer stops, validate the complete local archive:

```bash
make publish RUN=./output/RUN_ID URL=https://viewer.example.org DRY_RUN=1
```

This writes a local archive/operation log, with no HTTP or authentication. Missing
evidence refuses publication: keep the entire run, not only `report.json`. When
you intend to upload:

```bash
make publish RUN=./output/RUN_ID URL=https://viewer.example.org
```

Publication shows its phase and elapsed time. After archive transfer, object-store
persistence can take several minutes; wait for receipt and readback confirmation.
The upload response wait is bounded to 960 seconds (connect: 20 seconds), allowing
the viewer's default 900-second transfer budget. Login and readback keep their
shorter timeout. `PROGRESS=auto` updates a TTY line, `plain` prints a heartbeat
every 10 seconds, and `off` suppresses transient feedback. A timeout retains the
exact pending payload/key; repeat the command to recover the same publication.

In a terminal the client prompts for login and a hidden password, then requests
a one-use grant. Automation can use `TOKEN_ENV=PRSIM_VIEWER_TOKEN` or
`TOKEN_FILE=/private/publish.token` (mode 600, outside the run). Choose one source;
never pass literal passwords/tokens in Make arguments or YAML.

Confirm `status: completed` in `operations/publish/<uuid>/receipt.json`, including
server owner and original archive/report hashes. The publisher also reads back
run/config/tasks over HTTPS and checks basket and configured Model/Effort origins.
An overwrite preserves server display name/Setup tags and reports differences
from local labels. For an ambiguous upload, repeat the same command to replay
its frozen `payload.tar.gz`, request and idempotency key. `_meta/state.json`
selects `pending_publish`; moving the complete run preserves relative replay refs. A 409 is a conflict; use
`OVERWRITE=1` only when you intend to replace the same owner's publication.
The server saves a backup. See [viewer usage](../viewer/README.md#use-the-leaderboard).

<details>
<summary>Distinguish a wrong password, TLS failure and broken API</summary>

“Publication failed” is a safe summary, not a diagnosis. Each command writes
`_meta/invocations/<uuid>/benchmark.log` at DEBUG regardless of console `LOG_LEVEL`.
Use that folder's `invocation.json` (`kind: publish`, `logs_dir`) to locate the
command log, and the selected publish request/receipt for exact operation identity. Inspect it locally; do not paste a whole log without checking its contents.

Print the latest publication log path without reading any credentials:

```bash
.venv/bin/python - ./output/RUN_ID <<'PY'
import json
import sys
from pathlib import Path
run = Path(sys.argv[1])
calls = [json.loads(p.read_text()) for p in (run / '_meta/invocations').glob('*/invocation.json')]
calls = [c for c in calls if c.get('kind') == 'publish']
if not calls:
    raise SystemExit('No publication invocation found.')
latest = max(calls, key=lambda c: c['started_at'])
print(run / latest['logs_dir'] / 'benchmark.log')
PY
```

Open that file locally and look for the HTTP response status of the POST and PUT.

| Evidence | Meaning / next step |
|---|---|
| `CERTIFICATE_VERIFY_FAILED`, self-signed certificate | TLS failed before the password request reached the API; configure certificate trust |
| Hostname mismatch / expired certificate | Fix the origin/certificate, not the password |
| DNS, connection refused, timeout | Check address, listener, routing/proxy and service availability |
| Authentication request rejected with 401/403 | Check the account on this viewer, password and enabled state; those causes are intentionally not distinguished publicly |
| 429 | Login rate limit; wait, then retry with the correct account |
| 404, HTML response, redirects on the API route | Check origin, reverse-proxy routing and viewer API compatibility |
| 5xx | Server/proxy failure; administrator should inspect server logs |
| PUT returns 409 | Existing run conflict, separate from login failure |

Read-only checks, without login/password:

```bash
curl --fail --show-error https://viewer.example.org/healthz
curl --fail --show-error https://viewer.example.org/api/v1/auth/session
```

A health response of `{"status":"ok"}` shows reachability; the anonymous session
route exercises the API. Neither proves a particular account/password or storage
upload is valid. A browser can have manually accepted an untrusted certificate
while Python still rejects it.

If the credential POST returned 200 and the PUT returned 503, the password was
accepted: check the viewer's staging/storage side. An S3 viewer may keep serving
its cached catalogue while refreshing object-store credentials fails. The
administrator should check the configured SDK credential provider and storage
connectivity; a healthy listener does not establish either. Do not delete a
pending client journal or reset an account as a storage workaround.

For a private viewer with a self-signed certificate, obtain its **public**
certificate or CA bundle from the administrator and verify its identity. Supply
that trusted file to the publisher through HTTPX's `SSL_CERT_FILE`:

```bash
SSL_CERT_FILE=/path/to/viewer-trust.pem make publish RUN=./output/RUN_ID URL=https://viewer.example.org
```

Certificate/hostname verification stays enabled. Use a trust bundle including
any other needed roots; do not pass the server's private key or disable TLS
verification. Test trust first with `curl --cacert /path/to/viewer-trust.pem` and
the health/session URLs. `SSL_CERT_FILE` configures the host publication client;
it does not install custom certificates into the agent's bubblewrap sandbox.
The credentials/grant POST route is `/api/v1/auth/publish-token`; the upload route
is PUT `/api/v1/runs/{run_id}`. An administrator can inspect those server-side
failures without requesting the user's password.

</details>

## Further details

<details>
<summary>Setup, configuration and Git access</summary>

The [TDD skill](examples/skills/test-driven-development/SKILL.md), its linked
testing reference and [MIT license](examples/skills/test-driven-development/LICENSE)
are vendored unchanged from `obra/superpowers` at commit
`8ca22dba9a94f28898bbce59f2537ff4d87c747d`.
[UPSTREAM.json](examples/skills/test-driven-development/UPSTREAM.json) records
source paths and SHA-256 digests. [agent-tdd.md](examples/agent-tdd.md) defines
when the agent should load it and keeps documentation-only tasks in scope.
No separate skill installation is needed. The benchmark copies declared skills
and instructions outside the scored source tree and hashes their contents for
resume identity; changing them requires a new run.

The setup installs hash-pinned dependencies into this module's own `.venv`.
It uses public PyPI by default; `PACKAGE_INDEX_URL` can select a mirror that
contains every pinned version. CodeBLEU is installed from an exact GitHub source
archive with its hash checked. Build prerequisites are also hash-pinned.
`PYTHONHASHSEED=42` is fixed by Make.
The checks run offline; the smoke also builds and installs a wheel, then loads
its schemas and task instructions outside the source checkout.

Use [basket_collector](../basket_collector/README.md) to prepare `basket.json`.
The benchmark independently validates that frozen v1 contract: `tasks`, opaque
change IDs, source fetch URL/ref hints, exact commit/tree IDs and collector-selected
workspace. It imports no collector code and calls no GitHub/GitLab APIs. Each
attempt downloads source objects using its own Git adapter; ref tips can deliver
historical objects but never replace the saved before/after revisions.
[config.yaml](config.yaml) shows the single-agent run configuration;
replace its placeholder model before using it with a real provider. Keep API
keys in environment variables or `.env`, and reference their variable names
through `agent.credentials`. Native provider keys use `{env:VARIABLE}`.

Git authentication belongs to the operator. Configure a host credential helper
or askpass before the batch run; terminal password prompts are disabled. Only
credential settings are forwarded to host fetch processes, while hooks, filters
and the agent's private Git baseline stay isolated. Git credentials and agent
LLM credentials are separate. Private GitHub/GitLab sources can use the same
interface when the credentials have project read/clone/fetch access; repository
visibility is deliberately absent from the basket. Authentication, transport and
unavailable-revision errors stay distinct and give setup/resume recommendations.

</details>

<details>
<summary>Resume, retries, progress and logging</summary>

```bash
make run CONFIG=config.yaml DATASET=basket.json RESUME=./output/RUN_ID WORKERS=4
```

A new run creates one timestamp/agent/suffix directory beneath OUTPUT (default
`./output`), with its complete UUID in `run.json`.
Use explicit RESUME to continue that directory with the same complete basket
and meaningful agent configuration. Workers, technical paths and credential
values may change; model, runtime, native rules, setup contents, timeouts,
retry policy and diff exclusions are part of the run identity. Each run has
its own writer lock; separate runs can execute concurrently. Git preparation
has separate `git.timeout_seconds`, `git.initial_depth`, `git.max_depth` and
`git.max_transfer_bytes` limits. These are technical settings outside the agent
identity; each invocation retains its full config. The byte cap conservatively
counts growth of Git object and temporary-pack storage during retrieval.

Ctrl+C stops issuing tasks, cancels active processes and saves an interrupted
state. Resume resets the retry budget for unfinished tasks and preserves
attempt sequence/history. Scoring runs in an owned child process and can be
cancelled too. If a native seal binds intact source, resume completes its host scoring without
another agent call. Scoring failure/cancellation stays host-only; unknown/lost
telemetry is not recovered by repeating the old model session. This host
recovery does not consume the reset budget for fresh native attempts. Repeated
Ctrl+C only asks you to wait: termination deadlines remain controlled by the code,
and saved artifacts are not cut short by another signal.
Timeouts can still have a nonempty diff and a quality score. Infrastructure
failures are separate and always use the configured retry budget. HTTP 5xx follows
that infrastructure policy. Credentials rejection stops the whole run for explicit
resume after renewal. Other terminal OpenCode errors are agent failures even when
its exit code is zero; partial diffs can still be scored. Native OpenCode owns 429
retries. Enable `agent.retries.categories: [agent_error]` to retry agent failures
under the same `max_retries` budget; it is disabled by default.

PROGRESS defaults to auto: TTY creates a task row when work starts, then updates
that row through preparing/setup/running/saving/scoring and retries. Ordinal,
attempt and execution/scoring outcome remain visible; paths/hashes stay in files.
Final rows freeze once. Active rows fit the viewport, with a hidden-task count.
Redirected/plain output prints significant transitions and a five-second heartbeat;
off keeps the final summary, warnings and errors. Counters use saved completed/total
and active work; elapsed is total run time, not a model-inference measurement.
The startup prints report/log paths; interrupted runs print a ready-to-use resume command.
These display settings do not change run identity or task outcomes.

LOG_LEVEL defaults to INFO and changes only user-facing output. Warnings and
errors remain visible at every level; detailed diagnostics and tracebacks stay
in `_meta/invocations/<uuid>/benchmark.log`; `run.log` keeps human lifecycle messages.

</details>

<details>
<summary>ChatGPT subscription OAuth</summary>

```bash
make auth CONFIG=examples/chatgpt-oauth.yaml AUTH_FLOW=headless
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=basket.json
```

Private ChatGPT subscription login uses `make auth`, with AUTH_FLOW=headless
or browser, after selecting chatgpt_oauth and a private profile path in config.
Workers receive access-only credentials. Waiting for the profile lock is
cancellable; an already-started host refresh finishes within its configured
timeout and saves token rotation before honoring cancellation. The profile is outside run directories
and is never archived. API credentials can be supplied without this login.

</details>

<details>
<summary>Offline rescoring and pricing</summary>

```bash
make rescore RUN=./output/RUN_ID
make reprice RUN=./output/RUN_ID PRICING=pricing.yaml
make run CONFIG=config.yaml DATASET=basket.json REPRICE=1 PRICING=pricing.yaml
```

`make rescore` and `make reprice` work offline. They save immutable input-state/report
snapshots and result/receipt under `operations/`, then commit selected refs and
rebuild the report. A failed/uncommitted operation preserves prior selections. `REPRICE=1 PRICING=pricing.yaml` enables automatic pricing
on run/resume. A pricing gap returns a nonzero command exit code while keeping
the saved agent run status and quality scores; console output explains this.
The command reports Python's status through Make (Make generally returns 2
when a recipe fails).

</details>

<details>
<summary>Local viewing, archives and publication</summary>

```bash
make visualize RUN=./output/run-id NO_BROWSER=1
make compare RUNS_JSON='["./output/run-a", "./output/run-b"]' NO_BROWSER=1
make publish RUN=./output/run-id URL=https://viewer.example DRY_RUN=1
make publish RUN=./output/run-id URL=https://viewer.example TOKEN_ENV=PRSIM_VIEWER_TOKEN
make publish RUN=./output/run-id URL=https://viewer.example TOKEN_FILE=/private/publish.token OVERWRITE=1
```

Visualize writes a self-contained `report.html`; compare writes a unique HTML
file beside the first run. Both support filtering and sorting, use final
attempt scores, and show unknown token/cache/cost counters as —. Different
baskets are labelled explicitly. Without NO_BROWSER=1 the local page opens
in your default browser. These commands preserve report.json.

Publish snapshots a stopped run under its writer lock. A flat tar.gz contains
the registry/state, saved attempts, sanitized usage, inputs, logs and operation history.
Its manifest verifies paths, sizes, checksums and required references. Missing
required artifacts refuse upload; private source/HOME, credentials, symlinks
and special files are excluded by refusing unsafe run contents. Each publish operation freezes `operations/publish/<uuid>/payload.tar.gz`,
`request.json`, followed by a closed `receipt.json` for an upload or
`validation.json` for a dry-run. State
selects an unresolved request only after its payload is frozen. Relative refs
survive whole-run relocation; an external archive cache is only a cache.
Older publisher payload blobs are omitted from future archives to prevent
recursive growth; request/receipt history remains. DRY_RUN validates locally
without HTTP or authorization.

Publication uses HTTPS with certificate verification. Supply exactly one of
TOKEN_ENV (a variable name), TOKEN_FILE (owner-only mode 600, outside the run),
or TOKEN_PROMPT=1. With no explicit source, PRSIM_VIEWER_TOKEN is checked;
otherwise an interactive login and hidden password request a one-use grant.
There are no literal TOKEN/LOGIN/PASSWORD options or client-selected authors.
The server determines the author, token TTL, one-use consumption and storage.
The client holds credentials only in memory.

HTTP 409 returns an error with no automatic replacement. OVERWRITE=1 requests
replacement after a server backup. An unresolved upload stores its exact
archive and idempotency key; repeating publish recovers those original bytes
and settings before starting a new operation. Keep the saved pending archive
until the server confirms its receipt. After a confirmed conflict an explicit
overwrite uses a new key. Publication errors preserve local agent results.
Current publication logs and the receipt enter the next archive snapshot.

The client contract is POST `/api/v1/auth/publish-token` and PUT
`/api/v1/runs/{run_id}` with digest and idempotency headers. A compatible viewer
server is required for live uploads; local dry-run and transport tests work
without one. The viewer service and user administration are separate modules.

</details>

<details>
<summary>Scoring, telemetry and report format</summary>

The runnable harness validates frozen baskets, prepares complete before-only
Git snapshots, runs one pinned OpenCode agent in private bubblewrap sandboxes,
and scores tracked and nonignored new files with fixed MLP weights. Each retry
starts fresh. A frozen registry and authoritative state select final native/score/price facts;
`report.json` is their projection. All attempts, logs and operations stay inside
the portable run directory. Generated JSON documents use readable UTF-8/indent=2;
native `events.jsonl` stays one compact JSON object per line.

Similarity and local-resource identity intentionally ignore executable bits.
Baskets requiring chmod-only solutions are outside the v1 metric's intended scope.
CodeBLEU checks every pinned grammar before generation; broken imports or parser
APIs fail explicitly. Text parse failures retain whole-triplet fallback to
default MLP, while invalid API results fail and valid zero values remain measurements.

DB telemetry includes root, subagent and detached sessions without retaining
raw message/tool bodies or credential tables. Optional API-equivalent pricing
uses saved request-level usage and frozen policy/calculator snapshots; original
OpenCode cost stays separate. Unknown usage or pricing remains `null`. The native DB billing adapter is
validated for OpenCode 1.18.33; other exact runtime versions require separate
adapter compatibility checks.

Source code lives directly in `src/`; packaging maps it to the owning Python
namespace. Schemas and conformance fixtures belong to this project.

Report rows live in `tasks`; sidecar references use `task_id`, and outcomes
store the final row under `task`. Row `id` matches the basket's opaque ID.
Rescore, repricing, telemetry, archives and local HTML consume this single v1
format. The former prerelease PR-specific format has no compatibility reader.

</details>
