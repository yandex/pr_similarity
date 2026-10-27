# PR Similarity

Read the root README and the owning module's README/AGENTS.md before changes.
This directory is also the intended standalone repository root: keep shared
documentation and skills portable, with deployment-neutral examples and paths.

- `basket_collector` owns task discovery, filtering, generation and frozen exports.
- `benchmark` owns source-only sandbox execution, scoring, evidence and publication.
- `viewer` owns the public UI/API, accounts and SQLite/object-store persistence.
- Each module has its own `.venv` and Make interface. Run commands from that module;
  use `make help` to verify supported variables. Do not widen dependency setup.
- Keep module READMEs focused on prerequisites, quick start, configuration and
  checks. Detailed public workflows belong in each module's `docs/` directory.
- Keep application configuration examples in each module's `examples/` directory.
  `basket_collector/examples/full.yaml`, `benchmark/examples/full.yaml` and
  `viewer/examples/full.json` list all owned settings; update them when fields change.
  The benchmark baseline uses `openai/gpt-6.1-sol`, medium reasoning, ChatGPT OAuth
  and `tags: [baseline]`, without setup or additional skills/rules.
- Keep credentials, private profiles, caches, deployments and run artifacts out
  of commits. Preserve unrelated operator configs and active runs.
- Keep contract versions in their owning schemas and loaders. User documentation
  should describe workflows, with release history in CHANGELOG.md and historical
  input support in the README's compatibility note.
- Preserve frozen input bytes and hashes. New exports need new runs; do not
  rewrite existing artifacts or bypass resume compatibility checks.
- Changes to a file/API contract require checking its producers and consumers.
  A successful process exit alone does not prove evidence or publication is valid.
- Existing authorization determines live runs, paid generation, uploads, account
  changes and deployment. A docs/config request alone does not authorize those.
- Validate docs with local links, config loaders and bounded offline examples.
  For runtime changes, run the owning README's tests/smoke as applicable.

Operator workflows live in `.agents/skills/`: `prsim-build-basket`,
`prsim-run-benchmark`, `prsim-analyze-run`, `prsim-publish-run`. Their details
point to module READMEs and their linked guides. These are repository skills, not automatic additions
to the benchmarked agent's configuration.
