# Benchmark examples

Start with [full.yaml](full.yaml): the complete GPT-6.1 Sol medium baseline with
ChatGPT OAuth and the `baseline` tag. Choose tasks from the [basket catalog](baskets/README.md).
Run commands from `benchmark/`; follow the [quick start](../README.md#quick-start)
for setup and login.

| File or directory | Use it for |
|---|---|
| [full.yaml](full.yaml) | Every harness setting, including optional alternatives |
| [chatgpt-oauth.yaml](chatgpt-oauth.yaml) | A shorter OAuth baseline config |
| [openai-api-key.yaml](openai-api-key.yaml) | API-key access |
| [openai-endpoint.yaml](openai-endpoint.yaml) | A custom OpenAI-compatible endpoint |
| [openai-tdd.yaml](openai-tdd.yaml) | An agent using the bundled TDD skill |
| [custom-tools.yaml](custom-tools.yaml) | An agent with a setup-installed documentation search tool |
| [pricing.openai-gpt-6.1-sol.yaml](pricing.openai-gpt-6.1-sol.yaml) | Estimate API-equivalent cost from saved usage |
| [baskets/](baskets/README.md) | Frozen pandas and OpenSearch task sets |

The TDD recipe uses [agent-tdd.md](agent-tdd.md) and the
[bundled skill](skills/test-driven-development/SKILL.md). The custom-tools recipe
uses [setup-custom-tools.sh](setup-custom-tools.sh) and
[agent-custom-tools.md](agent-custom-tools.md). Keep these relative paths valid
when copying configs. These resources are optional; the baseline does not load them.

`CONFIG` selects the agent; `DATASET` selects the basket. See the
[configuration guide](../docs/configuration.md) for customization and the
[pricing guide](../docs/pricing.md) before applying a saved pricing policy.
