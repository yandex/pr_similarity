# PR Similarity

Read the root README and the owning module's README/AGENTS.md before changes.
This directory is also the intended standalone repository root: keep shared
documentation and skills portable, without internal services or absolute host paths.

- `basket_collector` owns task discovery, filtering, generation and frozen exports.
- `benchmark` owns source-only sandbox execution, scoring, evidence and publication.
- `viewer` owns the public UI/API, accounts and SQLite/object-store persistence.
- Each module has its own `.venv` and Make interface. Run commands from that module;
  use `make help` to verify supported variables. Do not widen dependency setup.
- Keep credentials, private profiles, caches, deployments and run artifacts out
  of commits. Preserve unrelated operator configs and active runs.
- Changes to a file/API contract require checking its producers and consumers.
  A successful process exit alone does not prove evidence or publication is valid.
- Existing authorization determines live runs, paid generation, uploads, account
  changes and deployment. A docs/config request alone does not authorize those.
- Validate docs with local links, config loaders and bounded offline examples.
  For runtime changes, run the owning README's tests/smoke as applicable.

Operator workflows live in `.agents/skills/`: `prsim-build-basket`,
`prsim-run-benchmark`, `prsim-analyze-run`, `prsim-publish-run`. Their details
point to module READMEs. These are repository skills, not automatic additions
to the benchmarked agent's configuration.
