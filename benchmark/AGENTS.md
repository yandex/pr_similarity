# Benchmark

Follow the root guide and this README. Use the module's `.venv` and Make interface.
`DATASET` supplies a frozen basket; agent YAML does not select the basket.

- Accept basket v1/v2 without conversion. Resume needs the original basket bytes;
  use a new run for a new export. Report v2 coverage/omissions separately from
  execution failures; all exported tasks, including partial baskets, are inputs.

- Preserve before-only source isolation, private HOME, exact runtime and durable
  attempt evidence. Never expose after-revision, host credentials or host HOME
  to an agent as a workaround for setup/MCP problems.
- Inspect native config, setup, resource copying and sandbox mounts together when
  documenting integration. Exclusions remove AI changes only, not human targets.
- Start analysis at `report.json`, follow final-attempt references, and keep
  execution, changes, scores and usage independent. Unknown is not zero.
- OAuth profiles persist outside runs. Do not print their contents or delete them
  while another run uses them. Never include them in archives or commits.
- Code changes: `make test` and `make smoke` are offline; real model calls need
  run authorization. Docs/examples can use loaders and bounded sandbox checks.

Use root skills `prsim-run-benchmark`, `prsim-analyze-run` and `prsim-publish-run`
for operator workflows. Live publication requires authorization and receipt readback.
