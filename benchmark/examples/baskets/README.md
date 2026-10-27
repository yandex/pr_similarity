# Example baskets

The 2026H1 frozen baskets contain tasks from pandas and OpenSearch PRs merged
in January–June 2026. Choose 1, 5, 10, 25 or 100 tasks per repository; baskets
within each repository are disjoint, not nested. They work without rerunning the
collector or generating tasks again.

| Tasks | pandas | OpenSearch |
|---:|---|---|
| 1 | [pandas-2026H1-1.json](pandas/pandas-2026H1-1.json) | [opensearch-2026H1-1.json](opensearch/opensearch-2026H1-1.json) |
| 5 | [pandas-2026H1-5.json](pandas/pandas-2026H1-5.json) | [opensearch-2026H1-5.json](opensearch/opensearch-2026H1-5.json) |
| 10 | [pandas-2026H1-10.json](pandas/pandas-2026H1-10.json) | [opensearch-2026H1-10.json](opensearch/opensearch-2026H1-10.json) |
| 25 | [pandas-2026H1-25.json](pandas/pandas-2026H1-25.json) | [opensearch-2026H1-25.json](opensearch/opensearch-2026H1-25.json) |
| 100 | [pandas-2026H1-100.json](pandas/pandas-2026H1-100.json) | [opensearch-2026H1-100.json](opensearch/opensearch-2026H1-100.json) |

Run from `benchmark/` after [setup and model login](../../README.md#quick-start):

```bash
make run CONFIG=examples/full.yaml DATASET=examples/baskets/pandas/pandas-2026H1-1.json OUTPUT=./output
```

Each basket freezes task statements, change metadata and exact Git revisions.
The basket's generator model does not select the benchmarked agent; choose any
agent configuration. No reference patches are embedded; the benchmark retrieves
public Git sources. OpenSearch is a large repository; check your configured Git
transfer budget and available disk space before a run.

## Format and saved runs

Each file is a ready-to-use frozen input. Choose a basket size for the task
budget you want; the sets are disjoint, so a larger file is not a superset of a
smaller one. Task statements, source revisions and recorded collection lineage
are part of the input and must stay unchanged.

A run binds the exact basket bytes and content hash. Reuse those bytes for
comparisons and resume; a different export requires a new run.

For a compact synthetic format example, see the
[GitHub contract fixture](../../tests/fixtures/basket-v3/github.json). Its fake
revisions are for offline validation, not a live run.

[Benchmark](../../README.md) · [PR Similarity](../../../README.md)
