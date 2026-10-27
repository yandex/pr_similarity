# Collector examples

These configs select repository changes and turn them into frozen coding tasks.
Start with [github-llm-diff.yaml](github-llm-diff.yaml) for one small task, or
[full.yaml](full.yaml) to see every setting and its alternatives.

| File | Use it for |
|---|---|
| [full.yaml](full.yaml) | Complete configuration reference |
| [github-llm-diff.yaml](github-llm-diff.yaml) | Generate a task from a GitHub PR diff |
| [github-llm-diff-issue.yaml](github-llm-diff-issue.yaml) | Generate from a diff and linked issues |
| [github-llm-issue.yaml](github-llm-issue.yaml) | Generate from linked issue text |
| [github-issue.yaml](github-issue.yaml) | Copy issue text without model calls |
| [github-date-range.yaml](github-date-range.yaml) | Filter and sample changes from a date interval |
| [github-pandas-date-range.yaml](github-pandas-date-range.yaml) | Random sample of up to 10 pandas tasks from January 2026 |
| [gitlab-mailman-date-range.yaml](gitlab-mailman-date-range.yaml) | Random sample of up to 10 Mailman tasks from January–June 2026 |
| [gitlab-llm-diff.yaml](gitlab-llm-diff.yaml) | Generate a task from a GitLab merge-request diff |
| [smoke-live.yaml](smoke-live.yaml) | Run a bounded live collection check |

Run commands from `basket_collector/`, passing `CONFIG=examples/<file>.yaml`.
Paths inside YAML resolve from the config file. When copying an example, update
its dataset identity and output/cache paths; keep credentials in the environment.
LLM recipes make paid API calls. Follow the [quick start](../README.md#quick-start)
and [operations guide](../docs/operations.md) before collecting or retrying work.

## Random samples from a date interval

[github-pandas-date-range.yaml](github-pandas-date-range.yaml) selects up to ten
pandas tasks from changes merged in January 2026.
[gitlab-mailman-date-range.yaml](gitlab-mailman-date-range.yaml) selects up to ten
Mailman Core tasks from changes merged in January–June 2026. The UTC interval includes
`merged_from` and excludes `merged_before`.
The Mailman example allows `src/mailman/docs/NEWS.rst` alongside code changes,
while still excluding other documentation and CI paths and requiring at least
half the changed files to be Python source files.

From `basket_collector/`, after the quick-start setup:

```bash
make basket CONFIG=examples/github-pandas-date-range.yaml
make basket CONFIG=examples/gitlab-mailman-date-range.yaml
```

Set `GITHUB_TOKEN` for pandas or `GITLAB_TOKEN` for Mailman, and `OPENAI_API_KEY`
for generation, in the environment or `basket_collector/.env`. GitLab requires
a token even for this public project.
Discovery collects candidates within the interval; local filters and percentile
trimming run before `shuffle: true`, `seed: 0`, and `limit: 10` select the sample.
The seed reproduces selection for the same candidate set. Ten is a maximum,
not a guaranteed task count: filtering and generation can leave fewer tasks.
`collect.max_changes` and `source.max_pages` bound discovery, not the final sample;
if discovery reaches either cap, the sample may not represent the whole interval.
Generation uses the paid OpenAI API. Change dataset identity and output/cache
paths when adapting the interval or filters for a new basket.
