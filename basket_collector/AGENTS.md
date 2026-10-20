# Basket collector

Follow the root guide and this module's README. Use this directory's `.venv`
and `make` interface; this module has no runtime dependency on benchmark/viewer.

- Frozen `basket.json` is the boundary. Preserve exact revisions, source identity,
  workspace and stage lineage; do not hand-edit exports to bypass validation.
- Only v2 configs/checkpoints are accepted. Start v1 collector workflows in a
  new output with a v2 config; preserve frozen v1 baskets for the consumers.
- Follow README resume rules for `RETRY_FAILED`, `ALLOW_PARTIAL` and `RESELECT`.
  Exit code 3 needs inspection, not automatic overwrite. Existing `basket.json`
  can be the previous publication; check selection, coverage and omissions.
- Read saved stage counts/rejection reasons before changing filters. Selection
  limit is a maximum, not a promised cardinality. Resume needs compatible settings.
- Do not silently rebuild or retry paid generation when only config edits were
  requested. Preserve successful tasks and separate output paths as appropriate.
- Code changes: `make test` and `make smoke` are offline. `make smoke-live` needs
  explicit live-run authorization and the documented credentials.

Use `prsim-build-basket` in the root `.agents/skills/` for operator tasks.
