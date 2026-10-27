# Viewer configuration examples

Start with [mock.json](mock.json) to try the viewer locally. For hosting, choose
[s3.json](s3.json), or [full.json](full.json) to see every server setting and limit.

| File | Purpose |
|---|---|
| [mock.json](mock.json) | Local development with filesystem storage |
| [s3.json](s3.json) | Minimal configuration for private S3-compatible storage |
| [full.json](full.json) | Complete settings reference with explicit limits |

From `viewer/`, copy your choice to `config.local.json` and customize it.
Paths resolve from the process working directory, not this examples directory;
use absolute paths for a hosted service. Storage credentials stay outside JSON.

Follow the [quick start](../README.md#quick-start) for a local instance, the
[configuration guide](../docs/configuration.md) for field meanings, or the
[deployment guide](../docs/deployment.md) for a persistent service with HTTPS.
