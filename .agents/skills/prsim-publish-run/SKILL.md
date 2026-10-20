---
name: prsim-publish-run
description: Use when publishing a requested PR Similarity run to an HTTPS viewer, validating a publication archive, or diagnosing an upload and its exact readback.
---

# Publish a run

Read root/benchmark AGENTS.md, the [publication guide](../../../benchmark/README.md#publish-to-a-leaderboard)
and [viewer guide](../../../viewer/README.md#use-the-leaderboard). Use benchmark's
`.venv` and Make interface. Resolve the exact stopped run and HTTPS origin;
validate current registry/state and complete native evidence first.
Both basket v1/v2 are supported; preserve archived basket bytes and hashes.
Do not substitute a newer collector export or convert v1 to suppress its notice.
For v2, check report/basket coverage and omissions as well as task identity;
collector omissions are not failed agent attempts. Confirm the target viewer
supports v2 before publishing a v2 run to an older deployment.

Diagnosis, config preparation and dry-run do not authorize upload. An explicit
publication request authorizes that upload without repeated confirmation.
`make publish RUN=... URL=... DRY_RUN=1` freezes/validates local evidence without
HTTP/login. Review intended sharing of source, prompts, declared resources and logs.
Use a supported hidden login or named token source; never print credentials,
place values in argv/YAML, read profiles as a workaround or disable TLS.

For upload use `make publish RUN=... URL=...`. Phase/elapsed feedback uses
`PROGRESS=auto|plain|off`; object-store persistence can
take several minutes after the bytes are sent. Upload read timeout is 960 seconds,
with connect at 20 seconds. Wait for receipt/readback, not an upload percentage.
Each operation owns
`operations/publish/<uuid>/{payload.tar.gz,request.json}` plus a closed
`receipt.json` for an upload or `validation.json` for a dry-run. `_meta/state.json` selects `pending_publish` after
the immutable payload/request is frozen. A lost reply keeps that exact request,
archive bytes, digest and idempotency key pending. Moving the complete run retains
relative replay paths; an external archive cache is only a cache. Older payload
blobs are omitted from future archives, with request/receipt history retained.

Confirm the closed receipt and original archive/report/basket hashes, server
owner and idempotency identity. Read back HTTPS run/config/tasks and
the `model [effort]` label and its origins (no brackets or internal effort tag
when the level is unknown); compare results to the frozen archived report, not a later
local reprice. Config readback confirms projection v2 and the frozen native input source;
harness timeout/retries/exclusions are separate run metadata. Paginated task
readback confirms original order/identity and publication binding.
Server name/Setup are separate editable metadata. An overwrite
preserves those edits and reports local differences; generated groups remain
configured facts, and user tag prefixes stay Setup.

After ambiguity, repeat only within authorized scope to replay the selected
pending request. Confirmed completed/conflict/rejected receipts clear it. A 409
never grants overwrite: use `OVERWRITE=1` only for explicit replacement intent.
Inspect `_meta/invocations/<uuid>/benchmark.log`; distinguish TLS, login POST,
upload PUT and storage failure. Health/session reads do not prove a password.
Preserve journals/payloads; do not edit state, accounts or services to repair an
upload. Return exact receipt/readback evidence and unresolved differences.
