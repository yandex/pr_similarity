[PR Similarity](../../README.md) / [benchmark](../README.md)

# Connect MCP and RAG

Native OpenCode MCP config and sandbox-visible CLI tools are supported. Network
access is shared with the host, while filesystem access is limited:

| Inside bubblewrap | Access / lifetime |
|---|---|
| `/repo` | Writable before-only source; cwd is the basket workspace |
| `/repo/.git` | Read-only fresh local baseline, without original PR history |
| `/sandbox-home` (`$HOME`) | Writable private state shared by setup/agent within the attempt |
| `/benchmark-runtime` | Read-only config, setup and copied declared resources |
| `/benchmark-bin/opencode` | Read-only pinned OpenCode executable |
| `/benchmark-python` | Read-only base Python snapshot, without host or benchmark packages |
| `/sandbox-home/.venv` | Writable Python environment private to this attempt |
| `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64` where present | Read-only host system tools/libraries |
| Selected DNS/TLS files from `/etc` | Read-only name resolution and certificate support |
| `/etc/alternatives` where present | Read-only links for host system libraries, including BLAS/LAPACK |
| `/tmp` | Fresh writable tmpfs for each individual invocation |

System libraries still come from the host. Mounting `/etc/alternatives` preserves
their symlink chains into the exposed system directories; it does not install
project dependencies or expose the whole `/etc`. Targets outside those directories
remain unavailable. Install project dependencies in setup.

There is **no YAML option for arbitrary host bind mounts**, a host HOME, Docker
socket, custom PATH, or a persistent setup daemon. The launcher owns `HOME`,
`PATH`, `XDG_*`, `OPENCODE_*`, `GIT_*`, `LD_*`, `VIRTUAL_ENV` and Python
import/prefix controls (including `PYTHONPATH` and `PYTHONHOME`); config bindings cannot replace
them. A host tool under your HOME, host virtualenv or host `/tmp` is not visible.
Install it into the private HOME, package nonsensitive files as declared local
resources, or fetch pinned inputs in setup. Copied local resources reject symlinks.

### Remote MCP, local MCP and a retrieval CLI

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

**RAG through a CLI.** The complete [custom-tools.yaml](../examples/custom-tools.yaml)
uses [setup-custom-tools.sh](../examples/setup-custom-tools.sh) to install `doc-search`
under `$HOME/.local/bin`, already on the fixed PATH.
[agent-custom-tools.md](../examples/agent-custom-tools.md) tells the agent how to
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
[MCP schema for the bundled runtime](https://github.com/anomalyco/opencode/blob/v1.18.33/packages/core/src/v1/config/mcp.ts)
and [skills schema](https://github.com/anomalyco/opencode/blob/v1.18.33/packages/core/src/v1/config/skills.ts).
Newer runtime documentation may describe a different config shape.
