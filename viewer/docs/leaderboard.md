[PR Similarity](../../README.md) / [viewer](../README.md)

# Use the leaderboard

If a viewer is already deployed, open its HTTPS address: **no local setup or
login is needed to browse public runs**. Search/filter a basket's runs, open one
run, then a task title to expand its condition, score, file list and Human/Agent
diffs. The Open Human PR or Open Human MR button inside the card opens the source change.
File dialogs open in **Diff** mode with Human and Agent unified diffs.
Use **Diff / Full file** beside the close button to switch both panels.
**Full file** shows source context, line numbers, inline additions/deletions and
a change map. The selected mode applies to subsequent files until page reload. Empty, absent and truncated files have
explicit messages. Expanded tasks show raw line-change and changed-file counts
for Human PR and Agent Solution; Partial counts omit files over display limits.
These counts are separate from normalized scoring weights.

The config button shows one safe native `opencode.json`, including the model,
reasoning options, skills and instructions. Resource paths are relative to the
agent directory in the downloaded archive. Copy and Download save the displayed
input JSON. Setup also reports authentication mode, setup-script presence,
environment-variable count and whether task instructions match the default.
The run page shows runtime, timeout,
retries and exclusions separately, plus configured/effective model facts and origins.
Model shows `provider/model [effort]` from configured native facts. When effort
is absent or unknown, Model shows only `provider/model`, without brackets.
Setup contains user tags as-is.
Partially known cost/usage shows the known subtotal with a Partial status.
Unavailable measurements have an explicit status. Click or focus the metric to
inspect coverage and cost provenance.
Completely unknown measurements remain a dash; measured zero remains zero.
Score and time also mark incomplete coverage. Repriced cost is an API-equivalent estimate;
native zero with OAuth is not measured API cost, and API-key zero can indicate
missing model pricing configuration. A timeout may still have a scored diff.
Durations use seconds, minutes or hours; tooltips retain exact seconds.

Subtotals include completed measured requests inside incomplete tasks, using
only the state-selected final attempt. For example, 99 measured requests and
one unfinished request show the known sum and `99/100 requests measured`, while
the full total remains unknown. Coverage is independent for each token category
and cost. A known zero subtotal is still marked partial; a dash means no known
measurement. Request coverage is not the fraction of tokens or money measured.
Historical evidence without request detail retains its known totals and explicitly
unknown request coverage. Supported existing publications need no re-upload.

The status selector applies before aggregation; its default includes `OK`,
`no-op` and `timeout`, excluding `fail`. Task status does not determine usage
completeness. Wall Time is independent of the selector. Cost/token sorting uses
the displayed full amount or known subtotal; partial amounts cannot establish
that an agent is cheaper. The selected saved reprice takes precedence even when
partial or unavailable; browsing does not reprice tokens or make model calls.

The API's `amount` remains a nullable full total and `known_tasks` counts tasks
with complete measurements. `known_subtotal` also includes known parts of
incomplete tasks; `partial_tasks`, `unavailable_tasks`, `known_requests`,
`total_requests`, `request_coverage_available` and `status` describe coverage.
These display fields do not change saved benchmark report or archive formats.

On a run page, its logged-in owner can use **Delete Run**; an administrator can
delete any run. Confirm the displayed name before deletion. The expected publication
hash prevents deleting a version that has changed since the page was loaded.
Local storage records a deletion tombstone and removes the run from the catalogue;
legacy storage retains its trash workflow. Guests and other ordinary users cannot
delete the run. Existing archives and historical receipts keep their original identity.

A run that has been idle may need to reload its archived data before the page or
file opens. The original request waits for validation and restore. Catalogue
listings remain available from the persisted database. A failed restore reports
an error without exposing partial run contents.

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
Uploading the same ID again is a conflict unless an explicit overwrite is
requested.
Local storage keeps distinct generations rather than changing archived bytes.

Administrators use Settings to arrange, collapse or hide baskets from the home
catalogue. Collapsed sections load their run rows when expanded. Drag a basket by its grip, or focus the grip and use ArrowUp, ArrowDown,
Home or End. Save basket layout persists the order and flags; Reload settings
recovers from a concurrent revision conflict. **Hidden is not private:** direct
run/task/config/diff links remain
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
