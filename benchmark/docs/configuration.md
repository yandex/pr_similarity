[PR Similarity](../../README.md) / [benchmark](../README.md)

# Configure your agent

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
shown in [openai-endpoint.yaml](../examples/openai-endpoint.yaml).

### Skills, rules, setup and diff exclusions

[openai-tdd.yaml](../examples/openai-tdd.yaml) is a complete skill/rules example:
`skills.paths` points to the vendored Superpowers TDD skill, and `instructions`
points to [agent-tdd.md](../examples/agent-tdd.md). Your skill folder needs a valid
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

Use the provided `$HOME/.venv` for Python dependencies, `$HOME/.local/bin`
for other tools and `$HOME/index` for retrieval indexes. The baseline is recorded **before setup**: writes under
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
