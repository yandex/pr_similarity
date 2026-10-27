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
| `storage` | `s3` for object storage; `mock` for local development |
| `bucket` | Existing private S3 bucket; unused with mock storage |
| `endpoint`, `region` | Optional S3 endpoint/region; null uses SDK defaults |
| `prefix` | Optional normalized object-key prefix to isolate this instance |
| `tls_cert`, `tls_key` | Certificate and private-key paths for built-in TLS; set both or neither |
| `public_origin` | Exact browser HTTPS origin, including a nondefault port; loopback HTTP is allowed |

The full example assumes an HTTPS reverse proxy in front of the loopback
listener. To serve TLS directly, set both certificate paths and the matching
origin/port. Storage credentials come from the environment or standard boto3
credential chain; do not add credentials to JSON.

## Authentication

| Field | Meaning |
|---|---|
| `grant_ttl_seconds` | Lifetime of one-use API grants (seconds) |
| `session_ttl_seconds` | Browser session lifetime (seconds) |
| `login_attempts` | Failed-login budget per login window |
| `login_window_seconds` | Failed-login window (seconds) |
| `password_checks` | Maximum admitted concurrent password checks |

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
