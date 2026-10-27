# PR Similarity Viewer

[PR Similarity](../README.md) / Viewer

Browse PR Similarity benchmark runs, task scores and Human/Agent diffs in a
standalone web UI. Publication checks the basket data against the report and
saved evidence. Guests can browse immediately; administrators manage users,
basket visibility and cache refresh through the UI.

## Use the leaderboard

Open a deployed viewer's HTTPS address to browse public baskets, runs, scores
and Human/Agent diffs without local setup. Owners can publish and manage their
runs; administrators manage accounts and presentation. See the
[leaderboard guide](docs/leaderboard.md) for metrics, visibility and account actions.

## Prerequisites

- **Linux**, **Python 3.12 or newer** with venv/pip support, and **GNU Make**.
- Network access to **PyPI** for setup. A persistent deployment also needs a
  private **S3-compatible object store** and an **HTTPS entrypoint**.

## Quick start

`make setup` creates a missing environment without prompting. If `.venv`
already exists or `VENV_PYTHON` is executable, it asks
`Virtual environment already exists. Reinstall dependencies? [Y/n] `.
Enter or `Y` repeats setup in that environment without deleting it; `n` skips
all setup steps. EOF cancels setup before installation. An accepted setup
upgrades pip before installing project dependencies; it uses `PACKAGE_INDEX_URL`
for pip bootstrap, upgrade and dependency installation.

Run from this directory. `make help` lists commands and variables without setup.

```sh
make help
make setup PYTHON=python3.12
cp examples/mock.json config.local.json
make start
make admin-profile-create LOGIN=admin
```

Open **http://127.0.0.1:8000**. The mock configuration stores data locally and
needs no object storage or credentials. The catalogue starts empty. To populate
it with the [benchmark](../benchmark/docs/publication.md#publish-to-a-leaderboard), expose
the mock service through verified HTTPS (reverse proxy or configured TLS); the
benchmark publisher does not accept the quick-start HTTP origin.

The admin command prompts for a password. Run it on the service machine as the
same OS user that owns the runtime. Log in to create ordinary users through
**Users**; manage baskets through **Settings**. Admin roles are assigned locally,
never through the public UI. Guests see only visible baskets on the home page;
hidden runs remain accessible by direct links.
Users manages ordinary accounts only: admin passwords and access are protected
from changes through UI/API. Use **Change password** for your own password, or
`make admin-profile-password-set LOGIN=admin` for recovery on the service machine.

```sh
make status
make logs
make stop
```

## Configuration

[examples/full.json](examples/full.json) lists every server setting and resource
limit. See the [configuration reference](docs/configuration.md) for units, path
resolution and storage/TLS alternatives. Use the [persistent Linux service recipe](docs/deployment.md)
for a supervised installation.

For a persistent deployment, copy `examples/s3.json` to `config.local.json`
and set these fields:

| Field | What to set |
| --- | --- |
| `storage` | `s3` for a private S3-compatible object store |
| `bucket` | An existing private bucket |
| `endpoint`, `region`, `prefix` | Values required by your object store; `prefix` optionally isolates this instance |
| `runtime` | A persistent absolute directory owned by the service user |
| `admin_token_file` | A file inside that runtime directory |
| `host`, `port` | The listener; default `127.0.0.1:8000` |
| `public_origin` | The exact external HTTPS origin, such as `https://viewer.example.org` |

Provide storage credentials through environment variables or the
[standard boto3 credential chain](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html),
outside the JSON config and repository. The service
needs list/read/write/delete access to its bucket prefix. Use one process per
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

Startup creates the private runtime and a random admin token automatically.
Passwords are entered at hidden prompts. Keep the runtime private and back up
SQLite using its backup API before upgrades; keep the backup available for recovery. Storage, recovery and optional trash lifecycle
settings are described in the [service reference](docs/reference.md#storage-backup-and-recovery).

<details>
<summary>Compatibility</summary>

The viewer can display supported archives from earlier installations without
converting the original files. Update benchmark and viewer together before
publishing new results, and preserve SQLite backups when upgrading the service.
See the [saved-data note](../README.md#reading-data-from-earlier-releases).

</details>

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
| Refresh caches | `make admin-refresh` |

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
a real bucket. `GET /healthz` returns `{"status":"ok"}` for a running service.
