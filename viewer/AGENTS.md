# Viewer

Follow the root guide and this README. Use this directory's `.venv` and Make
interface. Local/mock tests do not authorize deployment, account or storage changes.

- Validate exact bindings and coherent evidence within each archive. Preserve
  original archive bytes and supported historical reads; do not convert inputs.
  Partial basket omissions are distinct from benchmark execution failures.
- Preserve publisher API/archive compatibility with benchmark; verify producer
  payloads as well as viewer responses when changing that boundary.
- Keep public reads, owner operations, user accounts and administrator actions
  distinct. Protect private runtime/SQLite credentials and anonymous data policy.
- Deployment state/config/runtime belong outside tracked source. Backups and
  recovery must preserve accounts, original archives and publication receipts.
- Code/UI changes: use `make test`; browser tests require `make browser-setup`
  and system libraries described by the README. Docs-only checks can be scoped.
- Do not restart, redeploy, refresh storage or alter an account during read-only
  diagnosis without task authorization. Check transport/TLS before blaming login.

Publication instructions are in benchmark's README and root `prsim-publish-run`.
