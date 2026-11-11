# PR Similarity Viewer

[PR Similarity](../README.md) / Viewer

Browse PR Similarity benchmark runs, task scores and Human/Agent diffs in a
standalone web UI. Publication checks the basket data against the report and
saved evidence. Guests can browse immediately; administrators manage users,
basket visibility and cache refresh through the UI.

## Quick start

To host a local viewer, you need Linux, Python 3.12 or newer, Make and access
to PyPI. Run from `viewer/`:

```sh
make setup PYTHON=python3.12
cp examples/mock.json config.local.json
make start
make admin-profile-create LOGIN=admin
```

`make setup` prints installation progress and the environment and Python paths.
Package installation is quiet; warnings and errors remain visible.

Enter the administrator password when prompted, then open **http://127.0.0.1:8000**.
Log in to create users and manage baskets. The mock configuration uses local publication storage and a local backup backend.
It starts with an empty catalogue.

> [!NOTE]
> To browse an existing service, open its HTTPS address; no local installation
> is needed. See [Use the leaderboard](docs/leaderboard.md#use-the-leaderboard).

> [!IMPORTANT]
> Benchmark publication requires verified HTTPS. Before uploading to this local
> viewer, configure TLS or an HTTPS reverse proxy and set `public_origin` to its
> external origin. See the [deployment guide](docs/deployment.md#persistent-linux-service).

Manage the local process from the same directory:

```sh
make status
make logs
make stop
```

## Use the leaderboard

Open a deployed viewer's HTTPS address to browse public baskets, runs, scores
and Human/Agent unified diffs or full files with change maps without local setup.
Use **Diff / Full file** beside the file-dialog close button to switch both panels. Owners can publish and manage their
runs; administrators manage accounts and presentation. See the
[leaderboard guide](docs/leaderboard.md) for metrics, visibility and account actions.

## Configuration

[examples/full.json](examples/full.json) lists every server setting and resource
limit. See the [configuration reference](docs/configuration.md) for units, path
resolution and storage/TLS alternatives. Use the [persistent Linux service recipe](docs/deployment.md)
for a supervised installation.

## Accounts

The admin command prompts for a password. Run it on the service machine as the
same OS user that owns the runtime. Log in to create ordinary users through
**Users**; manage baskets through **Settings**. Admin roles are assigned locally,
never through the public UI. Guests see only visible baskets on the home page;
hidden runs remain accessible by direct links.
Users manages ordinary accounts only: admin passwords and access are protected
from changes through UI/API. Use **Change password** for your own password, or
`make admin-profile-password-set LOGIN=admin` for recovery on the service machine.

## Development

The root [operator skills](../README.md#work-with-a-coding-assistant) cover
publication and analysis. [AGENTS.md](AGENTS.md) is the local development guide;
[CLAUDE.md](CLAUDE.md) imports it. Service administration and deployment are
separate from uploading a run.

| Task | Command |
| --- | --- |
| Foreground service / local background service | `make serve` / `make start` |
| Status, log, stop | `make status`, `make logs`, `make stop` |
| Create an admin locally | `make admin-profile-create LOGIN=admin` |
| Assign a role locally | `make admin-role-set LOGIN=student ROLE=user` |
| Create / enable / disable a user | `make admin-user-create LOGIN=student`, `make admin-user-enable LOGIN=student`, `make admin-user-disable LOGIN=student` |
| Reset a password | `make admin-password-set LOGIN=student` |
| Verify and refresh all current backups | `make admin-refresh` |
| Import legacy storage while stopped | `make admin-local-import CONFIG=config.local.json` |
| Restore a private checkpoint while stopped | `make admin-checkpoint-restore CHECKPOINT_KEY=<key> TARGET_RUNTIME=<new-directory>` |

All service/admin commands accept `CONFIG=<path>`. Administration can use
`URL=<https-origin>`; the local provisioning commands access SQLite directly.
Additional commands and setup variables are listed by `make help`.

```sh
make browser-setup
make test
```

Chromium needs the [Playwright system libraries](https://playwright.dev/python/docs/browsers#system-dependencies).
`make test` is the required offline test and release smoke check. It includes
real Chromium browser scenarios, HTTP authentication and archive download,
publication/recovery with local storage, and archives produced by the benchmark.
Run the complete suite without skipped tests after code or UI changes; a partial
pytest selection does not satisfy the release smoke check. Browser setup and the
benchmark test environment must be available for these checks.

Tests use local/mock storage and temporary servers; they do not deploy or call
a real bucket. `GET /healthz` returns `{"status":"ok"}` for a running process.
`GET /readyz` returns 200 only after recovery and catalogue validation; otherwise
it returns 503 with the startup phase and a safe error code. The CLI starts its
listener during initialization; result APIs return 503 until ready. Local mode
loads its persisted catalogue without scanning S3 at startup. Legacy mode retains
its startup recovery and catalogue validation.
Settings places operation progress below the basket controls and shows server and
storage usage. Resources update on initial load and **Reload settings**; mock
storage hides unavailable S3 metrics. Available S3 usage categories stay visible
as cards while measurements update. See [monitoring configuration](docs/configuration.md#settings-monitoring).

## Advanced topics

<details>
<summary>Persistent deployment</summary>

For a persistent deployment, copy `examples/s3.json` to `config.local.json`
and set these fields:

| Field | What to set |
| --- | --- |
| `publication_storage` | `local` after explicit migration of any existing runtime |
| `storage` | `s3` for a private archive-backup object store |
| `bucket` | An existing private bucket |
| `endpoint`, `region`, `prefix` | Values required by your object store; `prefix` optionally isolates this instance |
| `runtime` | A persistent absolute directory owned by the service user |
| `admin_token_file` | A file inside that runtime directory |
| `host`, `port` | The listener; default `127.0.0.1:8000` |
| `public_origin` | The exact external HTTPS origin, such as `https://viewer.example.org` |

Provide storage credentials through environment variables or the
[standard boto3 credential chain](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html),
outside the JSON config and repository. The service
needs list/read/write/delete access to its bucket prefix and conditional
`PutObject` support (`IfNoneMatch` and `IfMatch`). Unsupported endpoints do not
fall back to overwriting immutable objects. Use one process per
runtime and keep its SQLite database and admin token across updates.

Expose the listener through your HTTPS reverse proxy, or configure `tls_cert`
and `tls_key` for built-in TLS. Browser login requires the configured
`public_origin` to match the served origin. Local HTTP is supported on loopback.

Start in the foreground for a process manager:

```sh
make serve CONFIG=config.local.json
```

After it starts, create the first administrator in another terminal as the
runtime owner:

```sh
make admin-profile-create CONFIG=config.local.json LOGIN=admin
```

> [!WARNING]
> Back up SQLite with its backup API before upgrading a persistent service.
> Keep the runtime directory, admin token and original archives.

Startup creates the private runtime and a random admin token automatically.
Passwords are entered at hidden prompts. Keep the runtime private.
Storage, recovery and optional trash lifecycle settings are described in the [service reference](docs/reference.md#storage-backup-and-recovery).

</details>

<a id="prerequisites"></a>
<details>
<summary>Setup behavior and development label</summary>

`make setup` creates a missing environment without prompting. If `.venv`
already exists or `VENV_PYTHON` is executable, it asks
`Virtual environment already exists. Reinstall dependencies? [Y/n] `.
Enter or `Y` repeats setup in that environment without deleting it; `n` skips
all setup steps. EOF cancels setup before installation. An accepted setup
upgrades pip before installing project dependencies; it uses `PACKAGE_INDEX_URL`
for pip bootstrap, upgrade and dependency installation.

The header shows the installed viewer version. Use `make start DEV=1` or
`make serve DEV=1` to add `[dev]` for a development deployment (default: `DEV=0`).
For direct `python -m viewer serve` launches, set `PRSIM_VIEWER_DEV=1`.
Restart the service to change the label. This flag only changes the label.

</details>

<details>
<summary>Compatibility</summary>

The viewer can display supported archives from earlier installations without
converting the original files. Update benchmark and viewer together before
publishing new results, and preserve SQLite backups when upgrading the service.
Existing configurations retain legacy storage until an explicit
[import](docs/reference.md#upgrading-an-existing-viewer). New publication clients
require local mode; keep the producing installation to recover legacy pending uploads.
The reader accepts state v3 recovery observations alongside v2 run/report/native
facts. An interrupted historical attempt may omit only explicitly unrecorded
native facts; the current selected attempt still needs valid publication evidence.
See the [saved-data note](../README.md#reading-data-from-earlier-releases).

</details>

<a id="publication-completion"></a>
<details>
<summary>Publication completion and recovery</summary>

With `publication_storage: "local"`, publication returns an `available` receipt
only after archive validation, a synced local version and a SQLite commit. The
backup intent is committed with the receipt; background backup starts after the
response and resumes after restart. A successful receipt confirms local durability,
not completion of S3 backup.

The catalogue is stored in SQLite. Run reads use a local version or wait in the
same HTTP request while a verified archive bundle is restored. Backed-up inactive
copies become eligible for eviction after 48 idle hours or under disk pressure;
active readers and unbacked data remain protected. Administrative refresh verifies
all current archive bundles, including unchanged ones. After checkpoint restore,
reconciliation also preserves allocation floors from interrupted remote backups
before allowing new writes. Startup upload recovery retries transient failures
without stopping cache maintenance or checkpoint scheduling. See
[storage and recovery](docs/reference.md#storage-backup-and-recovery) for migration,
checkpoints, progress and error handling.

</details>
