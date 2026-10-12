# Example baskets

These frozen v1 baskets work with the current benchmark without rerunning the
collector or generating tasks again.

| File | Source | Tasks | Selection |
|---|---|---:|---|
| [opensearch-10.json](opensearch-10.json) | [OpenSearch](https://github.com/opensearch-project/OpenSearch) | 10 | Seed 1337; default-branch PRs merged during the first half of 2026 |
| [pandas-25.json](pandas-25.json) | [pandas](https://github.com/pandas-dev/pandas) | 25 | Seed 42; default-branch PRs merged in April 2026 |

Run from `benchmark/` after [setup and model login](../../README.md#quick-start):

```bash
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=examples/baskets/opensearch-10.json OUTPUT=./output
make run CONFIG=examples/chatgpt-oauth.yaml DATASET=examples/baskets/pandas-25.json OUTPUT=./output
```

Each basket contains exact Git revisions, change metadata, source retrieval
receipts and tasks generated from diff + issue using `gpt-6.1-sol` with medium
reasoning effort. That generator model does not select the benchmarked agent.
No reference patches are embedded; the benchmark retrieves public Git sources.
OpenSearch is a large repository; check your configured Git transfer budget and
available disk space before a run.

Historical generation settings omit the provider endpoint and credential-variable
name. Task records and source revisions are unchanged. These are ordinary frozen
baskets; choose any agent configuration when running them.

[Benchmark](../../README.md) · [PR Similarity](../../../README.md)
