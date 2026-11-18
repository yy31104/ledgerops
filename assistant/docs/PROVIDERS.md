# Answer providers

| Provider | Use |
|---|---|
| `fake` | Deterministic sentence copier for CI and local wiring checks |
| `openai_api` | Responses API with an explicit model, API key and token prices |
| `ollama` | A configured local model |
| `chatgpt_subscription` | Existing ChatGPT sign-in through the official Codex Python SDK; local CLI only |

All providers return the same strict answer schema. Citation checks accept only
passage IDs supplied to that call; they do not establish semantic correctness.

## Local ChatGPT subscription

The adapter uses `openai-codex`, pinned in `uv.lock`. Sign in through the official
CLI with `codex login`, then run from `assistant/`:

```sh
uv sync --locked --extra subscription
LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION=1 uv run --locked --extra subscription \
  python -m ledgerops_assistant --backend memory --provider chatgpt_subscription \
  "Does a timeout release the hold?"
```

The default subscription model is `gpt-6.1-sol`; set
`LEDGEROPS_ASSISTANT_SUBSCRIPTION_MODEL` to choose another supported model.

The SDK owns sign-in. The adapter never reads or copies its credential file and
rejects API-key accounts. Calls consume ChatGPT plan quota; a per-request dollar
bill cannot be inferred from the subscription price. This provider is disabled in
CI and the HTTP service.

Each call uses an empty temporary directory, an ephemeral thread, read-only
filesystem mode and a process-group deadline. Shell execution, web search and
MCP/app integrations are disabled; escalation is denied. Codex skills, plugins,
apps, multi-agent and media tools, and its collaboration, permission and
environment notes are also off, so a call does not carry the local user's Codex
setup. These settings constrain the SDK but do not prove independent OS isolation.
Use trusted local material.
The SDK turn interface has no hard output-token cap; deadlines and concise-answer
instructions bound the call.

Historical smoke checks exercised cited answers, a refusal and draft recovery.
They are not representative answer-quality or injection-safety measurements.
See the [evaluation report](RETRIEVAL-QUALITY-V2.md#answer-review) for ratings of
recorded subscription answers.

## API and Ollama

For API mode, install `uv sync --locked --extra openai`, keep `OPENAI_API_KEY`
outside Git and set `LEDGEROPS_ASSISTANT_OPENAI_MODEL`. Set positive
`MODEL_INPUT_USD_PER_MILLION` and `MODEL_OUTPUT_USD_PER_MILLION` for that model.
For Ollama, set `LEDGEROPS_ASSISTANT_OLLAMA_MODEL` and its local server address.
A request cannot choose a URL or credential. Provider input/output counters and latency are recorded per request when
available; configured-price estimates and budget reservations are distinct from
billed cost. The subscription recorder keeps cached-input counts and the last
model request's counters. Controlled calls split the input tokens; see
[token use](RETRIEVAL-QUALITY-V2.md#token-use).

Official references: [authentication](https://learn.chatgpt.com/docs/auth),
[Python SDK](https://learn.chatgpt.com/docs/codex-sdk).
