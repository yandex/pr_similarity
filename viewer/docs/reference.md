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
`PACKAGE_INDEX_URL` are supported Make settings. Literal paths/JSON pass through
the environment without shell interpolation. Login is case-sensitive ASCII,
1–64 characters, starts with a letter/digit and otherwise permits `_`, `-`, `.`.
Users can be disabled, not renamed/deleted. Disable/password reset revoke unused
grants; already admitted transfers may finish.



<a id="storage-backup-and-recovery"></a>

### Storage, layout, backup and recovery

```text
runs/<user>/<run_id>/archive.tar.gz       exact original bytes
runs/<user>/<run_id>/data/...             verified archive members
runs/<user>/<run_id>/publication.json     server marker, written last
trash/<user>/<run_id>/<timestamp>/...     previous/deleted publication
settings/basket-order.json               ordered active basket hashes
settings/basket-display.json             versioned hidden/collapsed flags
settings/viewer-metadata.json            names, ordered tags, pinned, row dates
```

Users, salted scrypt password hashes, hashed grants, owner reservations, receipts
and recovery records live in the host SQLite database, not S3. The runtime is
`0700` and the database is `0600`. Back up SQLite with its backup API, not by
copying a live database file without its WAL. For example, after stopping the
service, create an owner-only backup using Python's `sqlite3.Connection.backup`.

Overwrite first saves the old data to one fixed trash prefix, then installs the
new publication. It requires the same owner and basket hash. UI metadata and
basket order remain intact. DELETE uses the same move mechanism, drops the UI
metadata and removes an empty basket. No metadata dump is added to trash.
The journal recovers interrupted writes before startup/refresh; if staging is
lost before publication, the previous publication is restored from complete trash.
Original archive bytes and receipt hashes are unaffected by UI edits.

Settings controls basket order, initial collapse and hiding from home. Hidden
baskets and their runs are excluded from `GET /baskets`, including all filter
options, counts, search, pinned selection and pagination. Direct basket/run,
task, config and diff links remain public; Settings still lists every basket.
`GET /admin/baskets/layout`
returns revision and ordered items; PUT takes `expected_revision` and the full
`items` set. Stale saves return 409 and require an explicit reload. Order and flags
commit through one recoverable journal operation; the Make order target
preserves flags. Overwrite preserves layout; deleting the last run removes active
basket settings. The operation envelope is consumable by the pre-layout viewer
on rollback, which preserves order/metadata and ignores display flags.

Settings also shows cached counts and last refresh time from `GET /admin/stats`.
Refresh is synchronous; the shared lock can delay public reads during S3 I/O.
The UI disables duplicate refresh and offers explicit retry. Failed refresh keeps
the last successful in-memory catalogue. Counts do not scan S3 for bucket size.

Use [the standard S3 lifecycle example](../ops/trash-lifecycle.example.json),
substituting your common prefix. Configure object expiration **90 days** after insertion for `<prefix>/trash/`
in your object store. Viewer never purges trash or restores old versions through
an API. Native bucket versioning is optional; if enabled, the example also removes
noncurrent versions after one day and expired delete markers. Current objects
expire at 90 days; physical cleanup can therefore take longer. Lifecycle is asynchronous.
To restore manually, stop writes, copy the chosen trash contents back to its
original runs prefix and call `admin-refresh`. Renamed/tags/pinned metadata removed
by DELETE is not restored; defaults come from the archived report.



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
| User | POST `/auth/publish-token` | `{"login":"...","password":"..."}` -> token/expires_at |
| User | PUT `/runs/{run_id}?overwrite=0\|1` | original gzip archive with publish grant |
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
| Admin | DELETE `/admin/runs/{run_id}` | move to trash; remove UI metadata |
| Admin | POST `/admin/refresh` | reread S3/settings; empty body or `{}` |

User grants are one-use Bearer tokens with TTL 60 seconds at admission. Publish
validation failure/409 does not consume the grant. The benchmark's existing
publish protocol is supported: `Content-Type: application/gzip`, `Idempotency-Key`,
`X-Archive-SHA256`, `X-Report-SHA256`, and its receipt fields. Retrying the same
key/fingerprint with fresh authorization returns the existing receipt without
another overwrite or consuming the fresh grant. Another fingerprint -> 409;
pending recovery/storage outages -> 503 with Retry-After.

Download consumes its owner grant at admission. Range is ignored: one response
returns the complete latest archive. An interrupted download needs a new token
and a full retry from the start. Administrators may download any archive using
their browser session, local admin token or one-use download grant. Publish and
metadata edits still require an owner grant. PATCH omits unchanged fields;
`tags: []` clears tags. Pinned is separate.

`GET /healthz` returns `{"status":"ok"}` without querying S3. There are no compare,
Judge, raw report/log, restore or purge routes.

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

See Run Config opens the frozen native `agent/opencode.json`, once per run.
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
- `src/service.py`: publication and catalogue coordination.
- `src/cli.py`: command-line entry points and local lifecycle.

The Python modules are internal implementation details. HTTP endpoints, CLI
commands and saved data are the integration boundaries.
