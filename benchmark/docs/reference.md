[PR Similarity](../../README.md) / [benchmark](../README.md)

# Reference

- [Setup, configuration and Git access](#setup-configuration-and-git-access)
- [Resume, retries, progress and logging](#resume-retries-progress-and-logging)
- [ChatGPT subscription OAuth](#chatgpt-subscription-oauth)
- [Offline rescoring and pricing](#offline-rescoring-and-pricing)
- [Local viewing, archives and publication](#local-viewing-archives-and-publication)
- [Scoring, telemetry and report format](#scoring-telemetry-and-report-format)

### Setup, configuration and Git access

The [TDD skill](../examples/skills/test-driven-development/SKILL.md), its linked
testing reference and [MIT license](../examples/skills/test-driven-development/LICENSE)
are vendored unchanged from `obra/superpowers` at commit
`8ca22dba9a94f28898bbce59f2537ff4d87c747d`.
[UPSTREAM.json](../examples/skills/test-driven-development/UPSTREAM.json) records
source paths and SHA-256 digests. [agent-tdd.md](../examples/agent-tdd.md) defines
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
Test and smoke suites require GCC and G++ to verify native compilation inside
the Python-isolated sandbox.

Use [basket_collector](../../basket_collector/README.md) to prepare `basket.json`.
The benchmark independently validates frozen baskets: tasks, source identities,
exact Git revisions and workspace, plus the recorded selection, coverage and
omissions. All exported tasks run, including those in a partial basket; rejected
omissions are reported separately from failed records. Do not rewrite an existing
run's basket.
Resume always requires the original basket bytes; use a new run for a
new export. A collector retry can leave the previous published basket in place;
check the collector's export lineage before starting a run on its output path.
It imports no collector code and calls no GitHub/GitLab APIs. Each
attempt downloads source objects using its own Git adapter; ref tips can deliver
historical objects but never replace the saved before/after revisions.
[full.yaml](../examples/full.yaml) shows the single-agent run configuration;
it uses the GPT-6.1 Sol medium baseline via ChatGPT OAuth. Keep API
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



### Resume, retries, progress and logging

```bash
make run CONFIG=examples/full.yaml DATASET=basket.json RESUME=./output/RUN_ID WORKERS=4
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

When resume must recover a saved score first, its initial plan leaves the
number of new native attempts provisional. For example, scoring a cancelled
attempt can be followed by a fresh attempt under the existing retry policy.
Resume prints the final plan after host recovery and before agent provisioning.
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
and active work. The summary shows total run time; each task row shows its own
attempt time in the current invocation, frozen on completion and reset for a new
attempt. After process restart, host-only score recovery has its own elapsed
time; saved execution durations are unchanged. Scored rows show
Mf and LCf rounded to three decimal places. These are wall-clock durations, not
model-inference measurements.
The startup prints report/log paths; interrupted runs print a ready-to-use resume command.
These display settings do not change run identity or task outcomes.

LOG_LEVEL defaults to INFO and changes only user-facing output. Warnings and
errors remain visible at every level; detailed diagnostics and tracebacks stay
in `_meta/invocations/<uuid>/benchmark.log`; `run.log` keeps human lifecycle messages.



### ChatGPT subscription OAuth

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



### Offline rescoring and pricing

```bash
make rescore RUN=./output/RUN_ID
make reprice RUN=./output/RUN_ID PRICING=pricing.yaml
make run CONFIG=examples/full.yaml DATASET=basket.json REPRICE=1 PRICING=pricing.yaml
```

`make rescore` and `make reprice` work offline. They save immutable input-state/report
snapshots and result/receipt under `operations/`, then commit selected refs and
rebuild the report. A failed/uncommitted operation preserves prior selections. `REPRICE=1 PRICING=pricing.yaml` enables automatic pricing
on run/resume. A pricing gap returns a nonzero command exit code while keeping
the saved agent run status and quality scores; console output explains this.
The command reports Python's status through Make (Make generally returns 2
when a recipe fails).



### Local viewing, archives and publication

```bash
make visualize RUN=./output/run-id
make publish RUN=./output/run-id URL=https://viewer.example DRY_RUN=1
make publish RUN=./output/run-id URL=https://viewer.example TOKEN_ENV=PRSIM_VIEWER_TOKEN
make publish RUN=./output/run-id URL=https://viewer.example TOKEN_FILE=/private/publish.token OVERWRITE=1
```

Visualize writes a self-contained `report.html` for one run, with the same
metric and status semantics, dark palette, metric cards and expandable tasks as
the viewer run page. Status selection changes aggregates while all task rows and
their diagnostics remain available. Only text search filters the task list.
Click a task title to expand it; its Original PR/MR link is inside the card.
File buttons open embedded Human/Agent diffs. The report also embeds the safe
OpenCode input, benchmark settings and task source, so copying the single HTML
file preserves these interactions without API access. Source bytes are verified
against the selected attempt before generating the diffs; the HTML can grow
with the amount of diff data. Sorting and unknown measurements are preserved. By default it only saves the HTML file. Set `OPEN_BROWSER=1`
to open it in the default browser. If opening fails, the command reports an error
with the saved file path and returns a nonzero exit code; the HTML remains available.
It preserves report.json. The comparison command is not available.

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



### Scoring, telemetry and report format

The runnable harness validates frozen baskets, prepares complete before-only
Git snapshots, runs one pinned OpenCode agent in private bubblewrap sandboxes,
and scores tracked and nonignored new files with fixed MLP weights. Each retry
starts fresh. A frozen registry and authoritative state select final native/score/price facts;
`report.json` is their projection. All attempts, logs and operations stay inside
the portable run directory. Generated JSON documents use readable UTF-8/indent=2;
native `events.jsonl` stays one compact JSON object per line.

Similarity and local-resource identity intentionally ignore executable bits.
Baskets requiring chmod-only solutions are outside the metric's intended scope.
CodeBLEU checks every pinned grammar before generation; broken imports or parser
APIs fail explicitly. Text parse failures retain whole-triplet fallback to
default MLP, while invalid API results fail and valid zero values remain measurements.

DB telemetry includes root, subagent and detached sessions without retaining
raw message/tool bodies or credential tables. Optional API-equivalent pricing
uses saved request-level usage and frozen policies; original OpenCode cost stays
separate. Unknown usage or pricing remains `null`.

Source code is packaged from `src/` under the `benchmark` namespace:

- `models/` defines typed documents and serialization; `validation/` owns schema checks.
- `runtime/` coordinates attempts, invocations and recovery.
- `execution/` provisions isolated processes and captures native evidence.
- `persistence/` owns durable state, references and filesystem transactions.
- `vcs/` accesses frozen Git objects and isolated workspaces.
- `metrics/` computes scores; `pricing/` calculates policy-bound cost estimates.
- `publication/` builds and uploads archives; `presentation/` produces reports and progress.

Each component owns its models and dependencies. Schemas and conformance fixtures
belong to this project.

Report rows live in `tasks`; sidecar references use `task_id`, and outcomes
store the final row under `task`. Row `id` matches the basket's opaque ID.
Rescore, repricing, telemetry, archives and local HTML consume the same report
structure. For saved data from another release, see the
[compatibility note](../../README.md#reading-data-from-earlier-releases).
