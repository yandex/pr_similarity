# Task-generation prompts

These Jinja templates turn collected evidence into the messages sent to the task
generator. Start with [system.jinja2](system.jinja2) for the generation rules,
then choose a user template matching `generation.context`:

| File | Purpose |
|---|---|
| [system.jinja2](system.jinja2) | Shared instructions and the required result structure |
| [diff.jinja2](diff.jinja2) | PR title, description, commit messages and diff |
| [issue.jinja2](issue.jinja2) | Linked issue titles and bodies |
| [diff_issue.jinja2](diff_issue.jinja2) | Both PR evidence and linked issues |

Set `generation.templates.system` and `generation.templates.user` in your YAML;
paths resolve from that config file. See the [full example](../examples/full.yaml)
for the wiring. These templates apply to LLM generation; `generation.source: issue`
copies issue text without a model call.

Keep template variables intact when editing. Template contents participate in
saved-input identity, so changing them can invalidate reuse of generated tasks.
See the [operations guide](../docs/operations.md) before resuming existing work.
