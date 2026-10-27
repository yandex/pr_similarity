[PR Similarity](../../README.md) / [viewer](../README.md)

# Use the leaderboard

If a viewer is already deployed, open its HTTPS address: **no local setup or
login is needed to browse public runs**. Search/filter a basket's runs, open one
run, then a task title to expand its condition, score, file list and Human/Agent
diffs. The Original PR or Original MR link inside the card opens the source change.
The config button shows one safe native `opencode.json`, including the model,
reasoning options, skills and instructions. Resource paths are relative to the
agent directory in the downloaded archive. The run page shows runtime, timeout,
retries and exclusions separately, plus configured/effective model facts and origins.
Model shows `provider/model [effort]` from configured native facts. When effort
is absent or unknown, Model shows only `provider/model`, without brackets.
Setup contains user tags as-is.
Partially known cost/usage shows the known subtotal with a compact `(!)` marker.
Completely unknown measurements remain a dash; measured zero remains zero.
Hover, focus or tap a marker to see coverage and cost provenance. Score and time
also mark incomplete coverage. Repriced cost is an API-equivalent estimate;
native zero with OAuth is not measured API cost, and API-key zero can indicate
missing model pricing configuration. A timeout may still have a scored diff.
Durations use seconds, minutes or hours; tooltips retain exact seconds.

On a run page, its logged-in owner can use **Delete Run**; an administrator can
delete any run. Confirm the displayed name before deletion. The publication moves
to server trash through durable recovery; a newer overwritten publication requires
a reload and new confirmation. Guests and other ordinary users cannot delete it.
Deletion retains ownership and old receipts. Replaying an upload for a deleted
publication returns 404 rather than confirming an absent run; the publisher closes
that pending request, and a subsequent publication uses a fresh operation.

Different basket hashes remain separate populations. Coverage and omissions
describe the collector's input population; failed
records and generation rejections are distinct from benchmark execution failures.
An accepted partial basket does not mean that the omitted tasks were executed.

Scores compare agent edits with the human reference; they are not pass rates for
the project's tests. Keep basket and score inclusion filters consistent. Different
baskets are separate populations. Use the local [benchmark artifacts](../../benchmark/docs/results.md#find-your-results)
for deeper logs/attempt history; the web UI is not a complete transcript viewer.

To publish, ask this viewer's administrator for an account, then use the
[benchmark publisher](../../benchmark/docs/publication.md#publish-to-a-leaderboard):

```bash
# From benchmark/, with an actual stopped run and your viewer's HTTPS origin:
make publish RUN=./output/RUN_ID URL=https://viewer.example.org DRY_RUN=1
make publish RUN=./output/RUN_ID URL=https://viewer.example.org
```

The first command validates a local archive; the second requests login/password
and uploads. ChatGPT model login is separate. The server assigns publication
ownership from your account; browsing does not require that account. Publishing
to another viewer requires an account on that instance.

The owner can change a run's display name/tags and download its saved original
archive through the UI. An administrator can also download archives and pin runs;
pinning is a viewer presentation choice, not a new benchmark evaluation.
Changing a display name or Setup tag does not rewrite the source report/archive
or configured Model/Effort. Local run labels remain frozen across resume; an
overwrite preserves the server name/Setup edits and reports any differences.
Uploading the same ID again is a conflict unless an explicit same-owner overwrite
is requested; the server keeps the previous publication in trash.

Administrators use Settings to arrange, collapse or hide baskets from the home
catalogue. **Hidden is not private:** direct run/task/config/diff links remain
public. Review prompts, source and logs before publishing confidential work.
Original full archive downloads require owner/admin authorization.

For publication trouble, consult the benchmark's troubleshooting table. A browser
can accept a self-signed certificate while the CLI rejects it. TLS failure occurs
before password authentication; 200 on the credential POST followed by 503 on
the upload PUT indicates a service/storage problem. Health OK only confirms that
the listener responds. Do not reset passwords or delete client journals to fix a
storage failure. A public certificate/CA can be trusted by the host publisher
using `SSL_CERT_FILE`; never pass the server's private key or disable verification.

To host your own viewer, follow the [setup guide](../README.md#quick-start)
and [deployment recipe](deployment.md).
