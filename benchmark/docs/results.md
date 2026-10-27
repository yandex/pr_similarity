[PR Similarity](../../README.md) / [benchmark](../README.md)

# Find your results

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
    ├── reprice/<uuid>/                          policy/input snapshots, result/receipt
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

### List selected task states and live locations

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
lock. It does not create an invocation or new DEBUG folder. The HTML command
remain optional, explicit presentations; running an agent creates no Markdown or
HTML report automatically.



### Read task states, scores and retry history

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
