[PR Similarity](../../README.md) / [viewer](../README.md)

# Reference

- [Authentication and local administration](#authentication-and-local-administration)
- [Administration through Make](#administration-through-make)
- [Storage, layout, backup and recovery](#storage-layout-backup-and-recovery)
- [HTTP API, publication and downloads](#http-api-publication-and-downloads)
- [Filters, task sorting and metric semantics](#filters-task-sorting-and-metric-semantics)
- [Task sorting](#task-sorting)
- [Public run config, limits and archive validation](#public-run-config-limits-and-archive-validation)
- [Configurable limits](#configurable-limits)
- [Source layout](#source-layout)

### Authentication and local administration

Admin API calls use the configured token file. Rotate it by replacing the file
with another owner-only regular file of mode `0400` and restarting the daemon.
`URL` can select the verified HTTPS origin used by administration commands;
plain HTTP administration is accepted only on loopback.

Browser authentication requires `public_origin` to match the HTTPS origin served
by the reverse proxy, for example `https://viewer.example.org`. Loopback HTTP is
accepted for local development; forwarded headers never select the trusted origin.
Sessions use HttpOnly SameSite cookies and hashed SQLite tokens (12-hour default
TTL). Writes require the origin and a short-lived CSRF challenge from
`GET /api/v1/auth/session`. The login dialog directs visitors to the server
administrator for credentials. Change password requires a matching confirmation;
clipboard paste is disabled only in the confirmation field.
Login failures are rate limited. Password reset,
disable and role changes revoke sessions and unused one-use grants. Browser
requests with an explicit invalid Bearer token cannot fall back to a cookie.

An administrator's Users page creates ordinary accounts, enables/disables them
and resets passwords. Roles are read-only in the UI/API. Create the first admin
or change a role **on the service machine, as the runtime owner**, with the local
token file accessible and passwords entered at the hidden prompt:

```sh
make admin-profile-create CONFIG=/path/to/config.json LOGIN=operator
make admin-role-set CONFIG=/path/to/config.json LOGIN=student ROLE=admin
make admin-profile-password-set CONFIG=/path/to/config.json LOGIN=operator
```

Authentication defaults: `session_ttl_seconds=43200`, `login_attempts=10`,
`login_window_seconds=300`, `password_checks=1`. Browser and grant issuance
share the failed-attempt budget. `password_checks` bounds admitted verification
requests and returns 429 when full; the service lock still serializes actual work.

These local provisioning/recovery commands access SQLite, ignore `URL`, and never call a role HTTP
endpoint. The last active administrator cannot be disabled or demoted. SQLite
account upgrades preserve password hashes and save an owner-only backup before
changing the database. Unsupported database formats are rejected. If rolling back
requires a database restore, stop the service and restore its compatible backup.

User-management PATCH/password routes reject admin targets with 403
`protected_admin`, including requests authenticated with the admin token.
The role check and update share a SQLite transaction. Admin rows have no access
or password-reset controls. An administrator can change their own password with
the current password through `/auth/password`; recovery is local provisioning.

### Administration through Make

```sh
make admin-user-create LOGIN=student
make admin-user-disable LOGIN=student
make admin-user-enable LOGIN=student
make admin-password-set LOGIN=student
make admin-basket-order ORDER_JSON='["<full-basket-sha256>"]'
make admin-run-delete RUN_ID='<uuid>'
make admin-refresh
```

Passwords are requested through a hidden prompt, never Make variables or argv.
`CONFIG`, `URL`, `LOGIN`, `ROLE`, `RUN_ID`, `ORDER_JSON`, `PYTHON`, `VENV_PYTHON` and
`PACKAGE_INDEX_URL`, `CHECKPOINT_KEY`, `TARGET_RUNTIME` and `MIGRATION_MAX_OBJECTS`
are supported Make settings. Literal paths/JSON pass through
the environment without shell interpolation. Login is case-sensitive ASCII,
1–64 characters, starts with a letter/digit and otherwise permits `_`, `-`, `.`.
Users can be disabled, not renamed/deleted. Disable/password reset revoke unused
grants; already admitted transfers may finish.



<a id="storage-backup-and-recovery"></a>

### Storage, layout, backup and recovery

New configurations use `publication_storage: "local"`. Publication installs a
validated, synced local version and commits its catalogue entry, receipt and
backup intent in SQLite before returning success. Backup begins after the HTTP
response; a crash retains the intent for the next startup. The receipt confirms
local durability, while `GET /admin/stats` reports backup jobs separately.

The private runtime contains the account database and local immutable versions.
Catalogue reads use SQLite projections without S3 calls. Run-specific reads hold
a lease on one version. If its local files have been evicted, the original HTTP
request waits for the exact archived bundle to be downloaded, checked and
unpacked; there is no 202/polling workflow for readers. Concurrent reads share a
restore of the same version. Cold admission is bounded by the
[restore worker and queue limits](configuration.md#local-publication-and-backups);
a full queue returns `503 restore_busy`. A request exceeding its original deadline
returns `504 cold_read_timeout`. Leaving the last queued reader removes that job;
running restoration continues until its storage operation finishes. Administrative
stats expose active, queued and waiting-reader counts in `restore_jobs`.
A damaged backup or insufficient disk space returns an error rather than partial evidence.

A successful run read extends its idle timestamp. Catalogue listings, refresh
and backup do not. After 48 idle hours, backed-up versions without readers can
be evicted; disk pressure may evict them earlier. Active readers and data without
a verified backup remain protected. `cache_max_bytes`, `pending_backup_max_bytes`
and `disk_reserve_bytes` bound admission and disk use; see
[configuration](configuration.md#local-publication-and-backups).

S3 stores whole original archives, separate versioned metadata and closing
manifests. A CAS-updated registry identifies the current closed bundles and
tombstones. Incomplete uploads are not readable backups. Immutable bundle objects use
conditional create; registry changes use an observed ETag with `IfMatch`.
The backend must support both `PutObject` conditions. Unsupported endpoints fail
without an unconditional-write fallback. Archive SHA-256 is verified from bytes;
an ETag does not replace that check.

```text
bundle-data/v1/...          original archives and versioned metadata
bundle-index/v1/...         closing bundle manifests
bundle-registry/v1/...      current registry head and immutable snapshots
checkpoint-data/v1/...      private SQLite checkpoint payloads
checkpoints/v1/...          closed recovery manifests
```

Owner name/tags/pin edits create new backup metadata without changing archive
bytes. Overwrite retains the original owner, basket and presentation metadata;
new content becomes a separate generation. Deletion records a tombstone so stale
backups cannot resurrect a run. Historical objects are not automatically purged
by the viewer. Do not apply the legacy trash lifecycle rule to bundle, registry
or checkpoint prefixes.

#### Refresh and background status

`make admin-refresh` queues a full verification of every current registered
bundle, including unchanged archive bytes. HTTP success acknowledges the job;
inspect `GET /api/v1/admin/stats` and its `refresh_job` until `status` is
`succeeded`. `partial` and `failed` do not mean that all backups were verified.
The job reports run/object identity, phase, progress and safe errors. A saved
incomplete request can be resumed through `POST /api/v1/admin/refresh/{request_id}/resume` with an empty body or `{}`.

Refresh validates a bounded registry snapshot, updates verified local data and
catalogue entries, and preserves newer local publications or metadata awaiting
backup. When owner, run, generation and archive SHA-256 match, it repairs local
files while retaining newer local names, tags, pin state and queued metadata
backups. Verifying an older remote metadata revision does not confirm the newer
local revision. Refresh never writes S3 or extends cache TTL. A failed backup check protects
the last valid local copy from eviction. Reads continue from available versions
while the audit runs. Newly admitted publication, overwrite or deletion can
return `503 run_updating` for the affected run; unrelated runs remain readable.

`backup_jobs` counts durable backup states. Network/temporary provider errors
are retried; invalid credentials and denied access leave the job `blocked`. Correct the
authorization problem, then request `POST /api/v1/admin/runs/{run_id}/backup/retry`
with administrator authentication and an empty body or `{}`. The response reports
how many jobs were scheduled; check `backup_jobs` afterward. This does not change
the publication or steal an active upload lease. A blocked job is not a completed
backup.
S3 failure logs include method, hashed object key, canonical run UUID when known,
HTTP status, provider error code, request ID, elapsed time and SDK retry count.
They omit raw keys, request bodies, credentials and provider error messages.

#### Checkpoints and offline restore

The server creates a private recovery checkpoint daily by default
(`checkpoint_interval_seconds=86400`) and retries failed attempts after
`checkpoint_retry_seconds=300`. An administrator can request one immediately
with `POST /api/v1/admin/checkpoint`; inspect `checkpoint_job` in
`GET /api/v1/admin/stats` for `status: succeeded` and its `manifest_key`.
The request returns before remote work completes.
The schedule survives restarts, including before the first checkpoint. An overdue
checkpoint runs on the next maintenance pass after startup.

A closed checkpoint includes a consistent SQLite backup with users, password
hashes, ownership, receipts, settings and catalogue state, plus verified archive
bundle dependencies. Keep its bucket and runtime private. Runs without an
available remote payload are recorded explicitly; a database checkpoint alone
cannot recover their local-only bytes after a lost disk.

Restore while the viewer is stopped, into a new absent runtime directory whose
parent directory already exists:

```sh
make admin-checkpoint-restore CONFIG=/path/to/config.local.json \
  CHECKPOINT_KEY=checkpoints/v1/CHECKPOINT_ID/manifest.json \
  TARGET_RUNTIME=/path/to/new-runtime
```

The command verifies the closed manifest, database and declared dependencies.
It refuses an existing target. Check its `restored_runs` and `lost_runs` output
before switching configuration to the restored runtime; missing payloads stay
unavailable rather than appearing as valid runs. Point `runtime` and
`admin_token_file` at the new private runtime, then start the service and verify
accounts, run reads and archive hashes. Restore never overwrites a live database.
Keep the original service stopped: only one runtime may write to this storage prefix.

The restored service starts with a durable write barrier. Local catalogue and cached
reads remain available without S3; publication, run metadata changes, deletion and
backup jobs stay blocked until reconciliation succeeds. Run `make admin-refresh`
with the restored configuration, then inspect `GET /api/v1/admin/stats`:
`restore_reconciliation.status` must be `reconciled`. This requires a fresh, complete
verification of the authoritative remote registry and its bundles, including newer
generations and metadata revisions. Before writing backup objects, the service reserves
allocation floors in remote storage. Reconciliation persists those floors locally so
new writes skip generations and metadata revisions from unregistered partial backups.
Missing or corrupt reservations keep writes blocked. A failed or partial refresh, or a registry that
changes during verification, keeps the barrier across restarts. Start another refresh
if the registry changed; an interrupted recovery refresh can be resumed by its new
request ID. A refresh saved inside the checkpoint cannot unlock the restored service.
Saved deletion intents above the verified remote generation resume automatically
after reconciliation; they do not need archive bytes or a manual backup retry.
Jobs whose archive payload was lost remain blocked.

Account administration remains available during reconciliation. If refresh reports
`owner_missing`, provision the original account through Users or the administrative
API and rerun refresh. Uncommitted uploads from the old runtime must be submitted
again after reconciliation. Preserve the old runtime and original archives until
recovery is confirmed.

#### Upgrading an existing viewer

An omitted `publication_storage` keeps legacy mode. Finish legacy recovery with
the old configuration before migration; unresolved journal operations prevent
import. Stop the service and preserve its complete runtime and a consistent
SQLite backup. Keep the original S3 prefix unchanged.

Set `publication_storage: "local"` in a private copy of the configuration, keeping
the existing runtime, account database and backend, then run:

```sh
make admin-local-import CONFIG=/path/to/config.local.json
```

The offline importer validates each legacy archive and preserves its exact bytes,
run UUID, original owner, presentation metadata, settings and historical receipts.
It records progress in SQLite; after an interrupted import, repeat the command
with the same runtime and backend. `MIGRATION_MAX_OBJECTS` bounds discovery
(default `100000`). Complete the import before starting local mode. Do not
remove its journal or change the backend to bypass a failed validation.

Start with the local configuration, verify the catalogue, owner/admin login and
original archive hashes, then check backup and checkpoint status. Retain the old
source installation and pre-upgrade backup until acceptance is complete. Older
writers must not open the upgraded database. Returning to a pre-upgrade snapshot
after new publications would lose those changes and requires a separate recovery
plan. Source replacement alone does not migrate user state.

#### Legacy storage

Legacy mode retains its existing reader and journal recovery. Its object layout
uses `runs/<owner>/<run_id>/{archive.tar.gz,data/...,publication.json}`,
`trash/<owner>/<run_id>/<timestamp>/...` and `settings/...`. It validates its
catalogue at startup and may need S3 access before becoming ready. Transferred
journal files are checked in storage; remaining transfers require their staging
files or a verified previous version. Preserve that state until recovery and
migration finish.

The optional [90-day trash lifecycle example](../ops/trash-lifecycle.example.json)
applies only to the legacy `trash/` prefix. The new publisher requires
`local-durable-v1` and does not silently publish through a legacy server. Recover
pending legacy uploads using their producing installation.

Settings still controls ordered, collapsed and hidden baskets. Hidden entries
are excluded from catalogue lists and filters, but direct run/task/config/diff
links remain public. Layout writes require `expected_revision`; stale saves
return 409 and require reload. The readiness endpoint reports initialization
status; in local mode a compatible persisted catalogue can start without S3.


### HTTP API, publication and downloads

Paths below have prefix `/api/v1`, except `GET /healthz`. Lists return
`items`, `total`, `offset`, `limit`; default limit 50, maximum 200. Errors are
`{"error":{"code":"...","message":"..."}}`, with no traceback or uploaded logs.
The home UI requests 25 runs per basket. Pagination appears only above 25
matching runs; if an old page offset becomes empty, it returns to the first page.
Catalogue tag groups show five tags initially, with the remainder under `+N`;
run-detail groups retain their seven-tag preview.

| Access | Method and path | Body / result |
|---|---|---|
| Anonymous | GET `/baskets` | ordered baskets, counts and filter options |
| Anonymous | GET `/baskets/{hash}/runs` | filtered, sorted current rows |
| Anonymous | GET `/runs/{run_id}` | metadata and selected aggregates |
| Anonymous | GET `/runs/{run_id}/config` | safe native OpenCode input |
| Anonymous | GET `/runs/{run_id}/tasks` | all tasks; status checkboxes do not hide them |
| Anonymous | GET `/runs/{run_id}/tasks/{task_id}` | condition, task score and file list |
| Anonymous | GET `/runs/{run_id}/tasks/{task_id}/files/{file_id}` | human/agent diffs and file score |
| Admin | PUT `/runs/{run_id}/pinned` | `{"pinned":true}`; explicit assignment |
| Anonymous | GET `/publication-capabilities` | supported publication protocols and operation-status support |
| Authorized publisher | GET `/publication-operations/{idempotency_key}` | actor-bound phase and receipt; requires run_id/archive_sha256/report_sha256 query fields |
| User | POST `/auth/publish-token` | `{"login":"...","password":"..."}` -> token/expires_at |
| Publisher | PUT `/runs/{run_id}?overwrite=0\|1` | original gzip archive with owner grant |
| Owner or admin | POST `/auth/download-token` | login/password/run_id -> token/expires_at |
| Owner or admin | GET `/runs/{run_id}/archive` | original latest tar.gz bytes |
| Owner | POST `/auth/metadata-token` | login/password/run_id -> token/expires_at |
| Owner | PATCH `/runs/{run_id}/metadata` | display_name and/or ordered tags |
| Owner or admin browser session | DELETE `/runs/{run_id}?expected_publication_sha256=<sha256>` | delete own run, or any run for admin; CSRF and current publication required |
| Anonymous | GET `/auth/session` | actor or guest, expiry and CSRF challenge |
| Browser | POST / DELETE `/auth/session` | explicit login/password or logout; CSRF required |
| User or admin | PUT `/auth/password` | current/new password; revokes all sessions |
| Admin | GET `/admin/users` | users and enabled state; `details=1` adds role |
| Admin | POST `/admin/users` | login/password; existing login -> 409 |
| Admin | PATCH `/admin/users/{login}` | `{"enabled":false}` |
| Admin | PUT `/admin/users/{login}/password` | new password; revoke unused grants |
| Admin | GET / PUT `/admin/baskets/layout` | full layout and revision; PUT requires expected_revision |
| Admin | GET `/admin/stats` | cached basket/run/user counts and last refresh |
| Admin | PUT `/admin/baskets/order` | `{"items":["<hash>",...]}`; complete active set |
| Admin | DELETE `/admin/runs/{run_id}` | delete the current publication; local mode records a tombstone |
| Admin | POST `/admin/refresh` | queue new full verification; empty body or `{}` |
| Admin | POST `/admin/refresh/{request_id}/resume` | resume the saved boundary of an incomplete local audit |
| Admin | POST `/admin/runs/{run_id}/backup/retry` | requeue blocked or delayed backup work after storage access is repaired |
| Admin | POST `/admin/checkpoint` | queue a private recovery checkpoint; inspect checkpoint_job in stats |

User grants are one-use Bearer tokens with TTL 60 seconds at admission. Local
publication requires `X-Publication-Protocol: local-durable-v1` in addition to
`Content-Type: application/gzip`, `Idempotency-Key`, `X-Archive-SHA256` and
`X-Report-SHA256`. Capabilities advertise this protocol only for local mode.
Operation status is authenticated but does not consume the grant; phases are
`receiving`, `extracting`, `validating`, `committing`, `available` or `rejected`.

Only `available` returns the final receipt. It binds the run, archive/report
hashes and idempotency key, and includes `protocol`, `durability: local_durable`,
`operation_id`, `phase`, `message`, `author_login` and `actor_login`.
Terminal replay is checked before overwrite CAS and returns the original receipt
without uploading bytes again. Another identity or actor is rejected.

Download consumes its owner grant at admission. Range is ignored: one response
returns the complete latest archive. An interrupted download needs a new token
and a full retry from the start. Administrators may download any archive using
their browser session, local admin token or one-use download grant. Ordinary
publication and metadata edits require an owner grant. PATCH omits unchanged fields;
`tags: []` clears tags. Pinned is separate.

`GET /healthz` returns `{"status":"ok"}` without querying S3. There are no compare,
Judge, raw report/log, online restore or purge routes.

For owner operations, send login/password as JSON over HTTPS using a client that
keeps them in memory (for example Python `getpass` plus `urllib.request`). Use the
returned token in the request headers without printing it or putting it into URL,
argv, committed files or logs. No download command is added to benchmark.



### Filters, task sorting and metric semantics

Read endpoints accept `statuses=OK,no-op,timeout,fail`, `pr=Mpr|FCpr|LCpr`,
`file=Mf|LCf`. Defaults: OK/no-op/timeout, LCpr, LCf. An explicit empty statuses
value selects no tasks and produces null aggregates. Each task keeps its original
outcome/score regardless of selection. LCpr weights PRs by sum `loc_weight`, with
`max(1,human_LOC)` per human file. F1 is micro-F1 over selected scored tasks.

Run filters use repeated `user`, `model`, `effort`, `setup`, `basket` values:
OR within a filter, AND between filters. Other queries: `q` literal case-insensitive
substring, `pinned=1`, UTC `from` and exclusive `to`, `sort`, `direction=asc|desc`,
`offset`, `limit`. Sort keys: score, f1_files, display_name, created_at,
selected_tasks, time, cost, total_tokens, cache_read, input, output, cache_write, reasoning.
The UI offers Total Tokens for token sorting, using the selected aggregate
`tokens.total.amount`. Category sort keys remain available to existing API clients.
Unknown values sort last in either direction; ties use creation time and run_id.

Model labels and the Model filter use structured configured native inference:
`provider/model [effort]` for a known level, otherwise `provider/model`. There is
no separate Effort column or UI filter. An internal effort tag is present only
when known; the API can filter it through `effort`. Generated tags never come
from basket names, generator names or user prefixes.
All user strings, including `model:custom`, remain in Setup with their original
order/duplicates. Benchmark top-level YAML `tags` supplies initial Setup, and
owner metadata edits can change server Setup later. Unknown/unsupported effort
stays explicit; effective values show their source and mixed/discrepancy status.
Captured runtime configuration does not prove an option reached/applied at the
provider. Basket generator provenance remains a separate population descriptor.
`+N` expands tags in place. Metric selection survives navigation in browser session
storage; it creates no account or private collection. Reset returns to defaults.

The catalogue's Cost column shows USD amounts with two decimal places and a
trailing dollar sign (for example, `0.01$`); unknown values remain `—`.
API amounts and sorting retain their original precision.

One Cost uses complete reprice when present; partial/unavailable reprice remains
null without OpenCode fallback. Otherwise native OpenCode cost is used. Tooltip
shows sources, coverage and known subtotal. Token categories are independent,
in order cache read/input/output/cache write/reasoning. Median time uses known
final execution durations, excluding setup and previous retries. Unknown is null,
not zero. The catalogue caches compact views for each status selection and reads
full reports/file contents on demand.

### Task sorting

Run pages sort tasks by Score, Total Tokens or Agent Time in either direction,
with an explicit original-basket-order reset. UI defaults to Score descending;
API requests without `sort` retain the original order. Task sorting has separate
session storage from catalogue sorting and keeps expanded tasks and the run URL.
All tasks remain listed regardless of the statuses used for run aggregates.
File aggregation changes task scores and Score ordering. Unknown scores/tokens/
time stay last in both directions, zero is known, and ties keep basket order.
Scores use raw precision; Total Tokens uses the reported total; Agent Time uses
only the final execution duration. Setup and earlier attempts are excluded.

`GET /runs/{run_id}/tasks` supports `sort=task_order|score|total_tokens|time` and
`direction=asc|desc`, sorts the complete list before offset/limit, and adds
`sort`, `direction`, `report_sha256`, `publication_sha256`. Basket order has effective direction asc.
Run responses also include both hashes. Publication SHA-256 is the exact archive
digest: unlike report JSON alone, it binds triplet contents too. Optional
`expected_report_sha256` and `expected_publication_sha256` on task list/detail/diff
guard against changed content: 409 `run_changed`, or 404 after
deletion. The UI offers a full run reload while preserving sort/expansion and
discards late requests. Catalogue cost/time/category sorts keep their contract.
PR/MR links accept HTTP(S) without userinfo and use `noopener noreferrer`.



### Public run config, limits and archive validation

The opencode.json button opens the frozen native `agent/opencode.json`, once per run.
Model/options, ordered permissions, skills, instructions and supported integrations
keep their native structure. Paths to declared resources are relative to the
archive's `agent/` directory. Runtime, timeout, setup timeout, retries and diff
exclusions are separate run-page metadata. Effective model facts and their
origins remain available through the run API; they do not replace the input.

`GET /runs/{run_id}/config` returns the native config, its supported state and a
verified `source` receipt. It includes report/publication
hashes; expected-hash query parameters reject stale content with 409. Literal
credentials, private host paths and unsupported native fields refuse public
projection instead of silently changing the displayed config. Environment
references remain references. The full input remains in owner/admin archives.
The display bound is `limits.config_display_bytes=65536`; it depends on actual
native input size, not on the number of tasks or attempts.

### Full-file and task-change views

`GET /runs/{run_id}/tasks/{task_id}/files/{file_id}?view=full` returns
`human_file` and `agent_file` instead of unified diffs. Each contains ordered
`lines` with `kind` (`context`, `added`, `removed`), literal `text`, and nullable
`base_line`/`result_line` numbers, plus `state`, `truncated` and
`ends_with_newline`. Source and rendered rows obey `diff_bytes` and `diff_lines`.
The default endpoint still returns `human_diff` and `agent_diff`.

`GET /runs/{run_id}/tasks/{task_id}?view=changes` returns `human` and `agent`
raw-line totals: `added`, `removed`, `files`, `measured_files` and `status`.
Files over display limits are excluded from the line subtotal, producing
`partial` or `unavailable`; changed-file counts remain known. Computation runs
outside the catalogue lock and checks report/publication identity before and
after source reads. Both views accept the existing expected-identity parameters.

The config response includes `setup_summary`, derived from the hash-bound
configuration identity without exposing environment values.
The config button displays the verified input `opencode.json`.


Admin basket-layout reads include each basket's description alongside its name.
The layout revision and save contract are unchanged.


### Configurable limits

`limits` in configuration overrides named defaults from `viewer.config.Limits`:
1 GiB compressed / 8 GiB inflated archive, 100000 files, 256 MiB/member,
64 MiB/JSON artifact, ratio 1000, 4096 UTF-8 path bytes, 1 MiB/API JSON body,
256-character display name/tag, 128 tags, 900-second transfer/validation deadline,
10 GiB free staging before import. Metadata headers are bounded to 64 KiB;
basket name/runtime to 256 characters, basket description to 8192, task title to
1024. A displayed diff side is bounded to 8 MiB and 20000 source lines, with an
explicit truncated flag. All numbers are adjustable; defaults target a small VM.

Tar import streams counters and hashes; no whole archive is loaded into RAM.
It rejects links, sparse/special files, unsafe/duplicate paths, oversized extended
headers, inconsistent manifests/scores and cross-attempt/log aliases in public
triplet references. Nested archives are preserved as bytes and never unpacked.
Uploaded code/HTML is never executed or served as an origin page. Public output
is an explicit JSON projection; logs and session DBs remain only in owner archives.

Consumer schemas and frozen producer fixtures belong to this package. Their
source SHA-256 values are recorded in `tests/fixtures/contract-fixtures.json` for
intentional updates and conformance review. Python source hashes are not part
of persisted run or metric identity.

The viewer uses Pydantic models internally after document validation. Readers
check that the report and its saved evidence are consistent within each archive.
Model serialization preserves missing fields, explicit nulls and JSON number types;
frozen artifact bytes are retained as received. Open JSON fields use the existing
parser and schema checks without adding a separate model nesting limit.




## Source layout

- `src/models/`: object models for frozen documents and serialization.
- `src/validation/`: schemas, archive validation, ownership and provenance checks.
- `src/presentation/`: metrics, sorting and public result projections.
- `src/web/`: HTTP routes, authentication and middleware.
- `src/persistence/`: SQLite state and object-store access.
- `src/service.py`: legacy publication and recovery.
- `src/local_service.py`, `local_repository.py`, `local_files.py`: durable local versions, catalogue and reader leases.
- `src/bundles.py`, `local_jobs.py`, `local_refresh.py`: archive backups, registry and full verification.
- `src/local_checkpoint.py`, `local_migration.py`: private checkpoints, offline restore and legacy import.
- `src/publication.py`: shared validation of staged and stored evidence.
- `src/catalogue.py`: read-only catalogue construction and coherent versions.
- `src/maintenance.py`: recovery, refresh scheduling and shutdown.
- `src/cli.py`: command-line entry points and local lifecycle.

The Python modules are internal implementation details. HTTP endpoints, CLI
commands and saved data are the integration boundaries.
