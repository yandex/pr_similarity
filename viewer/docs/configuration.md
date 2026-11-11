# Viewer configuration

[Viewer](../README.md) / Configuration

[examples/full.json](../examples/full.json) lists every supported field with
explicit values. JSON does not allow comments, so field meanings are documented
here. Copy it to `config.local.json`, customize it and pass `CONFIG=config.local.json`.
Use [mock.json](../examples/mock.json) for local storage or
[s3.json](../examples/s3.json) for a minimal deployment config.

Paths resolve from the process working directory, **not the config directory**.
Use absolute paths in a service installation. Run the local examples from
`viewer/`; keeping them in `examples/` does not move the runtime there.

## Server and storage

| Field | Meaning |
|---|---|
| `runtime` | Persistent private directory for SQLite, locks, cache and staging |
| `admin_token_file` | Private local administration token file; startup creates it |
| `host`, `port` | Listener address and port |
| `storage` | Backup backend: `s3` for object storage; `mock` for local development |
| `publication_storage` | `local` for durable local publication and archive backups; omitted fields retain `legacy` mode |
| `bucket` | Existing private S3 bucket; unused with mock storage |
| `endpoint`, `region` | Optional S3 endpoint/region; null uses SDK defaults |
| `prefix` | Optional normalized object-key prefix to isolate this instance |
| `tls_cert`, `tls_key` | Certificate and private-key paths for built-in TLS; set both or neither |
| `public_origin` | Exact browser HTTPS origin, including a nondefault port; loopback HTTP is allowed |

The full example assumes an HTTPS reverse proxy in front of the loopback
listener. To serve TLS directly, set both certificate paths and the matching
origin/port. Storage credentials come from the environment or standard boto3
credential chain; do not add credentials to JSON.

## Local publication and backups

The supplied examples select `publication_storage: "local"`. `storage` chooses
its backup backend. Switching an existing runtime requires the explicit
[import procedure](reference.md#upgrading-an-existing-viewer); changing this field
alone does not migrate legacy publications. S3 must implement conditional
`PutObject` with `IfNoneMatch` and `IfMatch`; unsupported endpoints are rejected.

| Field | Default and meaning |
|---|---|
| `cache_idle_ttl_seconds` | `172800` (48 hours) since successful run access before eviction eligibility |
| `cache_cleanup_interval_seconds` | `300`; interval between local eviction passes |
| `catalogue_cache_bytes` | `8388608` (8 MiB); serialized-byte budget for cached public catalogue responses |
| `catalogue_cache_entries` | `128`; maximum query variants, keyed by catalogue revision and in-progress run identities |
| `report_cache_bytes` | `67108864` (64 MiB); sum of source JSON bytes retained in the parsed report LRU; Python objects use additional RAM |
| `report_cache_entries` | `16`; maximum parsed reports; cache identity includes the immutable local generation and path |
| `cache_max_bytes` | `21474836480` (20 GiB); local cache budget |
| `pending_backup_max_bytes` | `17179869184` (16 GiB); admission budget for data awaiting backup |
| `disk_reserve_bytes` | `1073741824` (1 GiB); disk space kept free; zero disables this reserve |
| `backup_workers` | `2`; concurrent archive-backup workers |
| `restore_workers` | `2`; concurrent cold restores |
| `restore_queue_limit` | `4`; additional pending restore slots beyond the worker slots; zero disables this extra queue |
| `publish_workers` | `4`; concurrent publication workers |
| `backup_retry_seconds` | `30`; delay before retrying temporary backup failures |
| `cold_read_timeout_seconds` | `900`; deadline for a request waiting for restored run data |
| `version_retention_seconds` | `7776000` (90 days); local historical-version retention after supersession or last successful read; current, pending and unverified versions remain protected. Remote history is retained; automatic remote purging is not implemented |
| `checkpoint_interval_seconds` | `86400` (one day); interval between private account/catalogue checkpoints |
| `checkpoint_retry_seconds` | `300`; delay before retrying a failed checkpoint |

Worker counts range from 1 to 32. Other fields in this table are positive integers
except `disk_reserve_bytes` and `restore_queue_limit`, which may be zero. Active reader leases and versions
without a verified backup prevent eviction. Listing the catalogue and background
refresh do not extend idle TTL. Failed credentials block backup retries until the
underlying authorization problem is resolved; temporary network failures remain
retryable. Check administrative job status before removing local data.

Cold restoration admits at most `restore_workers + restore_queue_limit` distinct
run generations at once. Readers of an admitted generation share its job without
using another slot. If the bound is reached, a new cold identity receives
`503 restore_busy`; warm reads and catalogue requests do not use these slots.
When the last reader leaves a queued job, that job is removed. Running storage
I/O remains counted until completion, even if every reader has timed out.
Completed jobs are discarded. The request deadline is not extended by retries
after cache eviction.

## Authentication

| Field | Meaning |
|---|---|
| `grant_ttl_seconds` | Lifetime of one-use API grants (seconds) |
| `session_ttl_seconds` | Browser session lifetime (seconds) |
| `login_attempts` | Failed-login budget per login window |
| `login_window_seconds` | Failed-login window (seconds) |
| `password_checks` | Maximum admitted concurrent password checks |

## Background work and shutdown

Recovery and background refresh retries use `recovery_retry_seconds` (default 5) between attempts and
`recovery_attempts` (default 3) per request. Both must be positive integers.

`shutdown_seconds` (default 15, positive integer) bounds HTTP request draining
and the subsequent maintenance shutdown separately. `make stop` waits up to
`2 * shutdown_seconds + 5` seconds, including process-exit overhead.
The CLI terminates the process on timeout while retaining the runtime lock;
interrupted journal operations recover on the next startup. Embedded hosts must
terminate their process if application shutdown fails, without reusing its runtime.

## Resource limits

All fields below belong inside `limits`. Values are positive integers;
`staging_free_bytes` alone may be zero to disable that free-space requirement.
The full example records the defaults as explicit integers.

| Field | Bound |
|---|---|
| `archive_bytes` | Compressed upload bytes |
| `unpacked_bytes` | Total unpacked bytes |
| `files` | Archive file count |
| `file_bytes` | Bytes per archive member |
| `json_bytes` | Bytes per JSON artifact |
| `tar_header_bytes` | Extended tar header bytes |
| `expansion_ratio` | Unpacked/compressed size ratio |
| `path_bytes` | UTF-8 bytes per path |
| `api_body_bytes` | JSON request-body bytes |
| `name_chars` | Run display-name characters |
| `basket_name_chars` | Basket-name characters |
| `basket_description_chars` | Basket-description characters |
| `runtime_chars` | Runtime-label characters |
| `task_title_chars` | Task-title characters |
| `tag_chars`, `tags` | Characters per tag and tag count |
| `diff_bytes`, `diff_lines` | Displayed diff-side bytes and source lines |
| `config_display_bytes` | Public native-config JSON bytes |
| `transfer_seconds` | Transfer/validation deadline in seconds |
| `staging_free_bytes` | Required free staging bytes before import |

See [deployment](deployment.md) and [storage recovery](reference.md#storage-backup-and-recovery)
for operating procedures. Increasing a limit changes resource requirements;
it does not relax archive path, provenance or ownership checks.

### Settings monitoring

| Field | Default and purpose |
| --- | --- |
| `admin_activity_limit` | `100`; maximum visible upload, backup and refresh rows |
| `storage_stats_max_objects` | `200000`; maximum objects in one S3 usage inventory |
| `storage_stats_timeout_seconds` | `60`; inventory deadline, checked between objects and requests |

All three values must be positive integers. An in-flight S3 SDK request retains
its connection/read timeout; the inventory checks cancellation before another
request. If the deadline or object cap is reached, partial totals are discarded.
A previous complete snapshot remains visible with its timestamp and unavailable
status until the next successful reload. S3 usage counts current objects beneath
`prefix`, including backup copies and trash; object versions and provider billing
quotas are not included. A changing bucket can change during enumeration.

Settings requests resources on first load and **Reload settings**. Concurrent
requests share one inventory. Activity polling and saving basket layout do not
start an inventory. Mock storage omits the S3 card. Memory and CPU data are shown
only when supported by the host. Local cache usage counts existing resident paths
once across metadata revisions; disk usage covers the runtime filesystem.
