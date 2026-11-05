# Switchable answer providers

`fake` is the default and the only generation provider in CI. `openai_api` uses the Responses API with an explicit model and API key. `ollama` uses a configured local model. All return the same strict answer schema and pass citation validation.

`chatgpt_subscription` is implemented through the official `openai-codex` Python SDK (pinned by uv.lock). It reuses an existing ChatGPT sign-in; the adapter never reads, copies or exports the credential file. API-key sign-in is rejected for this route. It is deliberately disabled in CI and in the HTTP service.

```sh
cd assistant
uv sync --locked --extra subscription
LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1 uv run --locked --extra subscription python -m ledgerops_assistant --backend memory --provider chatgpt_subscription "Does a timeout release the hold?"
```

Install the official CLI and sign in with `codex login` if needed. This consumes ChatGPT plan usage, not free or unlimited inference; availability follows the account. The SDK runs in an empty temporary working directory, uses ephemeral threads, read-only filesystem mode, denies escalation, disables shell/unified execution, web search and configured MCP/app integrations, and has a process-group deadline. These are local harness controls, not an independently verified OS isolation environment. Use this adapter only with trusted local material; it is not a hosted multi-user completion API. No real subscription turn was run during implementation. Adapter and CI guards are tested without inference.

For API mode, install `uv sync --locked --extra openai`, set `OPENAI_API_KEY` outside Git and choose `LEDGEROPS_ASSISTANT_OPENAI_MODEL`. Ollama uses `LEDGEROPS_ASSISTANT_OLLAMA_MODEL`. The request itself never chooses a URL or credentials. Usage counts are recorded when provided; no cost estimate is inferred from a ChatGPT plan price.

Official documentation checked 2026-11-05: [authentication](https://learn.chatgpt.com/docs/auth), [Python SDK](https://learn.chatgpt.com/docs/codex-sdk). The current [Sign in with ChatGPT documentation](https://developers.openai.com/siwc/token-sharing-open-source) also describes plan usage for OSS and locally hosted apps. That separate OAuth integration is outside this local SDK adapter.
