[PR Similarity](../../README.md) / [benchmark](../README.md)

# Publish to a leaderboard

Use the **run directory**, not its OUTPUT parent. Ask the viewer administrator
for an ordinary account on the target instance; ChatGPT login is separate.
`URL` must be an HTTPS origin, with no path/query. A trailing `/` is accepted.
The mock viewer's loopback HTTP UI is browseable, but the publisher requires HTTPS.

After the writer stops, validate the complete local archive:

```bash
make publish RUN=./output/RUN_ID URL=https://viewer.example.org DRY_RUN=1
```

> [!NOTE]
> `DRY_RUN=1` writes and validates a local archive without HTTP or login.
> Keep the complete run directory: `report.json` alone is not enough to publish.

To upload:

```bash
make publish RUN=./output/RUN_ID URL=https://viewer.example.org
```

In a terminal the client prompts for login and a hidden password, then requests
a one-use grant. Automation can use `TOKEN_ENV=PRSIM_VIEWER_TOKEN` or
`TOKEN_FILE=/private/publish.token` (mode 600, outside the run). Choose one source;
never pass literal passwords/tokens in Make arguments or YAML.

Confirm `status: completed` in `operations/publish/<uuid>/receipt.json`. Its
`server_receipt` must contain `protocol: local-durable-v1`,
`durability: local_durable`, `phase: available`, server owner/actor and the original
archive/report hashes. The publisher also reads back
run/config/tasks over HTTPS and checks basket and configured Model/Effort origins.
An overwrite preserves server display name/Setup tags and reports differences
from local labels. For an ambiguous upload, repeat the same command to replay
its frozen `payload.tar.gz`, request and idempotency key. `_meta/state.json`
selects `pending_publish`; moving the complete run preserves relative replay refs. A 409 is a conflict; use
`OVERWRITE=1` only when you intend to replace the same owner's publication.
Publication completes after the server validates and durably commits the local version.
The server starts archival backup after responding. See [viewer usage](../../viewer/docs/leaderboard.md#use-the-leaderboard).

## Recovery and troubleshooting

<details>
<summary>Progress, timeouts and replaying a pending upload</summary>

The client requires `local-durable-v1` from
`GET /api/v1/publication-capabilities`. A legacy server gives an explicit
unsupported-protocol error. During PUT, a separate authenticated connection reads
`/api/v1/publication-operations/{idempotency_key}` and displays the server's
receiving, extracting, validating and committing messages. Wait for an `available`
receipt with `durability: local_durable` and successful public readback.
These checks determine success; upload completion alone does not.

Before resending a pending upload, the client checks the operation status with
its original run ID and hashes. A completed operation returns its receipt without
resending archive bytes. An operation still processing retains its journal and
asks for a later retry. Historical receipts remain readable; a pending legacy
request must be recovered with its original client, preserving its frozen bytes.
The upload response wait is bounded to 960 seconds (connect: 20 seconds), allowing
the viewer's default 900-second transfer budget. Login and readback keep their
shorter timeout. `PROGRESS=auto` updates a TTY line, `plain` prints a heartbeat
every 10 seconds, and `off` suppresses transient feedback. A timeout retains the
exact pending payload/key; repeat the command to recover the same publication.

</details>

### Publish after an interrupted attempt

<details>
<summary>Prepare a run with incomplete historical attempts</summary>

Run `make run CONFIG=... DATASET=<original-basket> RESUME=<run-directory>` before
publishing an older run with incomplete historical attempts. Resume records the
missing native facts separately and keeps every original attempt. Completed tasks
are reused. If unfinished tasks remain, normal resume may start new attempts.

The archive and viewer accept explicitly interrupted history only when the later
state-selected attempt satisfies the usual publication checks. They still reject
missing committed files, digest mismatches and foreign references. All-attempt
costs retain known subtotals and mark unknown usage as incomplete; selected task
scores and final-attempt measurements keep their existing meaning.

</details>

### Distinguish a wrong password, TLS failure and broken API

<details>
<summary>Diagnose authentication, TLS and storage failures</summary>

“Publication failed” is a safe summary, not a diagnosis. Each command writes
`_meta/invocations/<uuid>/benchmark.log` at DEBUG regardless of console `LOG_LEVEL`.
Use that folder's `invocation.json` (`kind: publish`, `logs_dir`) to locate the
command log, and the selected publish request/receipt for exact operation identity. Inspect it locally; do not paste a whole log without checking its contents.

Print the latest publication log path without reading any credentials:

```bash
.venv/bin/python - ./output/RUN_ID <<'PY'
import json
import sys
from pathlib import Path
run = Path(sys.argv[1])
calls = [json.loads(p.read_text()) for p in (run / '_meta/invocations').glob('*/invocation.json')]
calls = [c for c in calls if c.get('kind') == 'publish']
if not calls:
    raise SystemExit('No publication invocation found.')
latest = max(calls, key=lambda c: c['started_at'])
print(run / latest['logs_dir'] / 'benchmark.log')
PY
```

Open that file locally and look for the HTTP response status of the POST and PUT.

| Evidence | Meaning / next step |
|---|---|
| `CERTIFICATE_VERIFY_FAILED`, self-signed certificate | TLS failed before the password request reached the API; configure certificate trust |
| Hostname mismatch / expired certificate | Fix the origin/certificate, not the password |
| DNS, connection refused, timeout | Check address, listener, routing/proxy and service availability |
| Authentication request rejected with 401/403 | Check the account on this viewer, password and enabled state; those causes are intentionally not distinguished publicly |
| 429 | Login rate limit; wait, then retry with the correct account |
| 404, HTML response, redirects on the API route | Check origin, reverse-proxy routing and viewer API compatibility |
| 5xx | Server/proxy failure; administrator should inspect server logs |
| PUT returns 409 | Existing run conflict, separate from login failure |

Read-only checks, without login/password:

```bash
curl --fail --show-error https://viewer.example.org/healthz
curl --fail --show-error https://viewer.example.org/api/v1/auth/session
```

A health response of `{"status":"ok"}` shows reachability; the anonymous session
route exercises the API. Neither proves a particular account/password or storage
upload is valid. A browser can have manually accepted an untrusted certificate
while Python still rejects it.

If the credential POST returned 200 and the PUT returned 503, the password was
accepted: check the viewer's staging/storage side. An S3 viewer may keep serving
its cached catalogue while refreshing object-store credentials fails. The
administrator should check the configured SDK credential provider and storage
connectivity; a healthy listener does not establish either. Do not delete a
pending client journal or reset an account as a storage workaround.

For a private viewer with a self-signed certificate, obtain its **public**
certificate or CA bundle from the administrator and verify its identity. Supply
that trusted file to the publisher through HTTPX's `SSL_CERT_FILE`:

```bash
SSL_CERT_FILE=/path/to/viewer-trust.pem make publish RUN=./output/RUN_ID URL=https://viewer.example.org
```

Certificate/hostname verification stays enabled. Use a trust bundle including
any other needed roots; do not pass the server's private key or disable TLS
verification. Test trust first with `curl --cacert /path/to/viewer-trust.pem` and
the health/session URLs. `SSL_CERT_FILE` configures the host publication client;
it does not install custom certificates into the agent's bubblewrap sandbox.
The credentials/grant POST route is `/api/v1/auth/publish-token`; the upload route
is PUT `/api/v1/runs/{run_id}`. An administrator can inspect those server-side
failures without requesting the user's password.

</details>
