[PR Similarity](../../README.md) / [viewer](../README.md)

# Persistent Linux service

This recipe uses **one systemd process with built-in TLS**, a private S3 bucket,
and persistent local publication files plus SQLite state. The service listens on HTTPS port 8443;
replace `viewer.example.org` with a DNS name pointing to the host and allow that
port through your firewall. Obtain a certificate for that name from a CA trusted
by browsers and publishers. A private CA also works if clients trust it. Keep
`public_origin` equal to the exact browser URL, including the port.

An administrator provisions the dedicated account and directories once:

```sh
sudo useradd --system --user-group --home-dir /var/lib/pr-similarity-viewer --shell /usr/sbin/nologin prsim-viewer
sudo install -d -o prsim-viewer -g prsim-viewer -m 0700 /var/lib/pr-similarity-viewer
sudo install -d -o root -g prsim-viewer -m 0750 /etc/pr-similarity-viewer
sudo install -d -o prsim-viewer -g prsim-viewer -m 0750 /opt/pr-similarity
```

Install the trusted release source at `/opt/pr-similarity` (the directory contains
`viewer/`, `benchmark/`, etc.). Make the viewer installation writable by
`prsim-viewer` for setup, then install its own venv. For a verified source
export directory (replace `/path/to/verified-release`):

```sh
sudo cp -a /path/to/verified-release/. /opt/pr-similarity/
sudo chown -R prsim-viewer:prsim-viewer /opt/pr-similarity/viewer
sudo -u prsim-viewer sh -c 'cd /opt/pr-similarity/viewer && make setup PYTHON=python3.12'
```

Provision `/etc/pr-similarity-viewer/s3.env` through your secret-management system,
owned by root with mode `0600`, outside the checkout. It contains systemd
EnvironmentFile entries `AWS_ACCESS_KEY_ID=...`, `AWS_SECRET_ACCESS_KEY=...`, and
`AWS_SESSION_TOKEN=...` when required. Use the actual credentials for the private
bucket, never commit this file or paste secrets into command-line arguments.
With a host/workload identity supported by boto3, omit `EnvironmentFile` from the
unit instead. The identity needs list/read/write/delete access within the chosen
bucket prefix; use a separate prefix for each viewer runtime. The backend must
support conditional `PutObject` with both `IfNoneMatch` and `IfMatch`; providers
that reject these conditions cannot safely host the backup registry. The viewer
fails rather than falling back to unconditional writes.

Install the certificate chain and private key as
`/etc/pr-similarity-viewer/fullchain.pem` and `/etc/pr-similarity-viewer/privkey.pem`,
owned by root, group `prsim-viewer`, mode `0640`. The service user must be able to
traverse their parent directories. Create `/etc/pr-similarity-viewer/config.json`
with the same ownership and mode, replacing the bucket, region and hostname:

```json
{
  "runtime": "/var/lib/pr-similarity-viewer",
  "admin_token_file": "/var/lib/pr-similarity-viewer/admin-token",
  "storage": "s3",
  "publication_storage": "local",
  "bucket": "your-private-bucket",
  "region": "us-east-1",
  "endpoint": null,
  "prefix": "prsim/",
  "host": "0.0.0.0",
  "port": 8443,
  "public_origin": "https://viewer.example.org:8443",
  "tls_cert": "/etc/pr-similarity-viewer/fullchain.pem",
  "tls_key": "/etc/pr-similarity-viewer/privkey.pem"
}
```

For another S3-compatible provider, set its HTTPS endpoint and signing region.
The runtime, config and key paths are absolute: they must remain the same across
source updates. Install `/etc/systemd/system/prsim-viewer.service` as root:

```ini
[Unit]
Description=PR Similarity viewer
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=prsim-viewer
Group=prsim-viewer
WorkingDirectory=/opt/pr-similarity/viewer
Environment=PRSIM_VIEWER_CONFIG=/etc/pr-similarity-viewer/config.json
EnvironmentFile=/etc/pr-similarity-viewer/s3.env
ExecStart=/opt/pr-similarity/viewer/.venv/bin/python -m viewer serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
UMask=0077

[Install]
WantedBy=multi-user.target
```

`serve` stays in the foreground. Do not use `make start`, multiple workers or a
second unit against this runtime. systemd owns the process and captures its logs:

```sh
sudo systemd-analyze verify /etc/systemd/system/prsim-viewer.service
sudo systemctl daemon-reload
sudo systemctl enable --now prsim-viewer
sudo systemctl is-enabled prsim-viewer
sudo systemctl status prsim-viewer
sudo journalctl -u prsim-viewer -n 100 --no-pager
# Routine operations:
sudo systemctl restart prsim-viewer
sudo systemctl stop prsim-viewer
sudo systemctl start prsim-viewer
```

Background recovery and catalogue refresh failures log the error code, attempt
number, exception type and frame locations (file name, line and function) in the
service journal. These diagnostics omit exception messages, source lines and
local variables. The status API reports the safe error code without frame details. S3 diagnostics
also record the operation, hashed object identifier, HTTP/provider error code,
request ID, duration and SDK retry count; raw keys, headers and provider error
messages are omitted.

`enable` starts the unit after reboot. During a maintenance window, also verify
status, login and the saved run after a host reboot. Create the first administrator
locally, at the hidden password prompt, as the runtime owner:

```sh
sudo -u prsim-viewer sh -c 'cd /opt/pr-similarity/viewer && make admin-profile-create CONFIG=/etc/pr-similarity-viewer/config.json LOGIN=admin'
```

For certificate renewal, first obtain and validate the renewed certificate/key,
then install both at the configured paths with the same owner/group/permissions.
Restart `prsim-viewer` after both files are installed; built-in TLS loads them on
startup. Check HTTPS with a trusted client and inspect the new expiry date. Keep
the previous pair until this succeeds. Arrange this install/restart/check sequence
as your certificate manager's renewal hook; do not assume a file replacement
reloads the running listener. Restart after rotating static S3 credentials too.

### Deployment acceptance

From a client that trusts the certificate, check
`curl --fail https://viewer.example.org:8443/healthz` for process liveness and
`curl --fail https://viewer.example.org:8443/readyz` for readiness. The latter
returns 503 while initialization is running; wait for 200
before signing in through the browser. Create a publisher account through **Users**. From an installed benchmark
and an actual completed run, exercise storage and publication:

```sh
# From benchmark/, replace RUN with your existing completed run directory:
make publish RUN=/absolute/path/to/run URL=https://viewer.example.org:8443 DRY_RUN=1
make publish RUN=/absolute/path/to/run URL=https://viewer.example.org:8443
```

The second command prompts for that viewer account. Verify the completed receipt,
open the published run, task and native config pages, and download the original
archive as its owner. Compare its SHA-256 with the publisher's saved
`operations/publish/<operation>/payload.tar.gz`. Restart the service and repeat
login, run/task/config reads and the archive comparison. This checks local durable
publication and SQLite account/ownership persistence. Then inspect
`GET /api/v1/admin/stats`: wait for the backup job to complete, request
`make admin-refresh CONFIG=/etc/pr-similarity-viewer/config.json`, and require a
`succeeded` full audit. The audit reads and validates the remote archive bytes;
a local read or `/healthz` alone does not check S3 backup integrity.

Request `POST /api/v1/admin/checkpoint` using administrator authentication and
wait for `checkpoint_job.status: succeeded`. Retain its `manifest_key` and rehearse
[offline restore](reference.md#checkpoints-and-offline-restore) into a new runtime
before depending on it for disaster recovery. These are deployment acceptance
steps to run on your own authorized installation, not evidence of a production
validation performed by this release. Check the service journal for storage errors.

For an existing runtime, follow the stopped-service
[legacy import procedure](reference.md#upgrading-an-existing-viewer) before enabling
`publication_storage: "local"`. For updates use [Reading data from earlier releases](../../README.md#reading-data-from-earlier-releases). Preserve SQLite
and the admin token; see [storage, backup and recovery](reference.md#storage-backup-and-recovery)
for S3 layout, receipts and recovery. Source replacement must not replace runtime.
