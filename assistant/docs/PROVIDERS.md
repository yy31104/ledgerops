# Switchable answer providers

`fake` is the default and the only generation provider in CI. `openai_api` uses the Responses API with an explicit model and API key. `ollama` uses a configured local model. All return the same strict answer schema and pass citation validation.

`chatgpt_subscription` is implemented through the official `openai-codex` Python SDK (pinned by uv.lock). It reuses an existing ChatGPT sign-in; the adapter never reads, copies or exports the credential file. API-key sign-in is rejected for this route. It is deliberately disabled in CI and in the HTTP service.

```sh
cd assistant
uv sync --locked --extra subscription
LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1 uv run --locked --extra subscription python -m ledgerops_assistant --backend memory --provider chatgpt_subscription "Does a timeout release the hold?"
```

Install the official CLI and sign in with `codex login` if needed. This consumes ChatGPT plan usage; availability follows the account. The SDK uses the configured model default with explicit low reasoning effort for short structured turns. It runs in an empty temporary working directory with ephemeral threads, read-only filesystem mode, denied escalation, disabled shell/unified execution, web search and configured MCP/app integrations, and a process-group deadline. These are application controls, not independently verified OS isolation. Use trusted local material; this adapter is not a hosted multi-user completion API. The SDK turn interface does not expose a hard output-token cap, so the agent's deadline and concise-answer instructions bound this route.

Limited live checks used the pinned SDK and existing ChatGPT sign-in without an API key: cited timeout/hold, admission-limit and reconciliation answers, one no-answer refusal and one synthetic document-injection case. A synthetic GET-only workflow with two public-document passages then completed six low-effort model calls: the model selected payout, operation, investigation and metrics reads; changing metric age from 10 to 43 preserved handoff, changing payout amount rejected review, and repeated resume preserved one draft without generation. Two earlier workflow attempts safely refused; one recorded a 30-second call timeout. Low effort improved this smoke run's latency but does not guarantee completion. These checks establish neither representative answer quality nor comprehensive injection resistance or independent OS isolation. CI continues to use fake generation only.

For API mode, install `uv sync --locked --extra openai`, set `OPENAI_API_KEY` outside Git and choose `LEDGEROPS_ASSISTANT_OPENAI_MODEL`. Ollama uses `LEDGEROPS_ASSISTANT_OLLAMA_MODEL`. The request itself never chooses a URL or credentials. Usage counts are recorded when provided; no cost estimate is inferred from a ChatGPT plan price.

Official documentation checked 2026-11-05: [authentication](https://learn.chatgpt.com/docs/auth), [Python SDK](https://learn.chatgpt.com/docs/codex-sdk). The current [Sign in with ChatGPT documentation](https://developers.openai.com/siwc/token-sharing-open-source) also describes plan usage for OSS and locally hosted apps. That separate OAuth integration is outside this local SDK adapter.
